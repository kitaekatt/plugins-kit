"""The execution store's waiting state: request, resolve, expire, and reads.

A claim holder asks a person a typed question with ``request_interrupt``; the
unit waits until ``resolve_interrupt`` or ``expire_interrupts`` closes the
request. What the unit does after a rejection or a lapse is the requester's
policy (``stop`` or ``release``); the outcome row is written either way.

The verbs run against the real ``bootstrap_lib.interrupt_contract`` and the
real llm-scripting-kit validator. Expected values are literals.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import sqlite3
import sys
from pathlib import Path

import pytest

import content_pipeline.execution.store as store_mod
from content_pipeline.execution import interrupts
from content_pipeline.execution.controller import prepare_run, unfinished_units
from content_pipeline.execution.interrupts import unit_resolutions, waiting_units
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    ExecutionError,
    InterruptExpiredError,
    InterruptRequest,
    InterruptRequested,
    InterruptRequestError,
    NotClaimedError,
    ResolutionConflictError,
    ResolutionInputError,
    StaleFenceError,
    TerminalStateError,
    UnitState,
    UnitWaitingError,
    UnknownInterruptError,
    UnknownRunError,
    UsageRecord,
    WaitUnderDispatchError,
)
from content_pipeline.execution.status import compute_status
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.wave import graph_block_reason, ready_wave
from content_pipeline.pipeline.gate import Gate
from content_pipeline.pipeline.workunit import FlatChunkStrategy, GraphWalkStrategy, WorkUnit

from bootstrap_lib import interrupt_contract as contract

RUN = "r1"
FLAT = FlatChunkStrategy(select=lambda store: [])
GRAPH = GraphWalkStrategy(order=lambda store: [])

_SHARED_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"

APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"type": "boolean"}},
    "additionalProperties": False,
}

_TABLES = ("runs", "units", "attempts", "dispatches", "interrupts", "interrupt_resolutions")


def _lsk_names():
    return {n for n in sys.modules if n == "llm_scripting_kit" or n.startswith("llm_scripting_kit.")}


@pytest.fixture(autouse=True)
def lsk(monkeypatch):
    """Link llm-scripting-kit's validator for one test; unload it afterwards."""
    before = _lsk_names()
    monkeypatch.syspath_prepend(str(_SHARED_LIB))
    yield
    for name in _lsk_names() - before:
        del sys.modules[name]


def _store(tmp_path, units=("u0", "u1", "u2")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, list(units), at=1000.0)
    return store


def _request(**overrides) -> InterruptRequest:
    fields = {
        "kind": "approval",
        "request_schema": APPROVAL,
        "payload": {"question": "ship it?"},
    }
    fields.update(overrides)
    return InterruptRequest(**fields)


def _wait(store, unit="u0", *, at=1001.0, request=None, **policies):
    """Claim ``unit`` at ``at`` and request an interrupt one second later."""
    token = store.claim_unit(RUN, unit, "w", at=at).fencing_token
    return store.request_interrupt(
        RUN, unit, token, request if request is not None else _request(), at=at + 1.0, **policies
    )


@contextlib.contextmanager
def _sql(store):
    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _rows(store) -> dict:
    """Every row of every table, for a nothing-was-written comparison."""
    with _sql(store) as conn:
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _TABLES
        }


def _kinds(store, unit="u0") -> list:
    return [a.kind.value for a in store.list_attempts(RUN, unit)]


def _insert_interrupt(conn, *, unit="u0", token=7, on_rejected="stop", on_expired="stop"):
    conn.execute(
        "INSERT INTO interrupts(run_id, unit_id, fencing_token, envelope, kind, "
        "request_schema_json, payload_json, created_at, expires_at, on_rejected, on_expired) "
        "VALUES (?, ?, ?, 'plugins-kit.interrupt-request/v1', 'approval', '{}', '{}', 1.0, "
        "NULL, ?, ?)",
        (RUN, unit, token, on_rejected, on_expired),
    )


# -- the digest and the schema step ------------------------------------------


def test_status_lists_wait_states_only_when_occupied(tmp_path):
    store = _store(tmp_path)
    base = {"pending": 3, "claimed": 0, "accepted": 0, "failed": 0, "skipped": 0}
    assert compute_status(store, RUN, now=1100.0).counts_by_state == base

    record = _wait(store, "u0")
    assert compute_status(store, RUN, now=1100.0).counts_by_state == {
        "pending": 2,
        "claimed": 0,
        "accepted": 0,
        "failed": 0,
        "skipped": 0,
        "waiting": 1,
    }

    store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
    assert compute_status(store, RUN, now=1100.0).counts_by_state == {
        "pending": 2,
        "claimed": 0,
        "accepted": 0,
        "failed": 0,
        "skipped": 0,
        "operator_rejected": 1,
    }


def test_store_migrates_to_interrupts_from_step_9(tmp_path, monkeypatch):
    assert len(store_mod._MIGRATIONS) == 10
    db_path = tmp_path / "run.db"
    current = store_mod._MIGRATIONS
    monkeypatch.setattr(store_mod, "_MIGRATIONS", current[:9])
    old = ExecutionStore(db_path)
    old.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    old.register_units(RUN, ["u0", "u1"], at=1000.0)
    token = old.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    existing = ("runs", "units", "attempts", "dispatches")
    with _sql(old) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
        before = {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in existing
        }
        names_before = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "interrupts" not in names_before

    monkeypatch.setattr(store_mod, "_MIGRATIONS", current)
    store = ExecutionStore(db_path)
    with _sql(store) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
        after = {
            t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in existing
        }
        added = {
            (r["type"], r["name"])
            for r in conn.execute("SELECT type, name FROM sqlite_master")
            if r["name"] not in names_before and not r["name"].startswith("sqlite_autoindex")
        }
        assert conn.execute("SELECT COUNT(*) FROM interrupts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM interrupt_resolutions").fetchone()[0] == 0
    assert after == before
    assert added == {
        ("table", "interrupts"),
        ("table", "interrupt_resolutions"),
        ("index", "idx_interrupts_run_unit"),
        ("trigger", "interrupts_one_open_per_unit"),
        ("trigger", "interrupts_immutable_update"),
        ("trigger", "interrupts_immutable_delete"),
        ("trigger", "resolutions_immutable_update"),
        ("trigger", "resolutions_immutable_delete"),
    }
    # The claim made under step 9 can ask under step 10.
    record = store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    assert record.id == "1"
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING


