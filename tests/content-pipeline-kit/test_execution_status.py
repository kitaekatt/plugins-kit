"""Tests for content_pipeline.execution.status.

Pins the bounded RunStatus digest: counts by state, elapsed time, oldest
in-flight age, expired-lease count, fixed-window throughput, capped recent
failure groups, pause/halt state -- and invariant 6, that the digest never
contains prompts, unit payloads, or full outputs.
"""

from __future__ import annotations

import dataclasses
import sqlite3

import pytest

from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.status import (
    DEFAULT_MAX_FAILURE_GROUPS,
    FailureGroup,
    RunStatus,
    compute_status,
)
from content_pipeline.execution.store import ExecutionStore


def _store(tmp_path, *, unit_ids=("u0", "u1", "u2", "u3")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run("r1", driver="inline", backend="mock", model="m", adapter_version="1")
    store.register_units("r1", list(unit_ids))
    return store


def test_unknown_run_raises_key_error(tmp_path):
    store = ExecutionStore(tmp_path / "run.db")
    with pytest.raises(KeyError):
        compute_status(store, "no-such-run")


def test_counts_by_state_and_total_units(tmp_path):
    store = _store(tmp_path)
    claim = store.claim_unit("r1", "u0", "w1")
    store.accept_unit("r1", "u0", claim.fencing_token)
    store.claim_unit("r1", "u1", "w2")

    digest = compute_status(store, "r1", now=1000.0)
    assert digest.total_units == 4
    assert digest.counts_by_state["accepted"] == 1
    assert digest.counts_by_state["claimed"] == 1
    assert digest.counts_by_state["pending"] == 2
    assert digest.counts_by_state["failed"] == 0


def test_elapsed_and_oldest_in_flight_age(tmp_path):
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        "r1", driver="inline", backend="mock", model="m", adapter_version="1", created_at=100.0
    )
    store.register_units("r1", ["u0", "u1"], at=100.0)
    store.claim_unit("r1", "u0", "w1", at=110.0)  # in flight since 110

    digest = compute_status(store, "r1", now=150.0)
    assert digest.elapsed_s == 50.0
    assert digest.oldest_in_flight_age_s == 40.0  # 150 - 110


def test_oldest_in_flight_age_is_measured_from_claim_not_from_the_last_renew(tmp_path):
    """Defect 9: a renew must not reset the in-flight age -- it is the SAME
    claim, just with a refreshed lease. Before the fix this measured time
    since ``updated_at`` (which a renew touches), so a renewed unit looked
    freshly claimed no matter how long it had actually been in flight."""
    store = _store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("r1", "u0", "w1", at=100.0)
    store.renew_lease("r1", "u0", claim.fencing_token, at=140.0)

    digest = compute_status(store, "r1", now=150.0)
    assert digest.oldest_in_flight_age_s == 50.0  # since the CLAIM at 100, not the renew at 140


def test_expired_lease_count(tmp_path):
    store = _store(tmp_path, unit_ids=("u0", "u1"))
    store.claim_unit("r1", "u0", "w1", lease_seconds=10, at=1000.0)
    store.claim_unit("r1", "u1", "w2", lease_seconds=1000, at=1000.0)

    digest = compute_status(store, "r1", now=1020.0)  # u0's lease has expired
    assert digest.expired_lease_count == 1


def test_fixed_window_throughput_counts_only_within_window(tmp_path):
    store = _store(tmp_path, unit_ids=("u0", "u1"))
    claim0 = store.claim_unit("r1", "u0", "w1", at=0.0)
    store.accept_unit("r1", "u0", claim0.fencing_token, at=10.0)  # inside a 60s window at now=60
    claim1 = store.claim_unit("r1", "u1", "w2", at=0.0)
    store.accept_unit("r1", "u1", claim1.fencing_token, at=-100.0)  # long before the window

    digest = compute_status(store, "r1", now=60.0, throughput_window_s=60.0)
    assert digest.accepted_in_window == 1


