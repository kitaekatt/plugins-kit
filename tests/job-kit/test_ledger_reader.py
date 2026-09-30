"""Tests for LedgerReader: the read-only way to open a job-kit ledger.

It opens with ``mode=ro`` and ``PRAGMA query_only``, sets no journal mode,
never migrates, reads ledger schemas 10 to 12, and reads one snapshot in ONE
transaction. Only the stdlib ``sqlite3`` and ``tmp_path`` are used, so every
case runs on Windows too.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from job_kit.interrupts import REQUEST_ENVELOPE_V1
from job_kit.model import (
    Acceptance,
    Attempt,
    Contract,
    InterruptRequest,
    Job,
    JobState,
    Prompt,
)
from job_kit.store import (
    JobStore,
    LedgerReader,
    StoreError,
    StoreNotFoundError,
    UnknownRunError,
    _MIGRATIONS,
)


CREATED = 1000.0


def _job(directory: Path, job_id: str = "job") -> Job:
    return Job(
        id=job_id,
        prompt=Prompt(user=f"run {job_id}"),
        models=("fake",),
        directory=directory,
        max_attempts=2,
        contract=Contract(command=(sys.executable, "-c", "pass"), directory=directory),
    )


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


def _current_ledger(tmp_path: Path) -> tuple[Path, JobStore]:
    """A schema-12 ledger holding a waiting job with an interrupt."""
    path = tmp_path / "current.sqlite3"
    store = JobStore(path)
    store.create_run("run", [_job(tmp_path, "job"), _job(tmp_path, "other")])
    reservation = store.reserve_attempt(
        "run",
        "job",
        endpoint="e",
        backend="b",
        model="m",
        reserved_at="2026-09-01T00:00:00Z",
    )
    store.arm_reservation("run", "job", reservation.attempt_no, invoke_armed_at="2026-09-01T00:00:01Z")
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
            request_schema={"type": "object"},
            payload={"action": "deploy"},
            expires_in_s=60,
        ),
        at=CREATED,
    )
    return path, store


def _checkpoint(path: Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


@pytest.mark.parametrize("attempt", ["insert", "pragma_journal_mode", "file_mode"])
def test_reader_cannot_write(tmp_path: Path, attempt: str) -> None:
    path, _store = _current_ledger(tmp_path)
    _checkpoint(path)
    before = _digest(path)
    reader = LedgerReader(path)
    with reader._connect() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            if attempt == "insert":
                connection.execute(
                    "INSERT INTO runs(id, created_at, max_parallel) VALUES ('x', 1.0, 1)"
                )
            elif attempt == "pragma_journal_mode":
                connection.execute("PRAGMA journal_mode = DELETE").fetchall()
            else:
                # Even with query_only lifted, the file itself is open read-only.
                connection.execute("PRAGMA query_only = OFF")
                connection.execute(
                    "INSERT INTO runs(id, created_at, max_parallel) VALUES ('x', 1.0, 1)"
                )
    assert _journal_mode(path) == "wal"
    assert _digest(path) == before
    assert JobStore(path).get_run("x") is None


def test_reader_leaves_schema_11_ledger_at_11(tmp_path: Path) -> None:
    path = _ledger_at(tmp_path, 11)
    before = _digest(path)
    snapshot = LedgerReader(path).snapshot("run", now=5.0)
    assert snapshot.jobs[0].state is JobState.PENDING
    assert _version(path) == 11
    assert _digest(path) == before


def test_reader_leaves_non_wal_schema_12_ledger_non_wal(tmp_path: Path) -> None:
    path, _store = _current_ledger(tmp_path)
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.close()
    assert _journal_mode(path) == "delete"
    before = _digest(path)
    snapshot = LedgerReader(path).snapshot("run", now=CREATED + 1)
    assert snapshot.interrupts[0].kind == "approval"
    assert _journal_mode(path) == "delete"
    assert _digest(path) == before
    assert _version(path) == len(_MIGRATIONS)


def test_reader_reads_wal_ledger_without_changing_main_file(tmp_path: Path) -> None:
    path, _store = _current_ledger(tmp_path)
    _checkpoint(path)
    assert _journal_mode(path) == "wal"
    before = _digest(path)
    snapshot = LedgerReader(path).snapshot("run", now=CREATED + 1)
    assert snapshot.jobs[0].state is JobState.WAITING
    assert len(snapshot.interrupts) == 1
    assert _digest(path) == before
    assert _journal_mode(path) == "wal"
    assert _version(path) == len(_MIGRATIONS)


def test_reader_sees_uncheckpointed_wal_commit_beside_open_writer(tmp_path: Path) -> None:
    path, _store = _current_ledger(tmp_path)
    _checkpoint(path)
    before = _digest(path)
    writer = sqlite3.connect(str(path), isolation_level=None)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE jobs SET state = 'unroutable' WHERE id = 'other'")
        writer.execute("COMMIT")
        # The commit lives only in the -wal file; the main file is untouched.
        assert _digest(path) == before
        assert Path(str(path) + "-wal").stat().st_size > 0
        snapshot = LedgerReader(path).snapshot("run", now=CREATED + 1)
        states = {job.id: job.state for job in snapshot.jobs}
        assert states == {"job": JobState.WAITING, "other": JobState.UNROUTABLE}
        assert _digest(path) == before
        version = writer.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == len(_MIGRATIONS)
    finally:
        writer.close()


def test_reader_version_and_tables_share_one_snapshot(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A writer that migrates the ledger and changes a job between the
    version read and the table reads is invisible to the snapshot."""
    path = _ledger_at(tmp_path, 11)
    real = LedgerReader._checked_version

    def version_then_concurrent_migration(connection: sqlite3.Connection) -> int:
        version = real(connection)
        store = JobStore(path, create=False)  # migrates 11 -> 12 and commits
        with store._writer() as writer:
            writer.execute("UPDATE jobs SET state = 'unroutable' WHERE id = 'job'")
        return version

    monkeypatch.setattr(
        LedgerReader, "_checked_version", staticmethod(version_then_concurrent_migration)
    )
    snapshot = LedgerReader(path).snapshot("run", now=5.0)
    monkeypatch.undo()
    assert snapshot.jobs[0].state is JobState.PENDING
    assert snapshot.interrupts == () and snapshot.continuations == ()
    assert _version(path) == len(_MIGRATIONS)  # the migration did commit
    assert LedgerReader(path).snapshot("run").jobs[0].state is JobState.UNROUTABLE