# -- claims against a waiting unit -------------------------------------------


def test_waiting_unit_cannot_be_claimed(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    before = _rows(store)
    # Far past any lease: a waiting unit has none, so time cannot release it.
    with pytest.raises(UnitWaitingError) as info:
        store.claim_unit(RUN, "u0", "w2", at=9_000_000.0)
    assert info.value.interrupt_id == record.id == "1"
    assert "waiting on interrupt 1" in str(info.value)
    assert _rows(store) == before
    unit = store.get_unit(RUN, "u0")
    assert unit.state is UnitState.WAITING
    assert unit.fencing_token == 1


def test_unit_waiting_error_is_an_already_claimed_error(tmp_path):
    assert issubclass(UnitWaitingError, AlreadyClaimedError)
    store = _store(tmp_path)
    _wait(store)
    caught = None
    try:
        store.claim_unit(RUN, "u0", "w2", at=2000.0)
    except AlreadyClaimedError as exc:  # the class dispatch-side claim sites handle
        caught = exc
    assert type(caught) is UnitWaitingError


# -- request_interrupt ---------------------------------------------------------


def test_stale_request_is_superseded_and_raises_stale_fence(tmp_path):
    store = _store(tmp_path)
    old = store.claim_unit(RUN, "u0", "w", lease_seconds=1, at=1001.0).fencing_token
    new = store.claim_unit(RUN, "u0", "w2", at=2000.0).fencing_token
    store.accept_unit(RUN, "u0", new, at=2001.0)
    # The unit is ACCEPTED now. Fencing is checked before state, so the stale
    # caller gets the fence error, not TerminalStateError.
    with pytest.raises(StaleFenceError) as info:
        store.request_interrupt(RUN, "u0", old, _request(), at=2002.0)
    assert (info.value.presented, info.value.current) == (1, 2)
    # The SUPERSEDED row is durable: a second store object reads it.
    reread = ExecutionStore(store.db_path)
    last = reread.list_attempts(RUN, "u0")[-1]
    assert (last.kind, last.fencing_token, last.at) == (AttemptKind.SUPERSEDED, 1, 2002.0)
    assert reread.list_interrupts(RUN) == []
    assert reread.get_unit(RUN, "u0").state is UnitState.ACCEPTED


@pytest.mark.parametrize("state", ["pending", "waiting", "accepted", "failed"])
def test_request_refused_unless_claimed(tmp_path, state):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    expected = NotClaimedError
    if state == "pending":
        store.fail_unit(RUN, "u0", token, error="again", at=1002.0)
    elif state == "waiting":
        store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    elif state == "accepted":
        store.accept_unit(RUN, "u0", token, at=1002.0)
        expected = TerminalStateError
    else:
        store.fail_unit(RUN, "u0", token, error="dead", terminal=True, at=1002.0)
        expected = TerminalStateError
    assert store.get_unit(RUN, "u0").state.value == state
    before = _rows(store)
    with pytest.raises(expected) as info:
        store.request_interrupt(RUN, "u0", token, _request(), at=1003.0)
    assert type(info.value) is expected
    assert _rows(store) == before


def test_request_commits_with_its_attempt_row_atomically(tmp_path, monkeypatch):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    before = _rows(store)
    real = ExecutionStore._record_attempt

    def failing(self, conn, run_id, unit_id, kind, **kwargs):
        if kind is AttemptKind.INTERRUPT_REQUESTED:
            raise RuntimeError("attempt insert failed")
        return real(self, conn, run_id, unit_id, kind, **kwargs)

    monkeypatch.setattr(ExecutionStore, "_record_attempt", failing)
    with pytest.raises(RuntimeError, match="attempt insert failed"):
        store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    monkeypatch.setattr(ExecutionStore, "_record_attempt", real)
    # Neither the interrupt row nor the state change survived.
    assert _rows(store) == before
    unit = store.get_unit(RUN, "u0")
    assert (unit.state, unit.claimed_by) == (UnitState.CLAIMED, "w")


def test_waiting_unit_holds_no_lease_and_keeps_its_token(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    record = store.request_interrupt(
        RUN, "u0", token, _request(), usage=UsageRecord(5, 6, None), at=1002.0
    )
    unit = store.get_unit(RUN, "u0")
    assert unit.state is UnitState.WAITING
    assert (unit.claimed_by, unit.claimed_at, unit.lease_expires_at) == (None, None, None)
    assert unit.fencing_token == 1
    assert unit.updated_at == 1002.0
    assert (record.fencing_token, record.created_at, record.expires_at) == (1, 1002.0, None)
    assert record.resolution is None
    row = store.list_attempts(RUN, "u0")[-1]
    assert (row.kind, row.worker_id, row.fencing_token, row.at) == (
        AttemptKind.INTERRUPT_REQUESTED,
        "w",
        1,
        1002.0,
    )
    assert row.usage == UsageRecord(5, 6, None)
    assert row.error is None


def test_request_under_halt_is_recorded(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.set_halt(RUN, "rate_limit", "slow down", at=1001.5)
    record = store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    assert record.id == "1"
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING
    assert store.get_run(RUN).halted_kind == "rate_limit"


def test_request_refused_under_an_open_dispatch(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.record_dispatch(RUN, "u0", "w", session_id="s1", at=1001.5)
    before = _rows(store)
    with pytest.raises(WaitUnderDispatchError) as info:
        store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    assert "has an open dispatch" in str(info.value)
    assert _rows(store) == before
    assert [d.unit_id for d in store.open_dispatches(RUN)] == ["u0"]
    assert store.get_unit(RUN, "u0").state is UnitState.CLAIMED
    # The refusal is about the OPEN dispatch: once it settles, the unit may wait.
    store.settle_dispatch(RUN, "u0", outcome="blocked", at=1003.0)
    assert store.request_interrupt(RUN, "u0", token, _request(), at=1004.0).id == "1"


@pytest.mark.parametrize("verb", ["renew", "accept", "fail"])
def test_waiting_unit_refuses_renew_accept_fail(tmp_path, verb):
    store = _store(tmp_path)
    record = _wait(store)
    before = _rows(store)
    with pytest.raises(NotClaimedError) as info:
        if verb == "renew":
            store.renew_lease(RUN, "u0", record.fencing_token, at=1003.0)
        elif verb == "accept":
            store.accept_unit(RUN, "u0", record.fencing_token, text="T", at=1003.0)
        else:
            store.fail_unit(RUN, "u0", record.fencing_token, error="x", at=1003.0)
    assert "is waiting, not claimed" in str(info.value)
    assert _rows(store) == before


def test_second_open_interrupt_is_refused_by_the_trigger(tmp_path):
    store = _store(tmp_path)
    _wait(store)
    with pytest.raises(sqlite3.IntegrityError, match="unit already has an unresolved interrupt"):
        with _sql(store) as conn:
            _insert_interrupt(conn, unit="u0", token=7)
    # Another unit is not affected by u0's open request.
    with _sql(store) as conn:
        _insert_interrupt(conn, unit="u1", token=7)
    assert [r.unit_id for r in store.list_interrupts(RUN)] == ["u0", "u1"]


def test_one_request_per_claim_is_enforced(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    # The first request is resolved, so the one-open trigger does not fire;
    # the same claim (token 1) still cannot make a second request.
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        with _sql(store) as conn:
            _insert_interrupt(conn, unit="u0", token=1)
    with _sql(store) as conn:
        _insert_interrupt(conn, unit="u0", token=2)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE interrupts SET kind = 'other' WHERE id = 1",
        "UPDATE interrupts SET on_rejected = 'release' WHERE id = 1",
        "DELETE FROM interrupts WHERE id = 1",
    ],
    ids=["update", "update_policy", "delete"],
)
def test_interrupt_rows_are_immutable(tmp_path, statement):
    store = _store(tmp_path)
    _wait(store)
    before = _rows(store)
    with pytest.raises(sqlite3.IntegrityError, match="interrupt records are immutable"):
        with _sql(store) as conn:
            conn.execute(statement)
    assert _rows(store) == before


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE interrupt_resolutions SET outcome = 'rejected' WHERE interrupt_id = 1",
        "DELETE FROM interrupt_resolutions WHERE interrupt_id = 1",
    ],
    ids=["update", "delete"],
)
def test_resolution_rows_are_immutable(tmp_path, statement):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    before = _rows(store)
    with pytest.raises(sqlite3.IntegrityError, match="interrupt resolutions are immutable"):
        with _sql(store) as conn:
            conn.execute(statement)
    assert _rows(store) == before


@pytest.mark.parametrize("case", ["primary_key", "check", "fk"])
def test_resolution_constraints(tmp_path, case):
    store = _store(tmp_path)
    record = _wait(store)
    insert = (
        "INSERT INTO interrupt_resolutions(interrupt_id, outcome, input_json, reason, "
        "resolved_at) VALUES (?, ?, NULL, NULL, 5.0)"
    )
    if case == "primary_key":
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
        args, match = (1, "rejected"), "UNIQUE"
    elif case == "check":
        args, match = (1, "maybe"), "CHECK"
    else:
        args, match = (999, "answered"), "FOREIGN KEY"
    with pytest.raises(sqlite3.IntegrityError, match=match):
        with _sql(store) as conn:
            conn.execute(insert, args)


@pytest.mark.parametrize("column", ["on_rejected", "on_expired"])
def test_policy_check_is_enforced(tmp_path, column):
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with _sql(store) as conn:
            _insert_interrupt(conn, **{column: "maybe"})
    assert store.list_interrupts(RUN) == []


def test_interrupt_fk_to_unit_is_enforced(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        with _sql(store) as conn:
            _insert_interrupt(conn, unit="no-such-unit")


@pytest.mark.parametrize("name", ["on_rejected", "on_expired"])
def test_request_refuses_an_unknown_policy(tmp_path, monkeypatch, name):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    before = _rows(store)

    def no_transaction(self):
        raise AssertionError("a transaction was opened for a refused policy")

    monkeypatch.setattr(ExecutionStore, "_writer", no_transaction)
    with pytest.raises(InterruptRequestError) as info:
        store.request_interrupt(RUN, "u0", token, _request(), at=1002.0, **{name: "maybe"})
    assert str(info.value) == f"{name} must be one of stop, release, got 'maybe'"
    assert isinstance(info.value, ValueError)
    assert _rows(store) == before


def test_default_policies_are_stop(tmp_path):
    parameters = inspect.signature(ExecutionStore.request_interrupt).parameters
    assert (parameters["on_rejected"].default, parameters["on_expired"].default) == ("stop", "stop")
    signal = InterruptRequested(_request())
    assert (signal.on_rejected, signal.on_expired, signal.usage) == ("stop", "stop", None)

    store = _store(tmp_path)
    record = _wait(store, request=_request(expires_in_s=60))
    assert (record.on_rejected, record.on_expired) == ("stop", "stop")
    with _sql(store) as conn:
        row = conn.execute("SELECT on_rejected, on_expired FROM interrupts").fetchone()
    assert tuple(row) == ("stop", "stop")
    # A consumer that names no policy gets a unit that does not proceed.
    store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
    assert store.get_unit(RUN, "u0").state is UnitState.OPERATOR_REJECTED


def test_request_is_checked_by_the_shared_contract(tmp_path, monkeypatch):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    before = _rows(store)
    seen = {}

    def sentinel(**kwargs):
        seen.update(kwargs)
        raise contract.RequestError("sentinel from the shared contract")

    monkeypatch.setattr(contract, "check_request", sentinel)
    with pytest.raises(InterruptRequestError, match="sentinel from the shared contract"):
        store.request_interrupt(RUN, "u0", token, _request(), at=1002.0)
    assert seen["owner"] == "content-pipeline-kit"
    assert seen["accepted_envelopes"] == frozenset({"plugins-kit.interrupt-request/v1"})
    assert seen["envelope"] == "plugins-kit.interrupt-request/v1"
    assert seen["validator"].__name__ == "llm_scripting_kit.completion.json_schema"
    assert _rows(store) == before


def test_request_schema_outside_subset_is_refused(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    before = _rows(store)
    request = _request(request_schema={"type": "object", "patternProperties": {}})
    with pytest.raises(InterruptRequestError) as info:
        store.request_interrupt(RUN, "u0", token, request, at=1002.0)
    assert "outside the supported JSON Schema subset" in str(info.value)
    assert "patternProperties" in str(info.value)
    assert _rows(store) == before
    assert store.get_unit(RUN, "u0").state is UnitState.CLAIMED


def test_request_refuses_the_envelope_of_another_store(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    with pytest.raises(InterruptRequestError) as info:
        store.request_interrupt(
            RUN, "u0", token, _request(envelope="job-kit.interrupt-request/v1"), at=1002.0
        )
    assert str(info.value) == (
        "interrupt request schema 'job-kit.interrupt-request/v1' is not accepted; "
        "this content-pipeline-kit accepts 'plugins-kit.interrupt-request/v1'"
    )


def test_request_stores_canonical_copies_and_the_expiry(tmp_path):
    store = _store(tmp_path)
    token = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    request = _request(payload={"b": 2, "a": [1, {"z": 0, "y": 1}]}, expires_in_s=60)
    record = store.request_interrupt(RUN, "u0", token, request, at=1002.0)
    with _sql(store) as conn:
        row = dict(conn.execute("SELECT * FROM interrupts").fetchone())
    assert row == {
        "id": 1,
        "run_id": "r1",
        "unit_id": "u0",
        "fencing_token": 1,
        "envelope": "plugins-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema_json": (
            '{"additionalProperties":false,"properties":{"approved":{"type":"boolean"}},'
            '"required":["approved"],"type":"object"}'
        ),
        "payload_json": '{"a":[1,{"y":1,"z":0}],"b":2}',
        "created_at": 1002.0,
        "expires_at": 1062.0,
        "on_rejected": "stop",
        "on_expired": "stop",
    }
    assert record.payload == {"a": [1, {"y": 1, "z": 0}], "b": 2}
    assert record.request_schema == APPROVAL
    assert record.expires_at == 1062.0
    assert store.get_interrupt(RUN, "1") == record
    assert store.open_interrupt(RUN, "u0") == record
    assert store.list_interrupts(RUN, "u0") == [record]
    assert store.list_interrupts(RUN, "u1") == []
    assert store.get_interrupt(RUN, "2") is None
    assert store.get_interrupt("other", "1") is None
    assert store.open_interrupt(RUN, "u1") is None


# -- resolve_interrupt ---------------------------------------------------------


@pytest.mark.parametrize("case", ["run", "id", "other_run"])
def test_resolve_unknown_interrupt(tmp_path, case):
    store = _store(tmp_path)
    _wait(store)
    store.create_run("r2", driver="inline", backend="mock", model="m1", adapter_version="7")
    before = _rows(store)
    targets = {
        "run": [("no-such-run", "1")],
        "id": [(RUN, "999"), (RUN, "abc"), (RUN, "-1"), (RUN, "")],
        "other_run": [("r2", "1")],
    }[case]
    for run_id, interrupt_id in targets:
        with pytest.raises(UnknownInterruptError):
            store.resolve_interrupt(
                run_id, interrupt_id, decision="answer", input={"approved": True}, now=1003.0
            )
    assert _rows(store) == before


def test_resolve_refuses_input_failing_the_request_schema(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    before = _rows(store)
    with pytest.raises(ResolutionInputError) as info:
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"approved": "yes", "extra": 1}, now=1003.0
        )
    assert info.value.errors == (("/approved", "type"), ("/extra", "additionalProperties"))
    assert str(info.value) == "resolution input does not satisfy the request schema"
    assert _rows(store) == before
    assert store.get_unit(RUN, "u0").state is UnitState.WAITING


@pytest.mark.parametrize("case", ["oversized", "non_native"])
def test_resolve_refuses_unusable_input(tmp_path, case):
    store = _store(tmp_path)
    record = _wait(store, request=_request(request_schema={"type": "object"}))
    before = _rows(store)
    value = {"blob": "x" * 65536} if case == "oversized" else {"when": {1, 2}}
    with pytest.raises(ResolutionInputError) as info:
        store.resolve_interrupt(RUN, record.id, decision="answer", input=value, now=1003.0)
    assert info.value.errors == ()
    if case == "oversized":
        assert str(info.value) == (
            "resolution input is 65547 bytes as canonical JSON; the cap is 65536"
        )
    else:
        assert str(info.value) == "resolution input is not JSON-native: /when: set is not a JSON value"
    assert _rows(store) == before


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"decision": "approve"}, "decision must be one of answer, reject, got 'approve'"),
        ({"decision": "reject", "input": {}}, "a rejection carries a reason, not an input"),
        (
            {"decision": "answer", "input": {}, "reason": "x"},
            "an answer carries an input, not a reason",
        ),
    ],
    ids=["unknown", "reject_with_input", "answer_with_reason"],
)
def test_resolve_refuses_a_bad_decision(tmp_path, kwargs, message):
    store = _store(tmp_path)
    record = _wait(store)
    before = _rows(store)
    with pytest.raises(ValueError) as info:
        store.resolve_interrupt(RUN, record.id, now=1003.0, **kwargs)
    assert str(info.value) == message
    assert _rows(store) == before