def test_failed_in_window_counts_units_not_fail_attempts(tmp_path):
    """failed_in_window counts units, not FAIL ATTEMPT rows: counting rows, one
    unit failed three times then accepted reports accepted=1, failed=3 for
    total_units=1. A unit retried and then accepted, all inside the window,
    must report accepted_in_window=1 and failed_in_window=0; a unit whose
    last attempt is FAIL (never accepted) counts once, however many times it
    failed."""
    store = _store(tmp_path, unit_ids=("u0", "u1"))

    # u0: fails three times, then is accepted -- all inside the window.
    for i in range(3):
        claim = store.claim_unit("r1", "u0", f"w{i}", at=float(i))
        store.fail_unit("r1", "u0", claim.fencing_token, error="boom", terminal=False, at=float(i) + 0.5)
    final_claim = store.claim_unit("r1", "u0", "w-final", at=3.0)
    store.accept_unit("r1", "u0", final_claim.fencing_token, at=4.0)

    # u1: fails twice, never accepted -- still inside the window.
    for i in range(2):
        claim = store.claim_unit("r1", "u1", f"x{i}", at=float(i))
        store.fail_unit("r1", "u1", claim.fencing_token, error="also boom", terminal=False, at=float(i) + 0.5)

    digest = compute_status(store, "r1", now=10.0, throughput_window_s=60.0)
    assert digest.accepted_in_window == 1
    assert digest.failed_in_window == 1


def test_snapshot_attempt_kinds_and_since_filters(tmp_path):
    """``ExecutionStore.snapshot`` accepts keyword-only ``attempt_kinds`` and
    ``attempts_since`` filters, applied inside the same single read
    transaction (status.compute_status needs only FAIL attempts inside its
    window; an unfiltered store.snapshot reads every attempt row of the
    run). After claim/renew/accept/fail traffic, filtering to
    ``(AttemptKind.FAIL,)`` returns only the FAIL row, and a since-filter
    excludes an older FAIL."""
    store = _store(tmp_path, unit_ids=("u0", "u1"))
    claim0 = store.claim_unit("r1", "u0", "w1", at=0.0)
    store.renew_lease("r1", "u0", claim0.fencing_token, at=1.0)
    store.accept_unit("r1", "u0", claim0.fencing_token, at=2.0)
    claim1 = store.claim_unit("r1", "u1", "w2", at=0.0)
    store.fail_unit("r1", "u1", claim1.fencing_token, error="boom", at=3.0)

    _run, _units, attempts = store.snapshot("r1", attempt_kinds=(AttemptKind.FAIL,))
    assert len(attempts) == 1
    assert attempts[0].kind is AttemptKind.FAIL
    assert attempts[0].unit_id == "u1"

    _run2, _units2, since_attempts = store.snapshot(
        "r1", attempt_kinds=(AttemptKind.FAIL,), attempts_since=10.0
    )
    assert since_attempts == []

    # Default behaviour (no filters) is unchanged: every attempt row.
    _run3, _units3, all_attempts = store.snapshot("r1")
    assert len(all_attempts) == 5


def test_recent_failures_are_grouped_and_capped(tmp_path):
    unit_ids = [f"u{i}" for i in range(DEFAULT_MAX_FAILURE_GROUPS + 3)]
    store = _store(tmp_path, unit_ids=unit_ids)
    for i, uid in enumerate(unit_ids):
        claim = store.claim_unit("r1", uid, "w1", at=float(i))
        store.fail_unit(
            "r1", uid, claim.fencing_token, error=f"distinct-error-{i}", terminal=True, at=float(i)
        )

    digest = compute_status(store, "r1", now=float(len(unit_ids)), throughput_window_s=1000.0)
    assert len(digest.recent_failures) == DEFAULT_MAX_FAILURE_GROUPS
    assert digest.truncated_failure_groups is True
    assert digest.failed_in_window == len(unit_ids)


