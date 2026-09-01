"""Tests for the durable job ledger."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from job_kit.model import Acceptance, Attempt, AttemptError, Contract, Job, JobState, Prompt, Usage
from job_kit.store import JobStore, StoreNotFoundError, TerminalStateError


def _job(directory: Path, job_id: str = "job") -> Job:
    """Build a small durable job for store tests."""
    return Job(
        id=job_id,
        prompt=Prompt(user="run the task"),
        endpoint_preference=("fake",),
        directory=directory,
        contract=Contract(command=("true",), directory=directory),
    )


def _attempt(run_id: str, job_id: str, directory: Path) -> Attempt:
    """Build a completed attempt with all durable fields populated."""
    return Attempt(
        run_id=run_id,
        job_id=job_id,
        attempt_no=1,
        endpoint="fake",
        backend="fake",
        model="fake-model",
        status="completed",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:00:01Z",
        dropped_params=(),
        execution_controls_applied=(),
        usage=Usage(input_tokens=4, output_tokens=6, cache_hit_tokens=0),
        response_text="done",
        workspace=directory,
        base_ref="base-ref",
        workspace_status="isolated",
        acceptance=Acceptance(
            command=("true",),
            directory=directory,
            exit_code=0,
            stdout="",
            stderr="",
            wall_ms=2,
            accepted=True,
        ),
    )


def test_store_round_trip_and_terminal_refusal(tmp_path: Path) -> None:
    """A fresh store instance reads the same run and refuses terminal writes."""
    db_path = tmp_path / "run.sqlite3"
    store = JobStore(db_path)
    job = _job(tmp_path)
    store.create_run("run-1", [job], created_at=10.0, workspace_root=tmp_path / "workspaces")
    store.mark_running("run-1", job.id, at=11.0)

    stored_attempt = store.append_attempt(
        _attempt("run-1", job.id, tmp_path), terminal_state=JobState.ACCEPTED, at=12.0
    )
    assert stored_attempt.id == 1

    reopened = JobStore(db_path)
    snapshot = reopened.snapshot("run-1")
    assert snapshot.run.status.value == "completed"
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert snapshot.attempts[0].usage is not None
    assert snapshot.attempts[0].usage.input_tokens == 4
    assert snapshot.attempts[0].acceptance is not None
    assert snapshot.attempts[0].acceptance.exit_code == 0
    assert snapshot.attempts[0].acceptance.outcome == "observed"
    assert snapshot.attempts[0].base_ref == "base-ref"
    assert snapshot.attempts[0].workspace_status == "isolated"

    with pytest.raises(TerminalStateError):
        reopened.mark_running("run-1", job.id)
    with pytest.raises(TerminalStateError):
        reopened.append_attempt(
            _attempt("run-1", job.id, tmp_path), terminal_state=JobState.ACCEPTED
        )


def test_unknown_usage_round_trips_as_null_not_zero(tmp_path: Path) -> None:
    """An attempt with no usage keeps SQL NULL values."""
    db_path = tmp_path / "run.sqlite3"
    store = JobStore(db_path)
    job = _job(tmp_path)
    store.create_run("run-2", [job])
    store.mark_running("run-2", job.id)
    attempt = Attempt(
        run_id="run-2",
        job_id=job.id,
        attempt_no=1,
        endpoint="fake",
        backend="fake",
        model="fake-model",
        status="error",
        started_at="2026-09-01T00:00:00Z",
        ended_at="2026-09-01T00:00:01Z",
        error=AttemptError(code="execution", message="unknown"),
        workspace=tmp_path,
    )
    store.append_attempt(attempt, terminal_state=JobState.FAILED)

    with sqlite3.connect(str(db_path)) as connection:
        row = connection.execute(
            "SELECT input_tokens, output_tokens, cache_hit_tokens, total_tokens "
            "FROM attempts"
        ).fetchone()
    assert row == (None, None, None, None)
    assert JobStore(db_path).snapshot("run-2").attempts[0].usage is None


def test_noncreating_store_requires_an_existing_database(tmp_path: Path) -> None:
    """Opening an absent store without creation has no filesystem side effects."""
    db_path = tmp_path / "missing" / "run.sqlite3"

    with pytest.raises(StoreNotFoundError, match="store does not exist"):
        JobStore(db_path, create=False)
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_store_migrates_the_job_error_column_from_schema_v1(tmp_path: Path) -> None:
    """An existing v1 ledger gains the job-level error column on open."""
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(str(db_path)) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                jobs_path TEXT,
                max_parallel INTEGER NOT NULL,
                workspace_root TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE jobs (
                run_id TEXT NOT NULL,
                id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (run_id, id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                backend TEXT NOT NULL,
                model TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                halt_kind TEXT,
                dropped_params_json TEXT,
                execution_controls_applied_json TEXT,
                started_at TEXT,
                ended_at TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_hit_tokens INTEGER,
                total_tokens INTEGER,
                response_text TEXT,
                workspace TEXT,
                acceptance_json TEXT
            )
            """
        )
        connection.execute("INSERT INTO schema_version(version) VALUES (1)")

    JobStore(db_path, create=False)

    with sqlite3.connect(str(db_path)) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        attempt_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(attempts)").fetchall()
        }
    assert "error_message" in columns
    assert {
        "base_ref",
        "workspace_status",
        "workspace_reason",
        "workspace_removed_at",
        "workspace_removal_forced",
    } <= attempt_columns


def test_store_connection_posture_is_applied_per_connection(tmp_path: Path) -> None:
    """WAL, timeout and foreign-key enforcement are visible on each open."""
    store = JobStore(tmp_path / "posture.sqlite3")
    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_effective_directory_is_persisted_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job without a directory keeps the creation cwd in its definition."""
    original_directory = Path.cwd().resolve()
    job = Job(
        id="implicit-directory",
        prompt=Prompt(user="run"),
        endpoint_preference=("fake",),
        contract=Contract(command=("true",)),
    )
    db_path = tmp_path / "directory.sqlite3"
    JobStore(db_path).create_run("directory-run", [job])

    other_directory = tmp_path / "other"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    stored_job = JobStore(db_path).list_jobs("directory-run")[0].job

    assert job.directory == original_directory
    assert stored_job.declared_directory == original_directory