def test_answer_returns_unit_to_pending(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    resolution = store.resolve_interrupt(
        RUN, record.id, decision="answer", input={"approved": True}, now=1003.0
    )
    assert (resolution.interrupt_id, resolution.outcome, resolution.resolved_at) == (
        "1",
        "answered",
        1003.0,
    )
    assert (resolution.input, resolution.reason, resolution.replayed) == (
        {"approved": True},
        None,
        False,
    )
    unit = store.get_unit(RUN, "u0")
    assert unit.state is UnitState.PENDING
    assert (unit.claimed_by, unit.lease_expires_at, unit.failed_at) == (None, None, None)
    assert (unit.fencing_token, unit.updated_at) == (1, 1003.0)
    assert store.open_interrupt(RUN, "u0") is None
    assert store.get_interrupt(RUN, "1").resolution == resolution
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u0", "u1", "u2"]


def test_claim_after_resolution_is_a_fresh_attempt(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    claim = store.claim_unit(RUN, "u0", "w2", at=1004.0)
    assert claim.fencing_token == 2
    rows = store.list_attempts(RUN, "u0")
    assert [(r.kind.value, r.fencing_token) for r in rows] == [
        ("claim", 1),
        ("interrupt_requested", 1),
        ("interrupt_resolved", 1),
        ("claim", 2),
    ]
    store.accept_unit(RUN, "u0", claim.fencing_token, text="with the answer", at=1005.0)
    assert store.get_unit(RUN, "u0").state is UnitState.ACCEPTED


def test_reject_under_stop_ends_unit_operator_rejected(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, on_rejected="stop")
    resolution = store.resolve_interrupt(
        RUN, record.id, decision="reject", reason="not now", now=1003.0
    )
    assert (resolution.outcome, resolution.reason, resolution.input) == ("rejected", "not now", None)
    unit = store.get_unit(RUN, "u0")
    assert unit.state is UnitState.OPERATOR_REJECTED
    assert (unit.failed_at, unit.updated_at, unit.claimed_by) == (1003.0, 1003.0, None)
    with pytest.raises(TerminalStateError, match="already operator_rejected"):
        store.claim_unit(RUN, "u0", "w2", at=1004.0)
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u1", "u2"]


def test_reject_under_release_returns_unit_to_pending(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, on_rejected="release")
    store.resolve_interrupt(RUN, record.id, decision="reject", reason="not now", now=1003.0)
    unit = store.get_unit(RUN, "u0")
    assert unit.state is UnitState.PENDING
    assert (unit.failed_at, unit.claimed_by) == (None, None)
    assert store.claim_unit(RUN, "u0", "w2", at=1004.0).fencing_token == 2


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_outcome_row_is_written_under_both_policies(tmp_path, outcome):
    store = _store(tmp_path)
    request = _request(expires_in_s=60)
    records = {
        "stop": _wait(store, "u0", request=request, on_rejected="stop", on_expired="stop"),
        "release": _wait(
            store, "u1", request=request, on_rejected="release", on_expired="release"
        ),
    }
    for record in records.values():
        if outcome == "rejected":
            store.resolve_interrupt(RUN, record.id, decision="reject", reason="no", now=1003.0)
    if outcome == "expired":
        store.expire_interrupts(RUN, now=2000.0)
    with _sql(store) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT interrupt_id, outcome FROM interrupt_resolutions ORDER BY interrupt_id"
        )]
    assert rows == [(1, outcome), (2, outcome)]
    states = [store.get_unit(RUN, unit).state.value for unit in ("u0", "u1")]
    stopped = "operator_rejected" if outcome == "rejected" else "interrupt_expired"
    assert states == [stopped, "pending"]


