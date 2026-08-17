"""Tests for content_pipeline.execution.wave.

Pins the A-min.2 ready-wave contract: flat readiness is every PENDING unit in
ordinal order (capped by ``max_wave_size``); graph readiness is strictly
sequential (empty or exactly one unit -- the lowest-ordinal PENDING unit whose
predecessor is ACCEPTED); a terminally FAILED predecessor blocks the chain
permanently; and ``max_wave_size > 1`` against a graph strategy raises
``UnsafeGraphParallelismError`` eagerly, before any store read.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import (
    UnsafeGraphParallelismError,
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


def test_graph_readiness_yields_lowest_ordinal_pending_after_accept(tmp_path):
    store = _seeded_store(tmp_path)
    r0 = store.claim_unit("run-1", "u0", "worker-1")
    store.accept_unit("run-1", "u0", r0.fencing_token)

    wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [u.unit_id for u in wave] == ["u1"]
    assert len(wave) == 1


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
    store.register_units("run-1", ["u2", "u3"])
    # A hole in the PENDING set: ordinal 0 (u0) is ACCEPTED, so it is absent
    # from any PENDING-filtered result even though ordinals 1-3 are
    # contiguous and PENDING. Original ordinals must survive regardless.

    flat_wave = ready_wave(store, "run-1", FLAT_STRATEGY)
    assert [(u.unit_id, u.ordinal) for u in flat_wave] == [
        ("u1", 1),
        ("u2", 2),
        ("u3", 3),
    ]

    graph_wave = ready_wave(store, "run-1", GRAPH_STRATEGY)
    assert [(u.unit_id, u.ordinal) for u in graph_wave] == [("u1", 1)]
