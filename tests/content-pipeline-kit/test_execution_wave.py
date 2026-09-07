"""Tests for content_pipeline.execution.wave.

Pins the A-min.2 ready-wave contract: flat readiness is every PENDING unit in
ordinal order (capped by ``max_wave_size``); graph readiness is strictly
sequential (empty or exactly one unit -- the lowest-ordinal PENDING unit whose
predecessor is ACCEPTED *and applied* -- its last apply-kind attempt is
``AttemptKind.APPLY_SUCCEEDED`` -- or SKIPPED); a terminally FAILED predecessor
blocks the chain permanently; and ``max_wave_size > 1`` against a graph
strategy raises ``UnsafeGraphParallelismError`` eagerly, before any store
read.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import (
    UnsafeGraphParallelismError,
    graph_block_reason,
    is_graph_strategy,
    ready_wave,
)
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy


def _new_store(tmp_path, **kwargs) -> ExecutionStore:
    return ExecutionStore(tmp_path / "run.db", **kwargs)


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1", "u2")) -> ExecutionStore:
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="test-model", adapter_version="1"
    )
    store.register_units("run-1", list(unit_ids))
    return store


class _ExplodingStore:
    """A store stand-in whose ``list_units`` fails the test if ever called.

    Used to prove ``UnsafeGraphParallelismError`` is raised BEFORE any store
    read, not merely that the exception type is right.
    """

    def list_units(self, run_id):  # pragma: no cover - must never run
        raise AssertionError("store was read despite max_wave_size > 1 against a graph strategy")


class _ReadSpyStore:
    """Wraps a real ``ExecutionStore``, counting calls to ``snapshot``,
    ``list_units``, and ``list_attempts`` while delegating everything else
    (including the read methods themselves) to the real store.

    Used to pin that the graph path reads through ``snapshot`` -- ONE
    read-transaction call -- rather than a separate ``list_units`` +
    ``list_attempts`` pair, which would reopen the exact torn-read window
    ``snapshot`` exists to close (see ``wave.py``'s module docstring).
    """

    def __init__(self, real: ExecutionStore) -> None:
        self._real = real
        self.snapshot_calls = 0
        self.list_units_calls = 0
        self.list_attempts_calls = 0

    def snapshot(self, run_id, **kwargs):
        self.snapshot_calls += 1
        return self._real.snapshot(run_id, **kwargs)

    def list_units(self, run_id):
        self.list_units_calls += 1
        return self._real.list_units(run_id)

    def list_attempts(self, run_id, unit_id=None):
        self.list_attempts_calls += 1
        return self._real.list_attempts(run_id, unit_id)

    def __getattr__(self, name):
        return getattr(self._real, name)


FLAT_STRATEGY = FlatChunkStrategy(select=lambda store: [])
GRAPH_STRATEGY = GraphWalkStrategy(order=lambda store: [])


# -- is_graph_strategy --------------------------------------------------------

def test_is_graph_strategy_true_for_graph_walk():
    assert is_graph_strategy(GRAPH_STRATEGY) is True


def test_is_graph_strategy_false_for_flat_chunk():
    assert is_graph_strategy(FLAT_STRATEGY) is False


# -- flat readiness ------------------------------------------------------------

def test_flat_readiness_returns_every_pending_unit_in_ordinal_order(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u2", "u0", "u1"))
    wave = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [u.unit_id for u in wave] == ["u2", "u0", "u1"]
    assert [u.ordinal for u in wave] == [0, 1, 2]


def test_flat_readiness_respects_max_wave_size(tmp_path):
    store = _seeded_store(tmp_path)
    wave = ready_wave(store, "run-1", FLAT_STRATEGY, max_wave_size=2)
    assert [u.unit_id for u in wave] == ["u0", "u1"]


def test_flat_readiness_excludes_claimed_accepted_failed(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0", "u1", "u2", "u3"))
    r1 = store.claim_unit("run-1", "u1", "worker-1")
    store.accept_unit("run-1", "u1", r1.fencing_token)
    r2 = store.claim_unit("run-1", "u2", "worker-1")
    store.fail_unit("run-1", "u2", r2.fencing_token, terminal=True)
    store.claim_unit("run-1", "u3", "worker-1")  # left CLAIMED

    wave = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [u.unit_id for u in wave] == ["u0"]


# -- graph readiness ------------------------------------------------------------

def test_graph_readiness_first_unit_ready_when_pending(tmp_path):
    store = _seeded_store(tmp_path)
    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in wave] == ["u0"]


def test_graph_readiness_withholds_successor_until_predecessor_is_applied(tmp_path):
    """Decided rule (2026-08-17): a bare ACCEPTED predecessor is not enough --
    ``ACCEPTED`` only means the text was accepted into the store at submit
    time (D1), not that ``finalize_run`` has applied it. Readiness must be
    apply-aware, or ``ready_wave -> run_wave(accept) -> ready_wave`` hands
    back a successor over an unapplied predecessor even though ``prepare_run``
    already refuses this exact case loudly via ``UnappliedPredecessorError``.
    """
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    # Deliberately no record_apply_succeeded -- the predecessor is accepted
    # but not yet applied.

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert wave == []


def test_graph_readiness_releases_successor_once_predecessor_is_applied(tmp_path):
    """Companion to the withholding test above: once the predecessor's apply
    has actually succeeded, the successor becomes ready."""
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    store.record_apply_succeeded("run-1", "u0")

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in wave] == ["u1"]
    assert len(wave) == 1


def test_graph_readiness_withholds_successor_after_peer_accepts_without_applying_between_two_reads(tmp_path):
    """Multi-process-relevant case: a peer's ``accept_unit`` alone (no apply)
    landing between two SEPARATE ``ready_wave`` calls must not release the
    successor on the second call. Guards against a regression that treats
    "unit state changed since I last looked" as equivalent to "predecessor
    is applied".

    NOTE: this is single-threaded and reaches a store state identical to
    ``test_graph_readiness_withholds_successor_until_predecessor_is_applied``
    above -- it does NOT exercise a torn read within one ``ready_wave`` call
    (that atomicity guarantee is pinned separately by
    ``test_graph_readiness_reads_the_store_exactly_once_via_snapshot``, which
    proves the graph path never opens the two-read window a torn read would
    require). This test's only added value over the simpler one is
    confirming the withholding re-evaluates correctly across two INDEPENDENT
    ``ready_wave`` calls, not just one.
    """
    store = _seeded_store(tmp_path)

    first = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in first] == ["u0"]

    # A peer worker claims and accepts u0 -- but never applies it.
    r0 = store.claim_unit("run-1", "u0", "worker-2")
    store.accept_unit("run-1", "u0", r0.fencing_token)

    second = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert second == []


def test_graph_readiness_withholds_successor_after_apply_rejection(tmp_path):
    store = _seeded_store(tmp_path)
    claim = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", claim.fencing_token, text="t")
    store.record_apply_rejected("run-1", "u0", "stale anchored slice")

    assert ready_wave(store, "run-1", GRAPH_STRATEGY) == []
    reason = graph_block_reason(store, "run-1", GRAPH_STRATEGY)
    assert reason is not None
    assert "apply was refused" in reason
    assert "plan another run" in reason


def test_graph_readiness_re_withholds_after_apply_started_follows_apply_succeeded(tmp_path):
    """Attempts ordered [claim, accept, apply_succeeded, apply_started] --
    a LATER APPLY_STARTED after an APPLY_SUCCEEDED must RE-withhold the
    successor, because the true last apply-kind attempt is APPLY_STARTED
    (apply_unknown), not APPLY_SUCCEEDED.

    This pins two things the shipped code already gets right and a wrong
    implementation can get wrong independently:
    - the check must be "LAST apply-kind attempt is APPLY_SUCCEEDED", not
      "ANY attempt is APPLY_SUCCEEDED" (kills an ``any()``-based mutant);
    - ``_last_apply_kind`` must scan attempts in their true chronological
      (forward) order, not reversed (kills a ``reversed(attempts)`` mutant,
      which would report the OLDER of the two apply-kind attempts as "last").
    """
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    store.record_apply_succeeded("run-1", "u0")
    store.record_apply_started("run-1", "u0")

    attempts = store.list_attempts("run-1", "u0")
    assert [a.kind for a in attempts] == [
        AttemptKind.CLAIM,
        AttemptKind.ACCEPT,
        AttemptKind.APPLY_SUCCEEDED,
        AttemptKind.APPLY_STARTED,
    ]

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert wave == []


def test_graph_readiness_reads_the_store_exactly_once_via_snapshot(tmp_path):
    """Pins the commit's headline atomicity mechanism: the graph path must
    read units and attempts together via ONE ``store.snapshot`` call, never
    via separate ``list_units`` + ``list_attempts`` calls (which would
    reopen the torn-read window ``snapshot`` exists to close -- see the
    module docstring)."""
    store = _seeded_store(tmp_path)
    spy = _ReadSpyStore(store)

    wave = ready_wave(spy, "run-1", GRAPH_STRATEGY)

    assert [u.unit_id for u in wave] == ["u0"]
    assert spy.snapshot_calls == 1
    assert spy.list_units_calls == 0
    assert spy.list_attempts_calls == 0


def test_graph_readiness_skips_over_unit_whose_predecessor_is_still_pending(tmp_path):
    # Three-unit chain, nothing touched: u0 is PENDING with no predecessor
    # (vacuously ready), so it alone is the wave -- u1, whose predecessor
    # (u0) is PENDING rather than ACCEPTED, is never surfaced alongside it.
    store = _seeded_store(tmp_path)
    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in wave] == ["u0"]


def test_graph_readiness_empty_when_predecessor_claimed(tmp_path):
    store = _seeded_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-1")  # left CLAIMED, not accepted

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert wave == []


def test_graph_readiness_yields_lowest_ordinal_pending_after_predecessor_skipped(tmp_path):
    """Finding 1's fix: a SKIPPED predecessor unblocks its successor exactly
    like an ACCEPTED one -- a skip is not a broken link, it is a unit the
    run intentionally will not produce."""
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.fail_unit("run-1", "u0", r0.fencing_token, terminal=True, terminal_state=UnitState.SKIPPED)

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in wave] == ["u1"]
    assert len(wave) == 1


def test_graph_readiness_blocked_permanently_by_terminally_failed_predecessor(tmp_path):
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.fail_unit("run-1", "u0", r0.fencing_token, terminal=True)

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert wave == []

    # Confirm this is permanent: u0 stays FAILED (terminal), so a second read
    # still yields nothing -- the chain never recovers.
    wave_again = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert wave_again == []
    assert store.get_unit("run-1", "u0").state is UnitState.FAILED


# -- graph_block_reason -----------------------------------------------------


def test_graph_block_reason_none_for_flat_strategy(tmp_path):
    store = _seeded_store(tmp_path)
    assert graph_block_reason(store, "run-1", FLAT_STRATEGY) is None


def test_graph_block_reason_none_when_ready(tmp_path):
    store = _seeded_store(tmp_path)
    assert graph_block_reason(store, "run-1", GRAPH_STRATEGY) is None


def test_graph_block_reason_none_when_no_pending_unit(tmp_path):
    store = _seeded_store(tmp_path, unit_ids=("u0",))
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    store.record_apply_succeeded("run-1", "u0")
    assert graph_block_reason(store, "run-1", GRAPH_STRATEGY) is None


def test_graph_block_reason_names_unapplied_accepted_predecessor(tmp_path):
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)

    reason = graph_block_reason(store, "run-1", GRAPH_STRATEGY)
    assert reason is not None
    assert "u1" in reason
    assert "u0" in reason
    assert "finalize_run" in reason


def test_graph_block_reason_names_apply_unknown_predecessor(tmp_path):
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    store.record_apply_started("run-1", "u0")

    reason = graph_block_reason(store, "run-1", GRAPH_STRATEGY)
    assert reason is not None
    assert "apply_unknown" in reason
    assert "reconcile" in reason


def test_graph_block_reason_names_terminally_failed_predecessor(tmp_path):
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.fail_unit("run-1", "u0", r0.fencing_token, terminal=True)

    reason = graph_block_reason(store, "run-1", GRAPH_STRATEGY)
    assert reason is not None
    assert "FAILED" in reason


def test_graph_block_reason_names_other_predecessor_state(tmp_path):
    store = _seeded_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-1")  # left CLAIMED

    reason = graph_block_reason(store, "run-1", GRAPH_STRATEGY)
    assert reason is not None
    assert "claimed" in reason


# -- max_wave_size vs. graph strategies -----------------------------------------

def test_max_wave_size_greater_than_one_raises_before_store_read():
    exploding_store = _ExplodingStore()
    with pytest.raises(UnsafeGraphParallelismError):
        ready_wave(exploding_store, "run-1", GRAPH_STRATEGY, max_wave_size=2)


def test_max_wave_size_one_is_allowed_against_graph_strategy(tmp_path):
    store = _seeded_store(tmp_path)
    wave = ready_wave(store, "run-1", GRAPH_STRATEGY, max_wave_size=1)
    assert [u.unit_id for u in wave] == ["u0"]


# -- ordinals survive holes ------------------------------------------------------

def test_ordinals_survive_holes_in_a_run(tmp_path):
    store = _new_store(tmp_path)
    store.create_run(
        "run-1", driver="inline", backend="mock", model="test-model", adapter_version="1"
    )
    store.register_units("run-1", ["u0", "u1"])
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)
    store.record_apply_succeeded("run-1", "u0")
    store.register_units("run-1", ["u2", "u3"])
    # A hole in the PENDING set: ordinal 0 (u0) is ACCEPTED (and applied), so
    # it is absent from any PENDING-filtered result even though ordinals 1-3
    # are contiguous and PENDING. Original ordinals must survive regardless.

    flat_wave = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [(u.unit_id, u.ordinal) for u in flat_wave] == [
        ("u1", 1),
        ("u2", 2),
        ("u3", 3),
    ]

    graph_wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [(u.unit_id, u.ordinal) for u in graph_wave] == [("u1", 1)]