@pytest.mark.parametrize("closing", ["resolved", "rejected", "expired"])
def test_closing_attempt_rows_carry_the_owning_token(tmp_path, closing):
    store = _store(tmp_path)
    # Token 2 owns the request: the unit was claimed, failed and claimed again.
    first = store.claim_unit(RUN, "u0", "w", at=1001.0).fencing_token
    store.fail_unit(RUN, "u0", first, error="again", at=1001.5)
    token = store.claim_unit(RUN, "u0", "w", at=1002.0).fencing_token
    assert token == 2
    record = store.request_interrupt(
        RUN, "u0", token, _request(expires_in_s=60), at=1003.0, on_rejected="release"
    )
    if closing == "resolved":
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"approved": False}, now=1004.0
        )
    elif closing == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1004.0)
    else:
        store.expire_interrupts(RUN, now=1063.0)
    row = store.list_attempts(RUN, "u0")[-1]
    assert row.kind.value == f"interrupt_{closing}"
    assert row.fencing_token == 2 == record.fencing_token
    assert (row.worker_id, row.error, row.usage) == (None, None, None)


def test_identical_resolution_replays_without_rows(tmp_path):
    store = _store(tmp_path)
    answered = _wait(store, "u0")
    rejected = _wait(store, "u1")
    first = store.resolve_interrupt(
        RUN, answered.id, decision="answer", input={"approved": True}, now=1003.0
    )
    store.resolve_interrupt(RUN, rejected.id, decision="reject", reason="no", now=1003.0)
    before = _rows(store)

    replay = store.resolve_interrupt(
        RUN, answered.id, decision="answer", input={"approved": True}, now=5000.0
    )
    assert replay.replayed is True
    assert replay == first
    assert replay.resolved_at == 1003.0
    again = store.resolve_interrupt(RUN, rejected.id, decision="reject", reason="no", now=5000.0)
    assert (again.replayed, again.outcome, again.reason) == (True, "rejected", "no")
    assert _rows(store) == before