def test_recent_failures_group_identical_errors(tmp_path):
    store = _store(tmp_path, unit_ids=("u0", "u1"))
    claim0 = store.claim_unit("r1", "u0", "w1", at=0.0)
    store.fail_unit("r1", "u0", claim0.fencing_token, error="same error", terminal=False, at=1.0)
    claim0b = store.claim_unit("r1", "u0", "w1", at=2.0)
    store.fail_unit("r1", "u0", claim0b.fencing_token, error="same error", terminal=True, at=3.0)

    digest = compute_status(store, "r1", now=10.0)
    matching = [g for g in digest.recent_failures if g.error_code == digest.recent_failures[0].error_code]
    assert len(matching) == 1  # identical raw text still groups under one code
    assert matching[0].count == 2


def test_skipped_units_are_not_counted_as_failures_in_the_digest(tmp_path):
    """Finding 1's status-layer symptom: a terminal skip (execution.
    controller's terminal_state=UnitState.SKIPPED, error="skip:...") is
    recorded through the same fail_unit(terminal=True, ...) write path as a
    real failure. Before the fix it inflated counts_by_state["failed"],
    failed_in_window, and burned a recent_failures slot, and skip:up_to_date
    was indistinguishable from a real failure inside the bounded digest
    (both hashed to opaque codes). counts_by_state["skipped"] must carry the
    skip instead, "failed" must stay at the real-failure count only, and the
    skip must not appear in failed_in_window or recent_failures."""
    store = _store(tmp_path, unit_ids=("u0", "u1"))
    claim0 = store.claim_unit("r1", "u0", "w1", at=0.0)
    store.fail_unit(
        "r1", "u0", claim0.fencing_token,
        error="skip:up_to_date", terminal=True, terminal_state=UnitState.SKIPPED, at=1.0,
    )
    claim1 = store.claim_unit("r1", "u1", "w1", at=0.0)
    store.fail_unit(
        "r1", "u1", claim1.fencing_token, error="a real failure", terminal=True, at=1.0
    )

    digest = compute_status(store, "r1", now=10.0, throughput_window_s=100.0)

    assert digest.counts_by_state["skipped"] == 1
    assert digest.counts_by_state["failed"] == 1  # only the real failure
    assert digest.failed_in_window == 1  # the skip does not inflate this
    assert len(digest.recent_failures) == 1  # the skip does not burn a slot
    assert digest.recent_failures[0].count == 1


def test_halt_state_is_reported(tmp_path):
    store = _store(tmp_path)
    store.set_halt("r1", "rate_limit", "hit your limit")

    digest = compute_status(store, "r1")
    assert digest.halted_kind == "rate_limit"
    assert digest.halted_detail_code  # a non-empty content-free code, not the raw text
    assert digest.halted_detail_code != "hit your limit"


# -- invariant 6: never prompts, unit payloads, or full outputs -------------------

def test_digest_dataclass_has_no_content_bearing_field():
    """A cheap structural smoke check, NOT the invariant-6 pin.

    This only proves the dataclasses have no field NAMED like a content
    carrier -- it says nothing about what actually lands inside a
    correctly-named field (e.g. a raw error string stuffed into
    ``halted_detail_code``). It would pass unchanged even if invariant 6 were
    violated by value rather than by field name. The real guard is
    ``test_digest_never_contains_injected_prompt_shaped_error_or_halt_detail``
    below, which injects content and inspects the serialized digest.
    """
    forbidden_substrings = ("prompt", "payload", "output_text", "response", "result_text")
    for cls in (RunStatus, FailureGroup):
        for f in dataclasses.fields(cls):
            lowered = f.name.lower()
            assert not any(bad in lowered for bad in forbidden_substrings), f.name


