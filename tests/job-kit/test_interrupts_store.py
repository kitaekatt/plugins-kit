"""Tests for durable interrupts in the job-kit ledger (schema 12): the waiting
state, interrupt and resolution records, resolution idempotency, expiry,
continuations, the database constraints, and the interrupt events."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

import job_kit.events as events
from job_kit.interrupts import REQUEST_ENVELOPE_V1, JsonSchemaSupportError
from job_kit.model import (
    Acceptance,
    Attempt,
    InterruptRecord,
    InterruptRequest,
    Contract,
    Job,
    JobState,
    Prompt,
    RunState,
    TERMINAL_STATES,
    Usage,
)
from job_kit.store import (
    InterruptExpiredError,
    JobStore,
    ResolutionConflictError,
    ResolutionInputError,
    StoreError,
    UnknownInterruptError,
)

from bootstrap_lib import execution_event as real_module


ARMED_AT = "2026-09-01T00:00:01Z"
ENDED_AT = "2026-09-01T00:00:02Z"
CREATED = 1000.0
APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
    "additionalProperties": False,
}


def _job(directory: Path, job_id: str = "job", *, max_attempts: int = 2) -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=max_attempts,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _acceptance(outcome: str = "interrupt_requested", exit_code: Optional[int] = 0) -> Acceptance:
    return Acceptance(
        command=("contract",),
        directory=Path.cwd(),
        exit_code=exit_code,
        stdout="",
        stderr="",
        wall_ms=1,
        accepted=False,
        outcome=outcome,
    )


def _attempt(
    run_id: str,
    job_id: str,
    attempt_no: int,
    *,
    acceptance: Optional[Acceptance] = None,
    usage: Optional[Usage] = Usage(input_tokens=3, output_tokens=5),
) -> Attempt:
    return Attempt(
        run_id=run_id,
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        status="completed",
        started_at=ARMED_AT,
        ended_at=ENDED_AT,
        usage=usage,
        response_text="the model answer",
        acceptance=acceptance if acceptance is not None else _acceptance(),
    )


def _request(
    *,
    kind: str = "approval",
    schema: Optional[dict] = None,
    payload: Optional[dict] = None,
    expires_in_s: Optional[int] = None,
) -> InterruptRequest:
    return InterruptRequest(
        envelope=REQUEST_ENVELOPE_V1,
        kind=kind,
        request_schema=dict(APPROVAL if schema is None else schema),
        payload=dict({"action": "push tag"} if payload is None else payload),
        expires_in_s=expires_in_s,
    )


def _reserve(store: JobStore, run_id: str, job_id: str, *, arm: bool = True) -> int:
    reservation = store.reserve_attempt(
        run_id,
        job_id,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    if arm:
        store.arm_reservation(
            run_id, job_id, reservation.attempt_no, invoke_armed_at=ARMED_AT
        )
    return reservation.attempt_no


def _wait(
    store: JobStore,
    run_id: str = "run",
    job_id: str = "job",
    *,
    at: float = CREATED,
    **request: Any,
) -> InterruptRecord:
    """Take one attempt of a pending job to waiting on a fresh interrupt."""
    attempt_no = _reserve(store, run_id, job_id)
    store.append_attempt(
        _attempt(run_id, job_id, attempt_no), interrupt=_request(**request), at=at
    )
    record = store.open_interrupt(run_id, job_id)
    assert record is not None
    return record


def _store(tmp_path: Path, *jobs: Job) -> JobStore:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", list(jobs) or [_job(tmp_path)])
    return store


def _count(store: JobStore, table: str) -> int:
    with sqlite3.connect(str(store.db_path)) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _raw(store: JobStore) -> sqlite3.Connection:
    connection = sqlite3.connect(str(store.db_path))
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _set_state(store: JobStore, job_id: str, state: str, run_id: str = "run") -> None:
    with sqlite3.connect(str(store.db_path)) as connection:
        connection.execute(
            "UPDATE jobs SET state = ? WHERE run_id = ? AND id = ?",
            (state, run_id, job_id),
        )


def _of(stream: tuple[dict, ...], event: Optional[str] = None, **identity: str) -> list[dict]:
    return [
        item
        for item in stream
        if (event is None or item["event"] == event)
        and all(item["identity"].get(key) == value for key, value in identity.items())
    ]


def _names(stream: list[dict]) -> list[str]:
    return [item["event"] for item in stream]


def _answer_and_begin(store: JobStore, record: InterruptRecord, job_id: str = "job"):
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    return store.begin_continuation("run", job_id, now=CREATED + 2)


# --------------------------------------------------------------------------
# The waiting state
# --------------------------------------------------------------------------


def test_waiting_is_not_terminal_and_emits_no_terminal(tmp_path: Path) -> None:
    assert JobState.WAITING not in TERMINAL_STATES
    assert {JobState.OPERATOR_REJECTED, JobState.EXPIRED} <= TERMINAL_STATES
    store = _store(tmp_path)
    record = _wait(store)
    job = store.get_job("run", "job")
    assert job.state is JobState.WAITING and not job.terminal
    stream = store.list_events("run")
    assert _of(stream, "terminal") == []
    attempt_events = _names(_of(stream, unit_id="job", attempt_id="1"))
    assert attempt_events == [
        "dispatch-selected",
        "call-started",
        "usage",
        "result",
        "interrupt",
    ]
    assert _of(stream, "result")[0]["payload"]["acceptance"] == "interrupt_requested"
    assert record.continuation_no == 0 and record.attempt_no == 1


def test_interrupt_requested_is_never_accepted() -> None:
    requested = _acceptance()
    assert requested.exit_code == 0 and requested.accepted is False
    assert _acceptance(outcome="observed").accepted is True


def test_interrupt_request_commits_with_its_attempt_atomically(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store, "run", "job")
    before = _count(store, "events")

    def failing_insert(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("interrupt insert failed")

    monkeypatch.setattr(JobStore, "_insert_interrupt", staticmethod(failing_insert))
    with pytest.raises(RuntimeError, match="interrupt insert failed"):
        store.append_attempt(_attempt("run", "job", attempt_no), interrupt=_request())
    monkeypatch.undo()
    assert store.list_attempts("run") == []
    assert store.get_reservation("run", "job", attempt_no).disposition is None
    assert store.get_job("run", "job").state is JobState.RUNNING
    assert _count(store, "interrupts") == 0
    assert _count(store, "events") == before


def test_waiting_job_holds_no_live_reservation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store)
    [reservation] = store.list_reservations("run", "job")
    assert reservation.disposition == "completed"


def test_append_attempt_refuses_interrupt_with_terminal_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store, "run", "job")
    with pytest.raises(ValueError, match="terminal state"):
        store.append_attempt(
            _attempt("run", "job", attempt_no),
            interrupt=_request(),
            terminal_state=JobState.ACCEPTED,
        )
    assert store.list_attempts("run") == [] and _count(store, "interrupts") == 0


def test_append_attempt_refuses_interrupt_without_requested_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store, "run", "job")
    for acceptance in (_acceptance(outcome="observed"), None):
        attempt = _attempt("run", "job", attempt_no, acceptance=_acceptance())
        attempt = Attempt(**{**attempt.__dict__, "acceptance": acceptance})
        with pytest.raises(ValueError, match="interrupt_requested"):
            store.append_attempt(attempt, interrupt=_request())
    with pytest.raises(ValueError, match="needs its interrupt request"):
        store.append_attempt(_attempt("run", "job", attempt_no))
    assert store.list_attempts("run") == [] and _count(store, "interrupts") == 0


def test_waiting_job_cannot_reserve(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store)
    with pytest.raises(StoreError, match="cannot reserve from waiting"):
        _reserve(store, "run", "job", arm=False)
    assert len(store.list_reservations("run", "job")) == 1


@pytest.mark.parametrize("state", ["waiting", "pending"])
def test_attempt_request_refused_unless_job_running(tmp_path: Path, state: str) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store, "run", "job")
    _set_state(store, "job", state)
    with pytest.raises(StoreError, match="needs a running job"):
        store.append_attempt(_attempt("run", "job", attempt_no), interrupt=_request())
    assert store.list_attempts("run") == [] and _count(store, "interrupts") == 0


@pytest.mark.parametrize("case", ["legacy_no_reservation", "unarmed", "resolved"])
def test_attempt_request_refused_without_live_armed_reservation(
    tmp_path: Path, case: str
) -> None:
    store = _store(tmp_path)
    if case == "legacy_no_reservation":
        store.mark_running("run", "job")
        attempt_no = 1
    elif case == "unarmed":
        attempt_no = _reserve(store, "run", "job", arm=False)
    else:
        attempt_no = _reserve(store, "run", "job")
        with sqlite3.connect(str(store.db_path)) as connection:
            connection.execute("UPDATE reservations SET disposition = 'process_lost'")
    with pytest.raises(StoreError, match="live armed reservation"):
        store.append_attempt(_attempt("run", "job", attempt_no), interrupt=_request())
    assert store.list_attempts("run") == [] and _count(store, "interrupts") == 0


# --------------------------------------------------------------------------
# Recovery
# --------------------------------------------------------------------------


def test_recovery_leaves_waiting_job_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.recover_reservations("run")
    assert store.get_job("run", "job").state is JobState.WAITING
    assert store.open_interrupt("run", "job") == record


def test_lost_continuation_returns_to_waiting_not_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    assert store.get_job("run", "job").state is JobState.RUNNING
    store.recover_reservations("run")
    assert store.get_job("run", "job").state is JobState.WAITING
    [lost] = store.list_continuations("run", "job")
    assert lost.disposition == "process_lost" and lost.ended_at is not None
    results = _of(store.list_events("run"), "job-kit:continuation-result")
    assert [item["payload"] for item in results] == [
        {"status": "lost", "continuation_no": continuation.continuation_no}
    ]
    again = store.begin_continuation("run", "job", now=CREATED + 5)
    assert again.continuation_no == continuation.continuation_no + 1
    assert again.interrupt_id == record.id


# --------------------------------------------------------------------------
# Database constraints
# --------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_interrupt_rows_are_immutable(tmp_path: Path, operation: str) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    statement = (
        "UPDATE interrupts SET kind = 'other' WHERE id = ?"
        if operation == "update"
        else "DELETE FROM interrupts WHERE id = ?"
    )
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement, (int(record.id),))
    assert store.open_interrupt("run", "job") == record


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_resolution_rows_are_immutable(tmp_path: Path, operation: str) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    statement = (
        "UPDATE interrupt_resolutions SET outcome = 'rejected' WHERE interrupt_id = ?"
        if operation == "update"
        else "DELETE FROM interrupt_resolutions WHERE interrupt_id = ?"
    )
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement, (int(record.id),))
    [stored] = store.list_interrupts("run")
    assert stored.resolution.outcome == "answered"


def test_second_resolution_row_is_refused_by_the_schema(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO interrupt_resolutions(interrupt_id, outcome, resolved_at) "
                "VALUES (?, 'rejected', 5.0)",
                (int(record.id),),
            )
    assert _count(store, "interrupt_resolutions") == 1


_INSERT_INTERRUPT = (
    "INSERT INTO interrupts(run_id, job_id, attempt_no, continuation_no, envelope, "
    "kind, request_schema_json, payload_json, created_at) "
    "VALUES (?, ?, ?, ?, 'job-kit.interrupt-request/v1', 'approval', '{}', '{}', 1.0)"
)


def test_second_unresolved_interrupt_is_refused_by_the_trigger(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="unresolved interrupt"):
            connection.execute(_INSERT_INTERRUPT, ("run", "job", 1, 1))
    assert _count(store, "interrupts") == 1
    # A resolved earlier interrupt does not block the next one.
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    with _raw(store) as connection:
        connection.execute(_INSERT_INTERRUPT, ("run", "job", 1, 1))
    assert _count(store, "interrupts") == 2


def test_interrupt_fk_to_job_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(str(store.db_path)) as connection:
        # An orphan attempt (foreign keys off) so only the jobs key can refuse.
        connection.execute(
            "INSERT INTO attempts(run_id, job_id, attempt_no, endpoint, backend, "
            "model, status) VALUES ('run', 'ghost', 1, 'e', 'b', 'm', 'completed')"
        )
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(_INSERT_INTERRUPT, ("run", "ghost", 1, 0))


def test_interrupt_fk_to_owning_attempt_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(_INSERT_INTERRUPT, ("run", "job", 9, 0))


def test_interrupt_unique_per_contract_run_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(_INSERT_INTERRUPT, ("run", "job", 1, 0))


def test_resolution_fk_to_interrupt_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "INSERT INTO interrupt_resolutions(interrupt_id, outcome, resolved_at) "
                "VALUES (99, 'answered', 1.0)"
            )


def test_resolution_outcome_check_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO interrupt_resolutions(interrupt_id, outcome, resolved_at) "
                "VALUES (?, 'approved', 1.0)",
                (int(record.id),),
            )


_INSERT_CONTINUATION = (
    "INSERT INTO continuations(run_id, job_id, attempt_no, continuation_no, "
    "interrupt_id, started_at) VALUES (?, ?, ?, ?, ?, 1.0)"
)


def test_continuation_fk_to_interrupt_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(_INSERT_CONTINUATION, ("run", "job", 1, 1, 99))


def test_continuation_fk_to_owning_attempt_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    with _raw(store) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(_INSERT_CONTINUATION, ("run", "job", 9, 1, int(record.id)))


def test_continuation_unique_number_is_enforced(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    with _raw(store) as connection:
        connection.execute(_INSERT_CONTINUATION, ("run", "job", 1, 1, int(record.id)))
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(_INSERT_CONTINUATION, ("run", "job", 1, 1, int(record.id)))


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_resolve_answer_records_and_keeps_the_job_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    resolution = store.resolve_interrupt(
        "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1
    )
    assert resolution.outcome == "answered" and resolution.input == {"approved": True}
    assert resolution.replayed is False and resolution.resolved_at == CREATED + 1
    assert store.get_job("run", "job").state is JobState.WAITING
    assert _names(_of(store.list_events("run"), "interrupt")) == ["interrupt", "interrupt"]
    assert [
        item["payload"]["phase"] for item in _of(store.list_events("run"), "interrupt")
    ] == ["requested", "resolved"]


def test_resolve_refuses_input_failing_request_schema(tmp_path: Path) -> None:
    from llm_scripting_kit.completion.json_schema import validate

    store = _store(tmp_path)
    record = _wait(store)
    before = _count(store, "events")
    bad = {"approved": False, "extra": 1}
    with pytest.raises(ResolutionInputError) as excinfo:
        store.resolve_interrupt("run", record.id, decision="answer", input=bad, now=CREATED + 1)
    assert excinfo.value.errors == validate(APPROVAL, bad)
    assert excinfo.value.errors  # the validator's own tuples, verbatim
    assert _count(store, "interrupt_resolutions") == 0
    assert _count(store, "events") == before


def test_resolve_refuses_oversized_input(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store, schema={"type": "string"})
    with pytest.raises(ResolutionInputError, match="65536"):
        store.resolve_interrupt("run", record.id, decision="answer", input="x" * 65535, now=CREATED + 1)
    assert _count(store, "interrupt_resolutions") == 0
    store.resolve_interrupt("run", record.id, decision="answer", input="x" * 65534, now=CREATED + 1)


def test_identical_resolution_replays_without_rows_or_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    first = store.resolve_interrupt(
        "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1
    )
    rows, events_before = _count(store, "interrupt_resolutions"), _count(store, "events")
    again = store.resolve_interrupt(
        "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 9
    )
    assert again.replayed is True and again == first
    assert again.resolved_at == CREATED + 1
    assert _count(store, "interrupt_resolutions") == rows
    assert _count(store, "events") == events_before


def test_replay_ignores_key_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    schema = {"type": "object"}
    record = _wait(store, schema=schema)
    store.resolve_interrupt(
        "run", record.id, decision="answer", input={"a": 1, "b": {"c": 2, "d": 3}}, now=CREATED + 1
    )
    again = store.resolve_interrupt(
        "run", record.id, decision="answer", input={"b": {"d": 3, "c": 2}, "a": 1}, now=CREATED + 2
    )
    assert again.replayed is True


def test_identical_resolution_replays_after_acceptance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    store.finish_continuation(
        "run",
        "job",
        continuation.attempt_no,
        continuation.continuation_no,
        acceptance=_acceptance(outcome="observed"),
        terminal_state=JobState.ACCEPTED,
        now=CREATED + 3,
    )
    assert store.get_job("run", "job").state is JobState.ACCEPTED
    again = store.resolve_interrupt(
        "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 4
    )
    assert again.replayed is True


def test_conflicting_resolution_is_refused_and_original_kept(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store, schema={"type": "object"})
    store.resolve_interrupt("run", record.id, decision="answer", input={"x": 1}, now=CREATED + 1)
    before = _count(store, "events")
    for kwargs in (
        {"decision": "answer", "input": {"x": 2}},
        {"decision": "reject", "reason": "changed my mind"},
    ):
        with pytest.raises(ResolutionConflictError, match="answered") as excinfo:
            store.resolve_interrupt("run", record.id, now=CREATED + 2, **kwargs)
        assert excinfo.value.stored_outcome == "answered"
    [stored] = store.list_interrupts("run")
    assert stored.resolution.input == {"x": 1}
    assert _count(store, "events") == before
    assert store.get_job("run", "job").state is JobState.WAITING


def test_resolution_refuses_when_owning_job_is_not_waiting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    _set_state(store, "job", "pending")  # the invariant is broken
    before = _count(store, "events")
    with pytest.raises(StoreError, match="not waiting"):
        store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    assert _count(store, "interrupt_resolutions") == 0
    assert _count(store, "events") == before


def test_resolve_unknown_interrupt_or_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.create_run("other", [_job(tmp_path, "job")])
    for run_id, interrupt_id in (("absent", record.id), ("run", "99"), ("other", record.id), ("run", "x7")):
        with pytest.raises(UnknownInterruptError):
            store.resolve_interrupt(run_id, interrupt_id, decision="reject", now=CREATED + 1)
    assert _count(store, "interrupt_resolutions") == 0


def test_operator_reject_terminalizes_with_one_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    resolution = store.resolve_interrupt(
        "run", record.id, decision="reject", reason="not this release", now=CREATED + 1
    )
    assert resolution.outcome == "rejected" and resolution.reason == "not this release"
    job = store.get_job("run", "job")
    assert job.state is JobState.OPERATOR_REJECTED and job.terminal
    stream = store.list_events("run")
    terminals = _of(stream, "terminal", unit_id="job")
    assert [item["payload"] for item in terminals] == [
        {"state": "operator_rejected", "reason": "not this release"}
    ]
    assert [item["payload"]["phase"] for item in _of(stream, "interrupt")] == [
        "requested",
        "rejected",
    ]
    assert store.resolve_interrupt(
        "run", record.id, decision="reject", reason="not this release", now=CREATED + 2
    ).replayed


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_expire_interrupts_records_expired_and_terminal(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, "a"), _job(tmp_path, "b"))
    record = _wait(store, job_id="a", expires_in_s=60)
    _wait(store, job_id="b")  # no expiry: never lapses
    [expired] = store.expire_interrupts("run", now=CREATED + 60)
    assert expired.id == record.id and expired.resolution.outcome == "expired"
    assert expired.expires_at == CREATED + 60
    assert store.get_job("run", "a").state is JobState.EXPIRED
    assert store.get_job("run", "b").state is JobState.WAITING
    stream = store.list_events("run")
    assert [item["payload"]["state"] for item in _of(stream, "terminal", unit_id="a")] == [
        "expired"
    ]
    assert [item["payload"]["phase"] for item in _of(stream, "interrupt", unit_id="a")] == [
        "requested",
        "expired",
    ]


def test_interrupt_lapses_at_expires_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store, expires_in_s=30)
    assert len(store.expire_interrupts("run", now=CREATED + 30)) == 1
    assert store.get_job("run", "job").state is JobState.EXPIRED


def test_interrupt_open_before_expires_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store, expires_in_s=30)
    assert store.expire_interrupts("run", now=CREATED + 29.5) == []
    assert store.get_job("run", "job").state is JobState.WAITING


def test_resolve_after_lapse_records_expiry_and_refuses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store, expires_in_s=30)
    with pytest.raises(InterruptExpiredError):
        store.resolve_interrupt(
            "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 31
        )
    assert store.get_job("run", "job").state is JobState.EXPIRED
    [stored] = store.list_interrupts("run")
    assert stored.resolution.outcome == "expired"
    with pytest.raises(InterruptExpiredError):
        store.resolve_interrupt("run", record.id, decision="reject", now=CREATED + 32)


def test_expiry_is_recorded_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store, expires_in_s=30)
    assert len(store.expire_interrupts("run", now=CREATED + 40)) == 1
    before = _count(store, "events")
    assert store.expire_interrupts("run", now=CREATED + 50) == []
    assert _count(store, "interrupt_resolutions") == 1
    assert _count(store, "events") == before
    assert len(_of(store.list_events("run"), "terminal")) == 1


# --------------------------------------------------------------------------
# Continuations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("resolution", ["unresolved", "rejected", "expired"])
def test_begin_continuation_requires_answered_resolution(
    tmp_path: Path, resolution: str
) -> None:
    store = _store(tmp_path)
    record = _wait(store, expires_in_s=30)
    if resolution == "rejected":
        store.resolve_interrupt("run", record.id, decision="reject", now=CREATED + 1)
    elif resolution == "expired":
        store.expire_interrupts("run", now=CREATED + 30)
    with pytest.raises(StoreError, match=f"is {resolution}, not answered"):
        store.begin_continuation("run", "job", now=CREATED + 2)
    assert store.list_continuations("run") == []


def test_begin_continuation_refuses_a_live_continuation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    _answer_and_begin(store, record)
    with pytest.raises(StoreError, match="already has a live continuation"):
        store.begin_continuation("run", "job", now=CREATED + 3)
    assert len(store.list_continuations("run")) == 1


def test_begin_continuation_records_started_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    assert continuation.continuation_no == 1 and continuation.attempt_no == 1
    assert continuation.interrupt_id == record.id and continuation.disposition is None
    assert store.get_job("run", "job").state is JobState.RUNNING
    [started] = _of(store.list_events("run"), "job-kit:continuation-started")
    assert started["payload"] == {"interrupt_id": record.id, "continuation_no": 1}
    assert started["identity"]["attempt_id"] == "1"


def test_continuation_does_not_spend_attempt_budget(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, max_attempts=2))
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    store.finish_continuation(
        "run",
        "job",
        1,
        continuation.continuation_no,
        acceptance=_acceptance(outcome="observed", exit_code=1),
        now=CREATED + 3,
    )
    assert store.get_job("run", "job").state is JobState.PENDING
    reservation = store.reserve_attempt(
        "run",
        "job",
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        reserved_at="2026-09-01T00:00:10Z",
    )
    assert reservation.budget_no == 2
    assert reservation.attempt_no == 2


def test_finish_continuation_with_follow_up_request_waits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    store.finish_continuation(
        "run",
        "job",
        1,
        continuation.continuation_no,
        acceptance=_acceptance(),
        interrupt=_request(kind="second-approval"),
        now=CREATED + 3,
    )
    assert store.get_job("run", "job").state is JobState.WAITING
    follow_up = store.open_interrupt("run", "job")
    assert follow_up.kind == "second-approval"
    assert follow_up.continuation_no == continuation.continuation_no
    assert follow_up.attempt_no == 1 and follow_up.id != record.id
    stream = store.list_events("run")
    assert _names(_of(stream, unit_id="job", attempt_id="1"))[-2:] == [
        "job-kit:continuation-result",
        "interrupt",
    ]
    assert _of(stream, "job-kit:continuation-result")[0]["payload"] == {
        "status": "completed",
        "continuation_no": 1,
        "acceptance": "interrupt_requested",
    }


@pytest.mark.parametrize("case", ["none", "finished", "other_attempt"])
def test_follow_up_request_refused_without_live_continuation(
    tmp_path: Path, case: str
) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    attempt_no, continuation_no = 1, 1
    if case == "finished":
        store.begin_continuation("run", "job", now=CREATED + 2)
        store.finish_continuation(
            "run", "job", 1, 1, disposition="interrupted", now=CREATED + 3
        )
    elif case == "other_attempt":
        store.begin_continuation("run", "job", now=CREATED + 2)
        attempt_no = 2
    before = _count(store, "interrupts")
    with pytest.raises(StoreError, match="live continuation"):
        store.finish_continuation(
            "run",
            "job",
            attempt_no,
            continuation_no,
            acceptance=_acceptance(),
            interrupt=_request(kind="second-approval"),
            now=CREATED + 4,
        )
    assert _count(store, "interrupts") == before


def test_interrupted_continuation_returns_to_waiting_with_answer_intact(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    continuation = _answer_and_begin(store, record)
    store.finish_continuation(
        "run", "job", 1, continuation.continuation_no, disposition="interrupted", now=CREATED + 3
    )
    assert store.get_job("run", "job").state is JobState.WAITING
    [stored] = store.list_interrupts("run")
    assert stored.resolution.outcome == "answered"
    assert store.begin_continuation("run", "job", now=CREATED + 4).continuation_no == 2


# --------------------------------------------------------------------------
# Run state and snapshots
# --------------------------------------------------------------------------


def test_run_state_is_waiting_with_only_waiting_and_terminal_jobs(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, "a"), _job(tmp_path, "b"))
    _wait(store, job_id="a")
    store.mark_unroutable("run", "b", "nothing fits")
    assert store.get_run("run").status is RunState.WAITING
    assert store.snapshot("run").status is RunState.WAITING


def test_run_state_is_pending_with_pending_and_waiting_jobs(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, "a"), _job(tmp_path, "b"))
    _wait(store, job_id="a")
    assert store.get_run("run").status is RunState.PENDING
    store.mark_running("run", "b")
    assert store.get_run("run").status is RunState.RUNNING


def test_snapshot_lists_interrupts_with_resolutions(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, "a"), _job(tmp_path, "b"))
    first = _wait(store, job_id="a", payload={"action": "deploy"})
    _wait(store, job_id="b", expires_in_s=100)
    store.resolve_interrupt("run", first.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    continuation = store.begin_continuation("run", "a", now=CREATED + 2)
    snapshot = store.snapshot("run", now=CREATED + 3)
    assert [record.job_id for record in snapshot.interrupts] == ["a", "b"]
    assert snapshot.interrupts[0].resolution.input == {"approved": True}
    assert snapshot.interrupts[0].payload == {"action": "deploy"}
    assert snapshot.interrupts[1].resolution is None
    assert snapshot.continuations == (continuation,)
    assert snapshot.read_at == CREATED + 3
    mapping = snapshot.to_mapping()
    assert mapping["interrupts"][0]["resolution"]["outcome"] == "answered"
    assert mapping["interrupts"][0]["request_schema"] == APPROVAL
    assert mapping["interrupts"][1]["lapsed"] is False
    assert mapping["continuations"][0]["continuation_no"] == 1
    assert mapping["counts"]["waiting"] == 1 and mapping["counts"]["running"] == 1
    json.dumps(mapping)


def test_snapshot_reports_lapse_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store, expires_in_s=30)
    before = _count(store, "events")
    snapshot = store.snapshot("run", now=CREATED + 30)
    assert snapshot.jobs[0].state is JobState.WAITING
    assert snapshot.effective_state("job") is JobState.EXPIRED
    mapping = snapshot.to_mapping()
    assert mapping["jobs"][0]["state"] == "waiting"
    assert mapping["jobs"][0]["effective_state"] == "expired"
    assert mapping["interrupts"][0]["lapsed"] is True
    assert store.snapshot("run", now=CREATED + 29).effective_state("job") is JobState.WAITING
    assert _count(store, "interrupt_resolutions") == 0
    assert _count(store, "events") == before
    assert store.get_job("run", "job").state is JobState.WAITING


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def _lifecycle(store: JobStore, tmp_path: Path) -> None:
    """A waiting, an answered-and-continued (with a follow-up rejected), and an
    expired job, all in run ``run``."""
    record = _wait(store, job_id="answered", expires_in_s=500)
    continuation = _answer_and_begin(store, record, job_id="answered")
    store.finish_continuation(
        "run",
        "answered",
        1,
        continuation.continuation_no,
        acceptance=_acceptance(),
        interrupt=_request(kind="second"),
        now=CREATED + 3,
    )
    follow_up = store.open_interrupt("run", "answered")
    store.resolve_interrupt("run", follow_up.id, decision="reject", reason="no", now=CREATED + 4)
    _wait(store, job_id="expired", expires_in_s=10)
    store.expire_interrupts("run", now=CREATED + 10)
    _wait(store, job_id="waiting")


def _lifecycle_store(tmp_path: Path) -> JobStore:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run(
        "run",
        [_job(tmp_path, "answered"), _job(tmp_path, "expired"), _job(tmp_path, "waiting")],
    )
    _lifecycle(store, tmp_path)
    return store


def test_waiting_run_events_validate_as_stream(tmp_path: Path) -> None:
    store = _lifecycle_store(tmp_path)
    stream = store.list_events("run")
    assert real_module.validate_stream(stream) == stream
    assert [item["payload"]["phase"] for item in _of(stream, "interrupt", unit_id="answered")] == [
        "requested",
        "resolved",
        "requested",
        "rejected",
    ]
    assert store.get_job("run", "answered").state is JobState.OPERATOR_REJECTED
    assert store.get_job("run", "expired").state is JobState.EXPIRED
    assert store.get_job("run", "waiting").state is JobState.WAITING


def test_interrupt_events_render_under_v2_and_others_under_v1(tmp_path: Path) -> None:
    store = _lifecycle_store(tmp_path)
    stream = store.list_events("run")
    for item in stream:
        expected = real_module.SCHEMA_V2 if item["event"] == "interrupt" else real_module.SCHEMA_V1
        assert item["schema"] == expected, item
    with sqlite3.connect(str(store.db_path)) as connection:
        stored = dict(
            connection.execute(
                "SELECT event, schema FROM events WHERE event IN ('interrupt', 'result') "
                "GROUP BY event"
            ).fetchall()
        )
    assert stored == {"interrupt": real_module.SCHEMA_V2, "result": real_module.SCHEMA_V1}


def test_null_event_schema_renders_as_v1(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with sqlite3.connect(str(store.db_path)) as connection:
        connection.execute("UPDATE events SET schema = NULL")
    [created] = store.list_events("run")
    assert created["schema"] == real_module.SCHEMA_V1
    assert created["event"] == "job-kit:run-created"


def test_interrupt_event_identity_is_the_owning_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path, _job(tmp_path, max_attempts=3))
    # Attempt 1 is rejected; attempt 2 raises the interrupt.
    attempt_no = _reserve(store, "run", "job")
    store.append_attempt(
        _attempt("run", "job", attempt_no, acceptance=_acceptance("observed", exit_code=1))
    )
    record = _wait(store)
    assert record.attempt_no == 2
    store.resolve_interrupt("run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    for item in _of(store.list_events("run"), "interrupt"):
        assert item["identity"] == {"run_id": "run", "unit_id": "job", "attempt_id": "2"}
        assert item["source"] == {
            "plugin": "job-kit",
            "adapter": "fake-backend",
            "model": "fake-model",
        }
        assert item["payload"]["interrupt_id"] == record.id


def test_interrupt_event_with_content_key_rolls_back_its_fact(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = _store(tmp_path, _job(tmp_path, "a"), _job(tmp_path, "b"))
    _wait(store, job_id="a")
    [requested] = _of(store.list_events("run"), "interrupt")
    assert set(requested["payload"]) <= real_module.INTERRUPT_PAYLOAD_KEYS

    attempt_no = _reserve(store, "run", "b")
    before = _count(store, "events")
    real_build = events.build_event

    def with_content(**kwargs: Any) -> dict:
        if kwargs["event"] == "interrupt":
            kwargs["payload"] = {**kwargs["payload"], "payload": {"action": "push tag"}}
        return real_build(**kwargs)

    monkeypatch.setattr(events, "build_event", with_content)
    with pytest.raises(real_module.EventError, match="closed"):
        store.append_attempt(_attempt("run", "b", attempt_no), interrupt=_request())
    monkeypatch.undo()
    assert store.list_attempts("run", "b") == []
    assert store.get_reservation("run", "b", attempt_no).disposition is None
    assert store.open_interrupt("run", "b") is None
    assert _count(store, "events") == before


def test_no_event_field_carries_request_payload_schema_or_input(tmp_path: Path) -> None:
    sentinels = ("SENTINEL-PAYLOAD", "SENTINEL-SCHEMA", "SENTINEL-INPUT")
    schema = {
        "type": "object",
        "description": sentinels[1],
        "properties": {"note": {"type": "string", "title": sentinels[1]}},
    }
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path, "a"), _job(tmp_path, "b"), _job(tmp_path, "c")])
    record = _wait(store, job_id="a", schema=schema, payload={"note": sentinels[0]})
    store.resolve_interrupt(
        "run", record.id, decision="answer", input={"note": sentinels[2]}, now=CREATED + 1
    )
    continuation = store.begin_continuation("run", "a", now=CREATED + 2)
    store.finish_continuation(
        "run",
        "a",
        1,
        continuation.continuation_no,
        acceptance=_acceptance(),
        interrupt=_request(schema=schema, payload={"note": sentinels[0]}),
        now=CREATED + 3,
    )
    follow_up = store.open_interrupt("run", "a")
    store.resolve_interrupt("run", follow_up.id, decision="reject", reason="operator text", now=CREATED + 4)
    _wait(store, job_id="b", schema=schema, payload={"note": sentinels[0]}, expires_in_s=5)
    store.expire_interrupts("run", now=CREATED + 5)
    other = _wait(store, job_id="c", schema=schema, payload={"note": sentinels[0]})
    store.resolve_interrupt("run", other.id, decision="answer", input={"note": sentinels[2]}, now=CREATED + 6)
    store.begin_continuation("run", "c", now=CREATED + 7)
    store.recover_reservations("run")

    stream = store.list_events("run")
    assert {"interrupt", "terminal", "job-kit:continuation-result"} <= set(_names(list(stream)))
    for item in stream:
        text = json.dumps(item, sort_keys=True)
        for sentinel in sentinels:
            assert sentinel not in text, (sentinel, item)
    # The status snapshot does carry them: the operator needs them to answer.
    status = json.dumps(store.snapshot("run").to_mapping())
    assert all(sentinel in status for sentinel in sentinels)


def test_resolve_interrupt_refuses_before_writing_when_validator_too_old(
    tmp_path: Path, monkeypatch: Any
) -> None:
    store = _store(tmp_path)
    record = _wait(store, expires_in_s=30)
    before = _count(store, "events")
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion.json_schema", None)
    with pytest.raises(JsonSchemaSupportError, match="0.56.0"):
        # Lapsed: a probe inside the transaction would record the expiry first.
        store.resolve_interrupt(
            "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 31
        )
    monkeypatch.undo()
    assert _count(store, "interrupt_resolutions") == 0
    assert _count(store, "events") == before
    assert store.get_job("run", "job").state is JobState.WAITING