def test_replay_ignores_key_order(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, request=_request(request_schema={"type": "object"}))
    store.resolve_interrupt(
        RUN, record.id, decision="answer", input={"a": 1, "b": {"c": 2, "d": 3}}, now=1003.0
    )
    before = _rows(store)
    replay = store.resolve_interrupt(
        RUN, record.id, decision="answer", input={"b": {"d": 3, "c": 2}, "a": 1}, now=1004.0
    )
    assert replay.replayed is True
    assert _rows(store) == before


def test_replay_after_the_unit_was_reclaimed(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    token = store.claim_unit(RUN, "u0", "w2", at=1004.0).fencing_token
    store.accept_unit(RUN, "u0", token, at=1005.0)
    before = _rows(store)
    replay = store.resolve_interrupt(
        RUN, record.id, decision="answer", input={"approved": True}, now=1006.0
    )
    assert (replay.replayed, replay.outcome) == (True, "answered")
    assert _rows(store) == before
    assert store.get_unit(RUN, "u0").state is UnitState.ACCEPTED


def test_conflicting_resolution_is_refused(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    before = _rows(store)
    with pytest.raises(ResolutionConflictError) as info:
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"approved": False}, now=1004.0
        )
    assert info.value.stored_outcome == "answered"
    assert str(info.value) == (
        "interrupt 1 is already resolved (answered) with a different decision; "
        "the original resolution is kept"
    )
    with pytest.raises(ResolutionConflictError):
        store.resolve_interrupt(RUN, record.id, decision="reject", reason="no", now=1004.0)
    assert _rows(store) == before
    assert store.get_interrupt(RUN, "1").resolution.input == {"approved": True}