def test_digest_never_contains_injected_prompt_shaped_error_or_halt_detail(tmp_path):
    """Defect 3, replacing the tautological 'proof by construction' test.

    The old test only asserted that ``accept_unit``/``fail_unit`` take no
    ``text``/``payload`` PARAMETER -- true, and irrelevant, because
    ``fail_unit`` takes ``error`` and ``set_halt`` takes ``detail``, and both
    landed verbatim in the digest. This injects prompt/output-shaped text
    into both and asserts neither the exact string nor a distinguishing
    substring survives anywhere in the serialized digest.
    """
    store = _store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("r1", "u0", "w1")

    secret_error = "FULL OUTPUT: the quick brown fox jumps SECRET-TOKEN-9f3c1a"
    store.fail_unit("r1", "u0", claim.fencing_token, error=secret_error, terminal=True)

    secret_detail = "SYSTEM PROMPT: you are a helpful assistant SECRET-TOKEN-9f3c1a"
    store.set_halt("r1", "rate_limit", secret_detail)

    digest = compute_status(store, "r1")
    serialized = repr(digest.to_dict())
    assert secret_error not in serialized
    assert secret_detail not in serialized
    assert "SECRET-TOKEN-9f3c1a" not in serialized
    assert "SYSTEM PROMPT" not in serialized
    assert "FULL OUTPUT" not in serialized


# -- one consistent snapshot (defect 6) --------------------------------------------

def test_compute_status_reads_one_consistent_snapshot_despite_a_concurrent_write(
    tmp_path, monkeypatch
):
    """Defect 6: get_run / list_units / list_attempts used to be three
    separate connections/transactions, so a write landing between them could
    produce a torn digest. The interleaved write must mutate what the LATER
    query in the snapshot actually reads -- attempts, read after units -- or
    the test proves nothing about attempt-read isolation (a write to
    ``units``, which is already read into Python before the hook fires,
    can't be observed by a torn read regardless of whether isolation holds).
    This inserts a new FAIL attempt from a SEPARATE connection between the
    units read and the attempts read (by patching the low-level row-fetch
    helper compute_status's snapshot path uses) and asserts the digest still
    reflects the state as of when the snapshot's read transaction began --
    not the interleaved insert.
    """
    import content_pipeline.execution.store as store_mod

    store = _store(tmp_path, unit_ids=("u0", "u1"))
    claim = store.claim_unit("r1", "u1", "w1", at=0.0)
    store.fail_unit(
        "r1", "u1", claim.fencing_token, error="pre-existing", terminal=False, at=0.0
    )  # one FAIL attempt on record before the snapshot begins

    original_fetch_attempts = store_mod._fetch_attempt_rows

    def patched_fetch_attempts(conn, run_id, unit_id=None, **kwargs):
        # A write on an INDEPENDENT connection, landing between the units
        # read and the attempts read of the SAME snapshot transaction. It
        # inserts a new attempts ROW -- the exact table the very next query
        # in this transaction reads -- so a torn read would see it.
        writer = sqlite3.connect(str(store.db_path), timeout=store.busy_timeout_ms / 1000.0)
        try:
            writer.execute(f"PRAGMA busy_timeout = {store.busy_timeout_ms}")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO attempts(run_id, unit_id, kind, at, worker_id, fencing_token, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, "u1", AttemptKind.FAIL.value, 999.0, "w-interloper", 1, "interleaved-error"),
            )
            writer.commit()
        finally:
            writer.close()
        return original_fetch_attempts(conn, run_id, unit_id, **kwargs)

    monkeypatch.setattr(store_mod, "_fetch_attempt_rows", patched_fetch_attempts)

    digest = compute_status(store, "r1", now=1000.0, throughput_window_s=2000.0)

    # The snapshot's read transaction took its view before the interleaved
    # INSERT committed, so only the pre-existing FAIL attempt is visible
    # here -- a torn read would count the interleaved one too (2, not 1).
    assert digest.failed_in_window == 1


def test_digest_field_set_is_exactly_the_declared_bounded_set(tmp_path):
    store = _store(tmp_path, unit_ids=("u0",))
    claim = store.claim_unit("r1", "u0", "w1")
    store.accept_unit("r1", "u0", claim.fencing_token)

    digest = compute_status(store, "r1")
    assert set(digest.to_dict().keys()) == {f.name for f in dataclasses.fields(RunStatus)}


def test_to_dict_round_trips_through_asdict():
    fg = FailureGroup(error_code="abc123", count=1, last_unit_id="u0", last_at=1.0)
    assert dataclasses.asdict(fg) == {
        "error_code": "abc123",
        "count": 1,
        "last_unit_id": "u0",
        "last_at": 1.0,
    }