@pytest.mark.parametrize("version", [10, 11])
def test_reader_snapshot_of_schema_10_and_11_has_no_interrupts(
    tmp_path: Path, version: int
) -> None:
    path = _ledger_at(tmp_path, version)
    snapshot = LedgerReader(path).snapshot("run", now=5.0)
    assert snapshot.interrupts == () and snapshot.continuations == ()
    assert [job.id for job in snapshot.jobs] == ["job"]
    assert snapshot.to_mapping()["jobs"][0]["effective_state"] == "pending"
    assert _version(path) == version


@pytest.mark.parametrize("version", [9, 13])
def test_reader_refuses_unsupported_schema(tmp_path: Path, version: int) -> None:
    if version < len(_MIGRATIONS):
        path = _ledger_at(tmp_path, version)
    else:
        path, _store = _current_ledger(tmp_path)
        connection = sqlite3.connect(str(path))
        connection.execute("UPDATE schema_version SET version = ?", (version,))
        connection.commit()
        connection.close()
    before = _digest(path)
    with pytest.raises(StoreError) as excinfo:
        LedgerReader(path).snapshot("run")
    message = str(excinfo.value)
    assert f"version {version}" in message
    if version < len(_MIGRATIONS):
        assert "job-kit resume <run-id>" in message
    else:
        assert "update job-kit" in message
    assert _version(path) == version
    assert _digest(path) == before


def test_reader_snapshot_equals_store_snapshot(tmp_path: Path) -> None:
    path, store = _current_ledger(tmp_path)
    [interrupt] = store.list_interrupts("run")
    store.resolve_interrupt("run", interrupt.id, decision="answer", input={}, now=CREATED + 1)
    store.begin_continuation("run", "job", now=CREATED + 2)
    expected = store.snapshot("run", now=CREATED + 3)
    actual = LedgerReader(path).snapshot("run", now=CREATED + 3)
    assert actual == expected
    assert actual.continuations and actual.interrupts[0].resolution is not None
    assert actual.to_mapping() == expected.to_mapping()


def test_reader_refuses_missing_store_and_unknown_run(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite3"
    with pytest.raises(StoreNotFoundError):
        LedgerReader(missing)
    assert not missing.exists()
    path, _store = _current_ledger(tmp_path)
    with pytest.raises(UnknownRunError):
        LedgerReader(path).snapshot("no-such-run")