def test_resolve_refuses_when_unit_is_not_waiting(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    # Break the invariant by hand: an unresolved interrupt whose unit moved on.
    with _sql(store) as conn:
        conn.execute("UPDATE units SET state = 'pending' WHERE unit_id = 'u0'")
    before = _rows(store)
    with pytest.raises(ExecutionError) as info:
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"approved": True}, now=1003.0
        )
    assert type(info.value) is ExecutionError
    assert str(info.value) == (
        "interrupt 1 is unresolved but its unit 'r1'/'u0' is pending, not waiting; "
        "nothing was written"
    )
    assert _rows(store) == before


@pytest.mark.parametrize("policy", ["stop", "release"])
def test_resolve_after_lapse_records_expiry_then_raises(tmp_path, policy):
    store = _store(tmp_path)
    record = _wait(store, request=_request(expires_in_s=60), on_expired=policy)
    assert record.expires_at == 1062.0
    # Inclusive: the request lapses AT its expires_at.
    with pytest.raises(InterruptExpiredError) as info:
        store.resolve_interrupt(
            RUN, record.id, decision="answer", input={"approved": True}, now=1062.0
        )
    assert (info.value.interrupt_id, info.value.expires_at) == ("1", 1062.0)
    # The expiry is durable: a second store object reads it.
    reread = ExecutionStore(store.db_path)
    resolution = reread.get_interrupt(RUN, "1").resolution
    assert (resolution.outcome, resolution.resolved_at, resolution.input) == (
        "expired",
        1062.0,
        None,
    )
    unit = reread.get_unit(RUN, "u0")
    if policy == "stop":
        assert (unit.state, unit.failed_at) == (UnitState.INTERRUPT_EXPIRED, 1062.0)
    else:
        assert (unit.state, unit.failed_at) == (UnitState.PENDING, None)
    assert _kinds(reread) == ["claim", "interrupt_requested", "interrupt_expired"]
    # A later resolve is refused again and writes nothing.
    before = _rows(store)
    with pytest.raises(InterruptExpiredError):
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1063.0)
    assert _rows(store) == before


# -- expire_interrupts -----------------------------------------------------------


