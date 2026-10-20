"""The ``cancelled`` job state and ``llm-kit jobs cancel`` (ledger schema 13).

Cancel is a ledger operation on a run whose process has already stopped. These
tests drive the store directly (a job in each non-terminal state, with the open
facts a real run leaves behind) and the CLI verb, and pin the schema-13
migration from schema 12 and the deliberate refusal by a reader that predates
it. The frozen job-kit side of that refusal is in ``test_jobs_parity.py``
(``TestLedgerSchema``).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from llm_kit.cli import jobs as jobs_cli
from llm_kit.jobs import store as store_module
from llm_kit.jobs.interrupts import REQUEST_ENVELOPE_V1
from llm_kit.jobs.model import (
    TERMINAL_STATES,
    Acceptance,
    Attempt,
    Contract,
    InterruptRequest,
    Job,
    JobState,
    Prompt,
    RunState,
    Usage,
)
from llm_kit.jobs.store import (
    ALREADY_TERMINAL_OUTCOME,
    CANCEL_SCHEMA,
    CANCELLED_OUTCOME,
    INTERRUPT_SCHEMA,
    JobStore,
    LedgerReader,
    ResolutionConflictError,
    StoreError,
    UnknownJobError,
    UnknownRunError,
)

ARMED_AT = "2026-09-01T00:00:01Z"
ENDED_AT = "2026-09-01T00:00:02Z"
CREATED = 1000.0
REASON = "operator stopped the run"
APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"const": True}},
    "additionalProperties": False,
}


def _job(directory: Path, job_id: str = "job") -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


def _acceptance(outcome: str = "interrupt_requested") -> Acceptance:
    return Acceptance(
        command=("contract",),
        directory=Path.cwd(),
        exit_code=0,
        stdout="",
        stderr="",
        wall_ms=1,
        accepted=False,
        outcome=outcome,
    )


def _attempt(job_id: str, attempt_no: int) -> Attempt:
    return Attempt(
        run_id="run",
        job_id=job_id,
        attempt_no=attempt_no,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        status="completed",
        started_at=ARMED_AT,
        ended_at=ENDED_AT,
        usage=Usage(input_tokens=3, output_tokens=5),
        response_text="the model answer",
        acceptance=_acceptance(),
    )


def _request() -> InterruptRequest:
    return InterruptRequest(
        envelope=REQUEST_ENVELOPE_V1,
        kind="approval",
        request_schema=dict(APPROVAL),
        payload={"action": "push tag"},
        expires_in_s=None,
    )


def _store(tmp_path: Path, *job_ids: str) -> JobStore:
    store = JobStore(tmp_path / "ledger.sqlite3")
    store.create_run("run", [_job(tmp_path, job_id) for job_id in (job_ids or ("job",))])
    return store


def _reserve(store: JobStore, job_id: str = "job", *, arm: bool = True) -> int:
    reservation = store.reserve_attempt(
        "run",
        job_id,
        endpoint="fake-endpoint",
        backend="fake-backend",
        model="fake-model",
        reserved_at="2026-09-01T00:00:00Z",
    )
    if arm:
        store.arm_reservation("run", job_id, reservation.attempt_no, invoke_armed_at=ARMED_AT)
    return reservation.attempt_no


def _wait(store: JobStore, job_id: str = "job") -> Any:
    """Take a pending job to waiting on a fresh, unresolved interrupt."""
    attempt_no = _reserve(store, job_id)
    store.append_attempt(_attempt(job_id, attempt_no), interrupt=_request(), at=CREATED)
    record = store.open_interrupt("run", job_id)
    assert record is not None
    return record


def _continue(store: JobStore, job_id: str = "job") -> Any:
    """Take a pending job through an answered interrupt into a live continuation."""
    record = _wait(store, job_id)
    store.resolve_interrupt(
        "run", record.id, decision="answer", input={"approved": True}, now=CREATED + 1
    )
    return store.begin_continuation("run", job_id, now=CREATED + 2)


def _events(store: JobStore, event: Optional[str] = None, job_id: str = "job") -> list[dict]:
    return [
        item
        for item in store.list_events("run")
        if (event is None or item["event"] == event)
        and item["identity"].get("unit_id") in (job_id, None)
    ]


def _rows(store: JobStore, table: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(str(store.db_path))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        connection.close()


def _schema_version(store: JobStore) -> int:
    return int(_rows(store, "schema_version")[0]["version"])


# --------------------------------------------------------------------------
# The state
# --------------------------------------------------------------------------


def test_cancelled_is_a_terminal_state() -> None:
    assert JobState.CANCELLED.value == "cancelled"
    assert JobState.CANCELLED in TERMINAL_STATES
    assert CANCEL_SCHEMA == 13 and INTERRUPT_SCHEMA == 12
    assert len(store_module._MIGRATIONS) == CANCEL_SCHEMA


# --------------------------------------------------------------------------
# mark_cancelled from each non-terminal state
# --------------------------------------------------------------------------


def test_cancel_from_pending_writes_only_a_terminal_event(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = store.mark_cancelled("run", "job", REASON, at=CREATED)
    assert record.state is JobState.CANCELLED and record.terminal
    assert record.error_message == REASON
    terminal = _events(store, "terminal")
    assert [item["payload"] for item in terminal] == [{"state": "cancelled", "reason": REASON}]
    assert _events(store, "result") == []
    assert store.get_run("run").status is RunState.COMPLETED


def test_cancel_from_running_closes_an_armed_reservation_as_lost(tmp_path: Path) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store)
    assert store.get_job("run", "job").state is JobState.RUNNING
    store.mark_cancelled("run", "job", REASON, at=CREATED)
    reservation = store.get_reservation("run", "job", attempt_no)
    assert reservation.disposition == "cancelled"
    assert reservation.resolved_at is not None and reservation.lost_at is not None
    assert reservation.loss_reason == REASON
    stream = store.list_events("run")
    names = [item["event"] for item in stream if item["identity"].get("unit_id") == "job"]
    assert names[-2:] == ["result", "terminal"]
    result = next(item for item in stream if item["event"] == "result")
    assert result["payload"]["status"] == "lost"
    assert result["identity"]["attempt_id"] == str(attempt_no)
    assert [item["payload"]["state"] for item in stream if item["event"] == "terminal"] == [
        "cancelled"
    ]


def test_cancel_from_running_with_an_unarmed_reservation_records_no_loss_time(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    attempt_no = _reserve(store, arm=False)
    store.mark_cancelled("run", "job", REASON, at=CREATED)
    reservation = store.get_reservation("run", "job", attempt_no)
    assert reservation.disposition == "cancelled"
    assert reservation.lost_at is None and reservation.resolved_at is not None


def test_cancel_from_waiting_resolves_the_open_interrupt_as_cancelled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    assert store.get_job("run", "job").state is JobState.WAITING
    job = store.mark_cancelled("run", "job", REASON, at=CREATED + 5)
    assert job.state is JobState.CANCELLED
    assert store.open_interrupt("run", "job") is None
    resolved = store.list_interrupts("run", "job")[0]
    assert resolved.id == record.id
    assert resolved.resolution.outcome == "cancelled"
    assert resolved.resolution.reason == REASON
    assert resolved.resolution.input is None
    # The attempt that raised the interrupt already ended, so there is no
    # second result for it; the stream (validated whole) ends in the terminal.
    assert len(_events(store, "result")) == 1
    assert _events(store, "terminal")[-1]["payload"]["state"] == "cancelled"
    snapshot = store.snapshot("run")
    assert snapshot.interrupts[0].resolution.outcome == "cancelled"
    assert snapshot.counts["cancelled"] == 1 and snapshot.counts["waiting"] == 0


def test_cancel_closes_a_live_continuation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _continue(store)
    assert store.get_job("run", "job").state is JobState.RUNNING
    store.mark_cancelled("run", "job", REASON, at=CREATED + 5)
    continuation = store.list_continuations("run", "job")[0]
    assert continuation.disposition == "cancelled" and continuation.ended_at is not None
    lost = [
        item
        for item in _events(store)
        if item["event"] == "job-kit:continuation-result"
        and item["payload"]["status"] == "cancelled"
    ]
    assert len(lost) == 1
    assert _events(store, "terminal")[-1]["payload"]["state"] == "cancelled"


def test_cancel_leaves_no_open_fact_behind(tmp_path: Path) -> None:
    store = _store(tmp_path, "run-job", "wait-job", "cont-job", "idle-job")
    _reserve(store, "run-job")
    _wait(store, "wait-job")
    _continue(store, "cont-job")
    results = store.cancel_jobs("run", REASON, at=CREATED + 9)
    assert [item.outcome for item in results] == [CANCELLED_OUTCOME] * 4
    assert all(item.job.state is JobState.CANCELLED for item in results)
    assert all(row["disposition"] is not None for row in _rows(store, "reservations"))
    assert all(row["disposition"] is not None for row in _rows(store, "continuations"))
    assert store.list_interrupts("run")
    assert all(item.resolution is not None for item in store.list_interrupts("run"))
    assert store.recover_reservations("run") == []
    assert store.get_run("run").status is RunState.COMPLETED


# --------------------------------------------------------------------------
# Terminal jobs and idempotence
# --------------------------------------------------------------------------


def test_an_already_terminal_job_is_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_failed("run", "job", "model said no", at=CREATED)
    before_events = store.list_events("run")
    before_job = store.get_job("run", "job")
    [outcome] = store.cancel_jobs("run", REASON, job_id="job", at=CREATED + 9)
    assert outcome.outcome == ALREADY_TERMINAL_OUTCOME
    assert outcome.job == before_job and outcome.job.state is JobState.FAILED
    assert store.list_events("run") == before_events
    assert store.mark_cancelled("run", "job", REASON) == before_job


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store)
    first = store.cancel_jobs("run", REASON, at=CREATED + 5)
    events_after_first = store.list_events("run")
    interrupts_after_first = _rows(store, "interrupt_resolutions")
    second = store.cancel_jobs("run", "a second reason", at=CREATED + 6)
    assert [item.outcome for item in first] == [CANCELLED_OUTCOME]
    assert [item.outcome for item in second] == [ALREADY_TERMINAL_OUTCOME]
    assert second[0].job.state is JobState.CANCELLED
    assert second[0].job.error_message == REASON
    assert store.list_events("run") == events_after_first
    assert [tuple(row) for row in _rows(store, "interrupt_resolutions")] == [
        tuple(row) for row in interrupts_after_first
    ]


def test_cancel_run_cancels_only_the_jobs_that_have_not_ended(tmp_path: Path) -> None:
    store = _store(tmp_path, "done", "waiting", "idle")
    store.mark_failed("run", "done", "no", at=CREATED)
    _wait(store, "waiting")
    results = store.cancel_jobs("run", REASON, at=CREATED + 9)
    assert [(item.job.id, item.outcome) for item in results] == [
        ("done", ALREADY_TERMINAL_OUTCOME),
        ("waiting", CANCELLED_OUTCOME),
        ("idle", CANCELLED_OUTCOME),
    ]
    assert store.get_job("run", "done").state is JobState.FAILED


def test_cancel_is_one_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path, "a", "b")
    _wait(store, "a")
    real = JobStore._cancel_one
    seen: list[str] = []

    def fail_on_second(self: JobStore, conn: Any, job_row: Any, reason: str, when: float) -> None:
        if seen:
            raise RuntimeError("boom")
        seen.append(str(job_row["id"]))
        real(self, conn, job_row, reason, when)

    monkeypatch.setattr(JobStore, "_cancel_one", fail_on_second)
    with pytest.raises(RuntimeError):
        store.cancel_jobs("run", REASON)
    monkeypatch.undo()
    assert store.get_job("run", "a").state is JobState.WAITING
    assert store.open_interrupt("run", "a") is not None


def test_errors_are_typed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(UnknownRunError):
        store.cancel_jobs("missing", REASON)
    with pytest.raises(UnknownJobError):
        store.mark_cancelled("run", "missing", REASON)
    with pytest.raises(ValueError):
        store.mark_cancelled("run", "job", "   ")
    assert store.get_job("run", "job").state is JobState.PENDING


def test_a_cancelled_interrupt_cannot_be_resolved_afterwards(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record = _wait(store)
    store.mark_cancelled("run", "job", REASON)
    for decision, kwargs in (("answer", {"input": {"approved": True}}), ("reject", {})):
        with pytest.raises(ResolutionConflictError) as excinfo:
            store.resolve_interrupt("run", record.id, decision=decision, **kwargs)
        assert excinfo.value.stored_outcome == "cancelled"
    assert store.get_job("run", "job").state is JobState.CANCELLED


def test_the_ledger_reader_reads_a_cancelled_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _wait(store)
    store.mark_cancelled("run", "job", REASON)
    reader = LedgerReader(store.db_path)
    assert reader.schema_version() == 13
    snapshot = reader.snapshot("run")
    assert snapshot.jobs[0].state is JobState.CANCELLED
    assert snapshot.effective_state("job") is JobState.CANCELLED
    assert snapshot.interrupts[0].resolution.outcome == "cancelled"


# --------------------------------------------------------------------------
# Schema 13: migration, constraints and the old-reader refusal
# --------------------------------------------------------------------------


def _store_at_schema_12(path: Path, monkeypatch: pytest.MonkeyPatch) -> JobStore:
    full = list(store_module._MIGRATIONS)
    monkeypatch.setattr(store_module, "_MIGRATIONS", full[:12])
    try:
        return JobStore(path)
    finally:
        monkeypatch.setattr(store_module, "_MIGRATIONS", full)


def test_schema_13_changes_only_the_resolution_check(tmp_path: Path) -> None:
    fresh = JobStore(tmp_path / "fresh.sqlite3")
    assert _schema_version(fresh) == 13
    sql = {
        row["name"]: row["sql"]
        for row in _rows_master(fresh)
        if row["name"] in ("interrupt_resolutions",)
    }
    assert "'cancelled'" in sql["interrupt_resolutions"]
    assert {row["name"] for row in _rows_master(fresh) if row["type"] == "trigger"} == {
        "interrupts_one_open_per_job",
        "interrupts_immutable_update",
        "interrupts_immutable_delete",
        "resolutions_immutable_update",
        "resolutions_immutable_delete",
    }


def _rows_master(store: JobStore) -> list[sqlite3.Row]:
    return _rows(store, "sqlite_master")


def test_a_schema_12_ledger_upgrades_in_place_and_keeps_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "old.sqlite3"
    old = _store_at_schema_12(path, monkeypatch)
    assert _schema_version(old) == 12
    old.create_run("run", [_job(tmp_path, "answered"), _job(tmp_path, "rejected"), _job(tmp_path, "open")])
    first = _wait(old, "answered")
    old.resolve_interrupt("run", first.id, decision="answer", input={"approved": True}, now=CREATED + 1)
    second = _wait(old, "rejected")
    old.resolve_interrupt("run", second.id, decision="reject", reason="no", now=CREATED + 1)
    _wait(old, "open")
    before = [tuple(row) for row in _rows(old, "interrupt_resolutions")]
    assert [row[1] for row in before] == ["answered", "rejected"]

    upgraded = JobStore(path, create=False)
    assert _schema_version(upgraded) == 13
    assert [tuple(row) for row in _rows(upgraded, "interrupt_resolutions")] == before
    assert upgraded.open_interrupt("run", "open") is not None
    # The recreated triggers still enforce what schema 12 enforced.
    connection = sqlite3.connect(str(path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE interrupt_resolutions SET reason = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM interrupt_resolutions")
        with pytest.raises(sqlite3.IntegrityError, match="unresolved"):
            connection.execute(
                "INSERT INTO interrupts(run_id, job_id, attempt_no, continuation_no, "
                "envelope, kind, request_schema_json, payload_json, created_at) "
                "SELECT run_id, job_id, attempt_no, 7, envelope, kind, "
                "request_schema_json, payload_json, created_at FROM interrupts "
                "WHERE job_id = 'open'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO interrupt_resolutions(interrupt_id, outcome, resolved_at) "
                "VALUES (999, 'bogus', 1)"
            )
    finally:
        connection.close()
    # The upgraded ledger can cancel the job that was waiting at schema 12.
    upgraded.mark_cancelled("run", "open", REASON)
    assert upgraded.get_job("run", "open").state is JobState.CANCELLED


def test_the_resolution_check_admits_cancelled_only_from_schema_13(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _store_at_schema_12(tmp_path / "old.sqlite3", monkeypatch)
    old.create_run("run", [_job(tmp_path)])
    record = _wait(old)
    connection = sqlite3.connect(str(old.db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                "INSERT INTO interrupt_resolutions(interrupt_id, outcome, resolved_at) "
                "VALUES (?, 'cancelled', 1)",
                (int(record.id),),
            )
    finally:
        connection.close()


def test_a_reader_built_for_schema_12_refuses_a_schema_13_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    db = store.db_path
    before = db.read_bytes()
    monkeypatch.setattr(store_module, "_MIGRATIONS", list(store_module._MIGRATIONS)[:12])
    with pytest.raises(StoreError, match=r"schema version 13 is newer .*max 12"):
        JobStore(db)
    with pytest.raises(StoreError, match=r"schema version 13 is newer .*max 12"):
        LedgerReader(db).schema_version()
    monkeypatch.undo()
    assert db.read_bytes() == before


# --------------------------------------------------------------------------
# The CLI verb
# --------------------------------------------------------------------------


def _cli(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, str]:
    code = jobs_cli.run(list(argv))
    return code, capsys.readouterr().out


def test_cli_cancel_prints_one_outcome_per_job(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    store = _store(tmp_path, "done", "waiting", "idle")
    store.mark_failed("run", "done", "no", at=CREATED)
    _wait(store, "waiting")
    code, out = _cli(capsys, "cancel", "run", "--store", str(store.db_path), "--reason", REASON)
    assert code == 0
    assert json.loads(out) == {
        "run": "run",
        "jobs": [
            {"id": "done", "state": "failed", "outcome": "already-terminal"},
            {"id": "waiting", "state": "cancelled", "outcome": "cancelled"},
            {"id": "idle", "state": "cancelled", "outcome": "cancelled"},
        ],
    }
    code, out = _cli(capsys, "cancel", "run", "--store", str(store.db_path), "--reason", REASON)
    assert code == 0
    assert {item["outcome"] for item in json.loads(out)["jobs"]} == {"already-terminal"}


def test_cli_cancel_job_limits_the_cancel_to_one_job(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    store = _store(tmp_path, "a", "b")
    code, out = _cli(
        capsys, "cancel", "run", "--store", str(store.db_path), "--reason", REASON, "--job", "b"
    )
    assert code == 0
    assert json.loads(out)["jobs"] == [{"id": "b", "state": "cancelled", "outcome": "cancelled"}]
    assert store.get_job("run", "a").state is JobState.PENDING


def test_cli_cancel_exits_3_for_an_unknown_run_job_or_store(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    store = _store(tmp_path)
    for argv in (
        ("cancel", "missing", "--store", str(store.db_path), "--reason", REASON),
        ("cancel", "run", "--store", str(store.db_path), "--reason", REASON, "--job", "missing"),
        ("cancel", "run", "--store", str(tmp_path / "absent.sqlite3"), "--reason", REASON),
    ):
        code, out = _cli(capsys, *argv)
        assert code == 3 and out == "", argv
    assert not (tmp_path / "absent.sqlite3").exists()
    assert store.get_job("run", "job").state is JobState.PENDING


def test_cli_cancel_requires_a_reason(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    store = _store(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        jobs_cli.run(["cancel", "run", "--store", str(store.db_path)])
    assert excinfo.value.code == 2
    capsys.readouterr()
    code, _ = _cli(capsys, "cancel", "run", "--store", str(store.db_path), "--reason", "  ")
    assert code == 3
    assert store.get_job("run", "job").state is JobState.PENDING


def test_a_run_with_a_cancelled_job_exits_1_from_status_helpers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.mark_cancelled("run", "job", REASON)
    assert jobs_cli._exit_for_snapshot(store.snapshot("run")) == jobs_cli.EXIT_FAILURE


def test_the_exit_code_epilog_documents_cancel() -> None:
    epilog = jobs_cli._EXIT_EPILOG
    assert "cancelled" in epilog and "`cancel`" in epilog
