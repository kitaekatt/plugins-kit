"""Tests for the durable-interrupt CLI surface: the ``resolve`` verb, exit code 4
for a waiting run, and a ``status`` that reads through the read-only ledger
reader. Each test drives ``cli.main`` against a ledger built through store
verbs or by hand, so none needs the runner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from job_kit import cli, interrupts
from job_kit.interrupts import REQUEST_ENVELOPE_V1, JsonSchemaSupportError
from job_kit.model import (
    Acceptance,
    Attempt,
    Contract,
    InterruptRequest,
    Job,
    JobRecord,
    JobState,
    Prompt,
    RunRecord,
    RunSnapshot,
    RunState,
)
from job_kit.store import JobStore, _MIGRATIONS


CREATED = 1000.0
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


def _waiting_ledger(
    tmp_path: Path, *, expires_in_s: Optional[int] = None
) -> tuple[Path, JobStore]:
    """A schema-12 ledger whose job ``job`` waits on interrupt ``1``."""
    path = tmp_path / "ledger.sqlite3"
    store = JobStore(path)
    store.create_run("run", [_job(tmp_path, "job"), _job(tmp_path, "other")])
    reservation = store.reserve_attempt(
        "run", "job", endpoint="e", backend="b", model="m",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation(
        "run", "job", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z"
    )
    store.append_attempt(
        Attempt(
            run_id="run",
            job_id="job",
            attempt_no=reservation.attempt_no,
            endpoint="e",
            backend="b",
            model="m",
            status="completed",
            started_at=None,
            ended_at="2026-09-01T00:00:02Z",
            response_text="the model answer",
            acceptance=Acceptance(
                command=("c",),
                directory=tmp_path,
                exit_code=0,
                stdout="",
                stderr="",
                wall_ms=1,
                accepted=False,
                outcome="interrupt_requested",
            ),
        ),
        interrupt=InterruptRequest(
            envelope=REQUEST_ENVELOPE_V1,
            kind="approval",
            request_schema=dict(APPROVAL),
            payload={"action": "deploy"},
            expires_in_s=expires_in_s,
        ),
        at=CREATED,
    )
    return path, store


def _resolve(path: Path, *args: str, interrupt_id: str = "1", run: str = "run") -> list[str]:
    return ["resolve", run, interrupt_id, *args, "--store", str(path)]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path: Path) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        connection.close()


def _journal_mode(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        connection.close()


def _ledger_at(tmp_path: Path, version: int, *, wal: bool = True) -> Path:
    """Build a ledger at an older schema by hand, with one run and one job."""
    path = tmp_path / f"schema-{version}.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        if wal:
            connection.execute("PRAGMA journal_mode = WAL")
        for index in range(version):
            for statement in _MIGRATIONS[index]:
                connection.execute(statement)
            if index:
                connection.execute("UPDATE schema_version SET version = ?", (index + 1,))
        connection.execute(
            "INSERT INTO runs(id, created_at, max_parallel) VALUES ('run', 1.0, 1)"
        )
        connection.execute(
            "INSERT INTO jobs(run_id, id, ordinal, definition_json, state, created_at, "
            "updated_at) VALUES ('run', 'job', 0, ?, 'pending', 1.0, 1.0)",
            (json.dumps(_job(tmp_path).to_mapping(), sort_keys=True),),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _count(path: Path, table: str) -> int:
    connection = sqlite3.connect(str(path))
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _out(capsys: Any) -> dict:
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------
# resolve: arguments
# --------------------------------------------------------------------------


def test_resolve_arguments_are_exclusive_and_required(tmp_path: Path, capsys: Any) -> None:
    path, _store = _waiting_ledger(tmp_path)
    for extra in (
        [],
        ["--input", "{}", "--reject"],
        ["--input", "{}", "--input-file", str(tmp_path / "x.json")],
        ["--input-file", str(tmp_path / "x.json"), "--reject"],
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(_resolve(path, *extra))
        assert excinfo.value.code == cli.EXIT_USAGE
    capsys.readouterr()
    assert _count(path, "interrupt_resolutions") == 0


def test_resolve_reason_requires_reject(tmp_path: Path, capsys: Any) -> None:
    path, _store = _waiting_ledger(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(_resolve(path, "--input", '{"approved": true}', "--reason", "why"))
    assert excinfo.value.code == cli.EXIT_USAGE
    assert "--reason" in capsys.readouterr().err
    assert _count(path, "interrupt_resolutions") == 0


# --------------------------------------------------------------------------
# resolve: outcomes
# --------------------------------------------------------------------------


def test_resolve_records_and_exits_0(tmp_path: Path, capsys: Any) -> None:
    path, store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--input", '{"approved": true}')) == cli.EXIT_OK
    body = _out(capsys)
    assert body == {
        "run": "run",
        "interrupt_id": "1",
        "job_id": "job",
        "outcome": "recorded",
        "decision": "answer",
        "state": "waiting",
        "store": str(path.resolve()),
    }
    stored = store.list_interrupts("run")[0].resolution
    assert stored is not None and stored.outcome == "answered"
    assert stored.input == {"approved": True}


def test_resolve_reject_records_operator_rejection(tmp_path: Path, capsys: Any) -> None:
    path, store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--reject", "--reason", "not today")) == cli.EXIT_OK
    body = _out(capsys)
    assert body["decision"] == "reject" and body["state"] == "operator_rejected"
    assert store.list_interrupts("run")[0].resolution.reason == "not today"


def test_resolve_replay_exits_0_with_replayed(tmp_path: Path, capsys: Any) -> None:
    path, _store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--input", '{"approved": true}')) == cli.EXIT_OK
    capsys.readouterr()
    before = _count(path, "events")
    assert cli.main(_resolve(path, "--input", '{ "approved" : true }')) == cli.EXIT_OK
    assert _out(capsys)["outcome"] == "replayed"
    assert _count(path, "events") == before
    assert _count(path, "interrupt_resolutions") == 1


def test_resolve_conflict_exits_1(tmp_path: Path, capsys: Any) -> None:
    path, store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--input", '{"approved": true}')) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(_resolve(path, "--reject", "--reason", "changed my mind")) == cli.EXIT_FAILURE
    body = _out(capsys)
    assert body["refused"] == "conflict"
    assert store.list_interrupts("run")[0].resolution.outcome == "answered"


def test_resolve_schema_refusal_exits_1_with_errors(tmp_path: Path, capsys: Any) -> None:
    path, _store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--input", '{"approved": false}')) == cli.EXIT_FAILURE
    body = _out(capsys)
    assert body["refused"] == "schema"
    assert body["errors"] and set(body["errors"][0]) == {"pointer", "keyword"}
    assert _count(path, "interrupt_resolutions") == 0


@pytest.mark.parametrize("text", ["{not json", '{"approved": NaN}', '{"a": 1, "a": 2}', ""])
def test_resolve_invalid_json_exits_1(tmp_path: Path, capsys: Any, text: str) -> None:
    path, _store = _waiting_ledger(tmp_path)
    assert cli.main(_resolve(path, "--input", text)) == cli.EXIT_FAILURE
    captured = capsys.readouterr()
    assert json.loads(captured.out)["refused"] == "invalid_json"
    assert "Traceback" not in captured.err
    assert _count(path, "interrupt_resolutions") == 0


def test_resolve_oversized_input_exits_1(tmp_path: Path, capsys: Any) -> None:
    path, _store = _waiting_ledger(tmp_path)
    big = json.dumps({"approved": True, "pad": "x" * (interrupts.INPUT_LIMIT + 1)})
    assert cli.main(_resolve(path, "--input", big)) == cli.EXIT_FAILURE
    assert json.loads(capsys.readouterr().out)["refused"] in {"input", "schema"}
    assert _count(path, "interrupt_resolutions") == 0


def test_resolve_expired_exits_1_and_records(tmp_path: Path, capsys: Any) -> None:
    # Created at epoch 1000 with a 1 second expiry: long lapsed by the real clock.
    path, store = _waiting_ledger(tmp_path, expires_in_s=1)
    assert cli.main(_resolve(path, "--input", '{"approved": true}')) == cli.EXIT_FAILURE
    assert _out(capsys)["refused"] == "expired"
    assert store.get_job("run", "job").state is JobState.EXPIRED
    assert store.list_interrupts("run")[0].resolution.outcome == "expired"


@pytest.mark.parametrize("case", ["run", "interrupt", "other_run"])
def test_resolve_unknown_interrupt_exits_3(tmp_path: Path, capsys: Any, case: str) -> None:
    path, store = _waiting_ledger(tmp_path)
    store.create_run("second", [_job(tmp_path, "job")])
    args = {
        "run": _resolve(path, "--reject", run="missing"),
        "interrupt": _resolve(path, "--reject", interrupt_id="99"),
        "other_run": _resolve(path, "--reject", run="second"),
    }[case]
    assert cli.main(args) == cli.EXIT_RUNNER_FAILURE
    assert capsys.readouterr().err.startswith("job-kit:")
    assert _count(path, "interrupt_resolutions") == 0


def test_resolve_input_file(tmp_path: Path, capsys: Any) -> None:
    path, store = _waiting_ledger(tmp_path)
    answer = tmp_path / "answer.json"
    answer.write_text('{"approved": true}', encoding="utf-8")
    assert cli.main(_resolve(path, "--input-file", str(answer))) == cli.EXIT_OK
    assert _out(capsys)["outcome"] == "recorded"
    assert store.list_interrupts("run")[0].resolution.input == {"approved": True}


def test_resolve_refuses_before_opening_or_migrating_the_ledger(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    def broken() -> Any:
        raise JsonSchemaSupportError("llm-scripting-kit too old")

    monkeypatch.setattr(interrupts, "_schema_validator", broken)
    old = _ledger_at(tmp_path, 11)
    before = _digest(old)
    missing = tmp_path / "nowhere" / "ledger.sqlite3"

    assert cli.main(_resolve(old, "--reject")) == cli.EXIT_RUNNER_FAILURE
    assert "llm-scripting-kit too old" in capsys.readouterr().err
    assert _version(old) == 11
    assert _digest(old) == before

    assert cli.main(_resolve(missing, "--reject")) == cli.EXIT_RUNNER_FAILURE
    assert "llm-scripting-kit too old" in capsys.readouterr().err
    assert not missing.parent.exists()


# --------------------------------------------------------------------------
# exit code 4
# --------------------------------------------------------------------------


def _snapshot(tmp_path: Path, *states: JobState) -> RunSnapshot:
    records = tuple(
        JobRecord(
            job=_job(tmp_path, f"job-{index}"),
            state=state,
            created_at=0.0,
            updated_at=0.0,
        )
        for index, state in enumerate(states)
    )
    run = RunRecord(
        id="cli-run",
        created_at=0.0,
        jobs_path=None,
        max_parallel=1,
        workspace_root=None,
        status=RunState.WAITING,
    )
    return RunSnapshot(run=run, jobs=records, attempts=())


def _stub_runner(monkeypatch: Any, snapshot: RunSnapshot) -> None:
    monkeypatch.setattr(cli, "run_job_file", lambda *a, **k: snapshot)
    monkeypatch.setattr(cli, "resume_run", lambda *a, **k: snapshot)


def test_run_exits_4_when_only_waiting(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _stub_runner(monkeypatch, _snapshot(tmp_path, JobState.ACCEPTED, JobState.WAITING))
    store = tmp_path / "s.sqlite3"
    assert cli.main(["run", "jobs.yaml", "--store", str(store)]) == cli.EXIT_WAITING == 4
    capsys.readouterr()


@pytest.mark.parametrize(
    "failed", [JobState.REJECTED, JobState.FAILED, JobState.OPERATOR_REJECTED, JobState.EXPIRED]
)
def test_run_exits_1_when_rejected_and_waiting(
    tmp_path: Path, monkeypatch: Any, capsys: Any, failed: JobState
) -> None:
    _stub_runner(monkeypatch, _snapshot(tmp_path, failed, JobState.WAITING))
    store = tmp_path / "s.sqlite3"
    assert cli.main(["run", "jobs.yaml", "--store", str(store)]) == cli.EXIT_FAILURE
    capsys.readouterr()


def test_resume_exits_4_when_still_waiting(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    _stub_runner(monkeypatch, _snapshot(tmp_path, JobState.WAITING))
    store = tmp_path / "s.sqlite3"
    assert cli.main(["resume", "cli-run", "--store", str(store)]) == cli.EXIT_WAITING
    capsys.readouterr()
    _stub_runner(monkeypatch, _snapshot(tmp_path, JobState.ACCEPTED))
    assert cli.main(["resume", "cli-run", "--store", str(store)]) == cli.EXIT_OK
    _stub_runner(monkeypatch, _snapshot(tmp_path, JobState.PENDING))
    assert cli.main(["resume", "cli-run", "--store", str(store)]) == cli.EXIT_FAILURE


def test_epilog_documents_the_waiting_exit(capsys: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "4 --" in text and "waiting" in text and "resolve" in text
    with pytest.raises(SystemExit):
        cli.main(["resolve", "--help"])
    assert "--reject" in capsys.readouterr().out


# --------------------------------------------------------------------------
# status: read-only
# --------------------------------------------------------------------------


def test_status_reports_interrupts_and_lapse_read_only(tmp_path: Path, capsys: Any) -> None:
    # Created at epoch 1000 with a 1 second expiry: lapsed by the real clock.
    path, _store = _waiting_ledger(tmp_path, expires_in_s=1)
    tables = {name: _count(path, name) for name in ("events", "interrupt_resolutions", "jobs")}
    version = _version(path)

    assert cli.main(["status", "run", "--store", str(path)]) == cli.EXIT_OK
    payload = _out(capsys)

    job = next(item for item in payload["jobs"] if item["id"] == "job")
    assert job["state"] == "waiting"
    assert job["effective_state"] == "expired"
    (interrupt,) = payload["interrupts"]
    assert interrupt["lapsed"] is True and interrupt["resolution"] is None
    assert interrupt["payload"] == {"action": "deploy"}
    assert payload["continuations"] == []
    assert isinstance(payload["read_at"], float)
    other = next(item for item in payload["jobs"] if item["id"] == "other")
    assert other["effective_state"] == other["state"] == "pending"
    assert {name: _count(path, name) for name in tables} == tables
    assert _version(path) == version


@pytest.mark.parametrize("version", [10, 11])
def test_status_leaves_older_ledger_unmigrated(tmp_path: Path, capsys: Any, version: int) -> None:
    path = _ledger_at(tmp_path, version)
    before = _digest(path)
    assert cli.main(["status", "run", "--store", str(path)]) == cli.EXIT_OK
    payload = _out(capsys)
    assert payload["interrupts"] == [] and payload["continuations"] == []
    assert _version(path) == version
    assert _digest(path) == before
    assert _journal_mode(path) == "wal"


def test_status_leaves_schema_11_ledger_unmigrated(tmp_path: Path, capsys: Any) -> None:
    path = _ledger_at(tmp_path, 11)
    before = _digest(path)
    assert cli.main(["status", "run", "--store", str(path)]) == cli.EXIT_OK
    capsys.readouterr()
    assert _version(path) == 11
    assert _digest(path) == before


def test_status_leaves_non_wal_ledger_non_wal(tmp_path: Path, capsys: Any) -> None:
    path = _ledger_at(tmp_path, len(_MIGRATIONS), wal=False)
    assert _journal_mode(path) == "delete"
    before = _digest(path)
    assert cli.main(["status", "run", "--store", str(path)]) == cli.EXIT_OK
    capsys.readouterr()
    assert _journal_mode(path) == "delete"
    assert _digest(path) == before


def test_status_leaves_wal_schema_12_ledger_unchanged(tmp_path: Path, capsys: Any) -> None:
    # A lapsed interrupt is the case a writing status would record.
    path, _store = _waiting_ledger(tmp_path, expires_in_s=1)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    assert _journal_mode(path) == "wal"
    before = _digest(path)
    events = _count(path, "events")
    assert cli.main(["status", "run", "--store", str(path)]) == cli.EXIT_OK
    capsys.readouterr()
    assert _digest(path) == before
    assert _count(path, "events") == events
    assert _count(path, "interrupt_resolutions") == 0
    assert _journal_mode(path) == "wal"
    assert _version(path) == len(_MIGRATIONS)


def test_events_still_migrates_on_open(tmp_path: Path, capsys: Any) -> None:
    """Only `status` reads without migrating; the other verbs keep migrating."""
    path = _ledger_at(tmp_path, 11)
    assert _version(path) == 11
    cli.main(["events", "run", "--store", str(path)])
    capsys.readouterr()
    assert _version(path) == len(_MIGRATIONS)