@pytest.mark.parametrize("case", ["at_expires_at", "before", "twice"])
def test_expire_interrupts(tmp_path, case):
    store = _store(tmp_path)
    record = _wait(store, request=_request(expires_in_s=60))
    assert record.expires_at == 1062.0
    if case == "before":
        before = _rows(store)
        assert store.expire_interrupts(RUN, now=1061.999) == []
        assert _rows(store) == before
        assert store.get_unit(RUN, "u0").state is UnitState.WAITING
        return
    expired = store.expire_interrupts(RUN, now=1062.0)
    assert [r.id for r in expired] == ["1"]
    assert (expired[0].resolution.outcome, expired[0].resolution.resolved_at) == ("expired", 1062.0)
    assert store.get_unit(RUN, "u0").state is UnitState.INTERRUPT_EXPIRED
    if case == "twice":
        before = _rows(store)
        assert store.expire_interrupts(RUN, now=5000.0) == []
        assert _rows(store) == before


def test_expire_interrupts_of_an_unknown_run_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(UnknownRunError):
        store.expire_interrupts("no-such-run", now=5000.0)
    assert store.list_interrupts("no-such-run") == []


@pytest.mark.parametrize("policy", ["stop", "release"])
def test_expire_interrupts_applies_on_expired(tmp_path, policy):
    store = _store(tmp_path)
    _wait(store, request=_request(expires_in_s=60), on_expired=policy)
    store.expire_interrupts(RUN, now=1100.0)
    unit = store.get_unit(RUN, "u0")
    if policy == "stop":
        assert (unit.state, unit.failed_at) == (UnitState.INTERRUPT_EXPIRED, 1100.0)
        with pytest.raises(TerminalStateError):
            store.claim_unit(RUN, "u0", "w2", at=1101.0)
    else:
        assert (unit.state, unit.failed_at) == (UnitState.PENDING, None)
        assert store.claim_unit(RUN, "u0", "w2", at=1101.0).fencing_token == 2


def test_expire_interrupts_skips_non_waiting_units(tmp_path):
    store = _store(tmp_path, units=("u0", "u1", "u2", "u3", "u4"))
    lapsing = _request(expires_in_s=60)
    _wait(store, "u0")  # no expiry: never lapses
    answered = _wait(store, "u1", request=lapsing)
    store.resolve_interrupt(RUN, answered.id, decision="answer", input={"approved": True}, now=1003.0)
    _wait(store, "u2", request=lapsing)  # lapsed, but its unit is forced out of WAITING
    with _sql(store) as conn:
        conn.execute("UPDATE units SET state = 'claimed' WHERE unit_id = 'u2'")
    store.claim_unit(RUN, "u3", "w", at=1001.0)  # claimed, never asked
    _wait(store, "u4", request=lapsing)  # the only one to expire
    untouched = {u.unit_id: u for u in store.list_units(RUN) if u.unit_id != "u4"}

    expired = store.expire_interrupts(RUN, now=5000.0)
    assert [(r.id, r.unit_id) for r in expired] == [("4", "u4")]
    assert {u.unit_id: u for u in store.list_units(RUN) if u.unit_id != "u4"} == untouched
    with _sql(store) as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT interrupt_id, outcome FROM interrupt_resolutions ORDER BY interrupt_id"
        )]
    assert rows == [(2, "answered"), (4, "expired")]


def test_prepare_run_and_ready_wave_record_no_expiry(tmp_path):
    store = _store(tmp_path)
    _wait(store, "u0", request=_request(expires_in_s=60))
    before = _rows(store)
    work_units = [WorkUnit(id=u) for u in ("u0", "u1", "u2")]
    # Long past the request's expiry. None of these may record the lapse.
    wave = prepare_run(store, RUN, FLAT, work_units, at=9000.0, reclaim_at=9000.0)
    assert [u.unit_id for u in wave] == ["u1", "u2"]
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u1", "u2"]
    assert ready_wave(store, RUN, GRAPH, reclaim_at=9000.0) == []
    assert [u.unit_id for u in unfinished_units(store, RUN)] == ["u0", "u1", "u2"]
    compute_status(store, RUN, now=9000.0)
    assert _rows(store) == before
    record = store.open_interrupt(RUN, "u0")
    assert record.resolution is None
    assert record.lapsed(9000.0) is True  # reported by the read, not recorded


# -- waves, gates and helpers ------------------------------------------------------


def test_flat_wave_omits_waiting_units(tmp_path):
    store = _store(tmp_path)
    _wait(store, "u1")
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u0", "u2"]
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT, max_wave_size=1)] == ["u0"]


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_released_unit_is_in_the_next_wave(tmp_path, outcome):
    store = _store(tmp_path)
    record = _wait(
        store, "u1", request=_request(expires_in_s=60), on_rejected="release", on_expired="release"
    )
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u0", "u2"]
    if outcome == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
    else:
        store.expire_interrupts(RUN, now=2000.0)
    assert [u.unit_id for u in ready_wave(store, RUN, FLAT)] == ["u0", "u1", "u2"]
    assert unit_resolutions(store, RUN, "u1")[0]["outcome"] == outcome


def test_gate_skips_a_released_unit_by_its_outcome(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, "u0", on_rejected="release")
    store.resolve_interrupt(RUN, record.id, decision="reject", reason="not needed", now=1003.0)

    def rejected(work_unit):
        seen = unit_resolutions(store, RUN, work_unit.id)
        return "operator rejected" if seen and seen[-1]["outcome"] == "rejected" else None

    wave = prepare_run(
        store,
        RUN,
        FLAT,
        [WorkUnit(id=u) for u in ("u0", "u1", "u2")],
        gates=[Gate(name="operator", predicate=rejected)],
        at=1004.0,
    )
    assert [u.unit_id for u in wave] == ["u1", "u2"]
    assert store.get_unit(RUN, "u0").state is UnitState.SKIPPED
    assert store.list_attempts(RUN, "u0")[-1].error == "skip:gate:operator:operator rejected"


def test_graph_wave_is_empty_behind_a_waiting_predecessor(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, "u0")
    assert ready_wave(store, RUN, GRAPH) == []
    assert ready_wave(store, RUN, GRAPH, reclaim_at=9000.0) == []
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    assert [u.unit_id for u in ready_wave(store, RUN, GRAPH)] == ["u0"]


def test_graph_block_reason_names_the_wait(tmp_path):
    store = _store(tmp_path)
    _wait(store, "u0")
    assert graph_block_reason(store, RUN, GRAPH) == (
        "unit 'u1' is blocked: predecessor 'u0' is waiting on an open interrupt; "
        "nothing behind it is released until the interrupt is resolved"
    )


@pytest.mark.parametrize("outcome", ["rejected", "expired"])
def test_graph_block_reason_is_permanent_after_a_stop(tmp_path, outcome):
    store = _store(tmp_path)
    record = _wait(store, "u0", request=_request(expires_in_s=60))
    if outcome == "rejected":
        store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
        state = "OPERATOR_REJECTED"
    else:
        store.expire_interrupts(RUN, now=2000.0)
        state = "INTERRUPT_EXPIRED"
    assert graph_block_reason(store, RUN, GRAPH) == (
        f"unit 'u1' is blocked: predecessor 'u0' is terminally {state} "
        "(its interrupt closed under the stop policy), which permanently blocks the chain"
    )
    assert ready_wave(store, RUN, GRAPH) == []


def test_unfinished_and_waiting_units(tmp_path):
    store = _store(tmp_path)
    record = _wait(store, "u0")
    token = store.claim_unit(RUN, "u1", "w", at=1001.0).fencing_token
    store.accept_unit(RUN, "u1", token, at=1002.0)
    assert [u.unit_id for u in unfinished_units(store, RUN)] == ["u0", "u2"]
    assert [u.unit_id for u in waiting_units(store, RUN)] == ["u0"]
    store.resolve_interrupt(RUN, record.id, decision="reject", now=1003.0)
    assert [u.unit_id for u in unfinished_units(store, RUN)] == ["u2"]
    assert waiting_units(store, RUN) == []


def test_unit_resolutions_lists_every_outcome_oldest_first(tmp_path):
    store = _store(tmp_path)
    policies = {"on_rejected": "release", "on_expired": "release"}
    first = _wait(store, "u0", at=1001.0, **policies)
    store.resolve_interrupt(RUN, first.id, decision="reject", reason="not yet", now=1003.0)
    second = _wait(store, "u0", at=1004.0, request=_request(expires_in_s=10), **policies)
    store.expire_interrupts(RUN, now=1100.0)
    third = _wait(store, "u0", at=1101.0, request=_request(payload={"question": "now?"}), **policies)
    store.resolve_interrupt(RUN, third.id, decision="answer", input={"approved": True}, now=1103.0)
    fourth = _wait(store, "u0", at=1104.0)  # open: not a resolution yet
    _wait(store, "u1", at=1001.0)  # another unit's interrupt is not listed
    assert [first.id, second.id, third.id, fourth.id] == ["1", "2", "3", "4"]
    assert unit_resolutions(store, RUN, "u0") == [
        {
            "interrupt_id": "1",
            "kind": "approval",
            "outcome": "rejected",
            "input": None,
            "reason": "not yet",
            "payload": {"question": "ship it?"},
        },
        {
            "interrupt_id": "2",
            "kind": "approval",
            "outcome": "expired",
            "input": None,
            "reason": None,
            "payload": {"question": "ship it?"},
        },
        {
            "interrupt_id": "3",
            "kind": "approval",
            "outcome": "answered",
            "input": {"approved": True},
            "reason": None,
            "payload": {"question": "now?"},
        },
    ]
    assert unit_resolutions(store, RUN, "u1") == []
    assert store.get_unit(RUN, "u0").fencing_token == 4


# -- the resolution document ---------------------------------------------------


def test_resolution_document_is_byte_identical_across_calls(tmp_path):
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=1003.0)
    expected = (
        '{"input":{"approved":true},"interrupt_id":"1","kind":"approval",'
        '"outcome":"answered","payload":{"question":"ship it?"},'
        '"resolved_at":"1970-01-01T00:16:43Z",'
        '"schema":"plugins-kit.interrupt-resolution/v1"}'
    )
    assert interrupts.resolution_document(store.get_interrupt(RUN, "1")) == expected
    # A replay and a second store object change nothing in the document.
    store.resolve_interrupt(RUN, record.id, decision="answer", input={"approved": True}, now=9000.0)
    reread = ExecutionStore(store.db_path).list_interrupts(RUN)[0]
    assert interrupts.resolution_document(reread) == expected


def test_resolution_document_names_the_shared_literal(tmp_path, monkeypatch):
    # The adapter takes the record and nothing else: a caller cannot name an
    # envelope.
    assert list(inspect.signature(interrupts.resolution_document).parameters) == ["record"]
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt(RUN, record.id, decision="reject", reason="no", now=1003.0)
    passed = {}
    real = contract.resolution_document

    def spy(**kwargs):
        passed.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(contract, "resolution_document", spy)
    document = json.loads(interrupts.resolution_document(store.get_interrupt(RUN, "1")))
    assert passed["resolution_envelope"] == "plugins-kit.interrupt-resolution/v1"
    assert document["schema"] == "plugins-kit.interrupt-resolution/v1"
    assert sorted(document) == [
        "input",
        "interrupt_id",
        "kind",
        "outcome",
        "payload",
        "resolved_at",
        "schema",
    ]
    with pytest.raises(ValueError, match="interrupt 2 has no resolution"):
        interrupts.resolution_document(_wait(store, "u1"))
