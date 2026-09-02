"""Tests for the durable job ledger."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from job_kit.model import Acceptance, Attempt, AttemptError, Contract, Job, JobState, Prompt, Usage
from job_kit.store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    JobStore,
    StoreNotFoundError,
    TerminalStateError,
)


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
        forwarded_params=("extras.top_k",),
        execution_controls_applied=(),
        usage=Usage(input_tokens=4, output_tokens=6, cache_hit_tokens=0),
        response_text="done",
        reasoning="internal reasoning",
        finish_reason="stop",
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
    job = replace(_job(tmp_path), options={"extras": {"sandbox": "read-only"}})
    store.create_run(
        "run-1",
        [job],
        created_at=10.0,
        workspace_root=tmp_path / "workspaces",
        disallowed_tools="Bash",
    )
    store.mark_running("run-1", job.id, at=11.0)

    stored_attempt = store.append_attempt(
        _attempt("run-1", job.id, tmp_path), terminal_state=JobState.ACCEPTED, at=12.0
    )
    assert stored_attempt.id == 1

    reopened = JobStore(db_path)
    snapshot = reopened.snapshot("run-1")
    assert snapshot.run.status.value == "completed"
    assert snapshot.run.disallowed_tools == "Bash"
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert snapshot.jobs[0].job.options == {"extras": {"sandbox": "read-only"}}
    assert snapshot.attempts[0].usage is not None
    assert snapshot.attempts[0].usage.input_tokens == 4
    assert snapshot.attempts[0].acceptance is not None
    assert snapshot.attempts[0].acceptance.exit_code == 0
    assert snapshot.attempts[0].acceptance.outcome == "observed"
    assert snapshot.attempts[0].base_ref == "base-ref"
    assert snapshot.attempts[0].workspace_status == "isolated"
    assert snapshot.attempts[0].forwarded_params == ("extras.top_k",)
    assert snapshot.attempts[0].reasoning == "internal reasoning"
    assert snapshot.attempts[0].finish_reason == "stop"

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
    stored_attempt = JobStore(db_path).snapshot("run-2").attempts[0]
    assert stored_attempt.usage is None
    assert stored_attempt.forwarded_params is None


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
        "forwarded_params_json",
        "reasoning",
        "finish_reason",
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


def test_create_run_accepts_a_worker_pool_bound_and_refuses_a_non_positive_one(
    tmp_path: Path,
) -> None:
    """The ledger records a bound above one and still validates the value."""
    store = JobStore(tmp_path / "bound.sqlite3")

    record = store.create_run("wide", [_job(tmp_path)], max_parallel=6)
    assert record.max_parallel == 6
    assert store.get_run("wide").max_parallel == 6

    for value in (0, -2, True, 1.5, "3"):
        with pytest.raises(ValueError, match="max_parallel must be a positive integer"):
            store.create_run(f"bad-{value!r}", [_job(tmp_path)], max_parallel=value)


def test_scale_busy_timeout_only_ever_raises_the_budget(tmp_path: Path) -> None:
    """A wider pool gets proportionally more wait budget, never less."""
    store = JobStore(tmp_path / "timeout.sqlite3")
    assert store.busy_timeout_ms == DEFAULT_BUSY_TIMEOUT_MS

    assert store.scale_busy_timeout(4) == DEFAULT_BUSY_TIMEOUT_MS * 4
    assert store.scale_busy_timeout(1) == DEFAULT_BUSY_TIMEOUT_MS * 4


def test_concurrent_append_attempt_keeps_per_job_numbering(tmp_path: Path) -> None:
    """Parallel writers over distinct jobs never collide on attempt numbers."""
    store = JobStore(tmp_path / "concurrent.sqlite3")
    job_ids = [f"job-{index}" for index in range(8)]
    store.create_run(
        "concurrent",
        [_job(tmp_path, job_id) for job_id in job_ids],
        max_parallel=len(job_ids),
    )
    store.scale_busy_timeout(len(job_ids))
    start = threading.Barrier(len(job_ids))
    failures: list[BaseException] = []

    def append(job_id: str) -> None:
        """Append two attempts for one job, starting with every other writer."""
        try:
            start.wait(timeout=30)
            for attempt_no in (1, 2):
                store.append_attempt(
                    replace(
                        _attempt("concurrent", job_id, tmp_path),
                        attempt_no=attempt_no,
                        acceptance=None,
                    )
                )
        except BaseException as exc:  # pragma: no cover - reported by the assert
            failures.append(exc)

    threads = [threading.Thread(target=append, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    for job_id in job_ids:
        numbers = [attempt.attempt_no for attempt in store.list_attempts("concurrent", job_id)]
        assert numbers == [1, 2]


def test_concurrent_writers_do_not_report_a_locked_database(tmp_path: Path) -> None:
    """Upfront write locking plus the busy timeout keeps every writer serving."""
    store = JobStore(tmp_path / "locking.sqlite3")
    job_ids = [f"job-{index}" for index in range(12)]
    store.create_run(
        "locking", [_job(tmp_path, job_id) for job_id in job_ids], max_parallel=12
    )
    store.scale_busy_timeout(12)
    start = threading.Barrier(len(job_ids))
    errors: list[BaseException] = []

    def write(job_id: str) -> None:
        """Take the write lock repeatedly against every other writer."""
        try:
            start.wait(timeout=30)
            for _ in range(10):
                store.mark_running("locking", job_id)
        except BaseException as exc:  # pragma: no cover - reported by the assert
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(job_id,)) for job_id in job_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert [str(error) for error in errors] == []


def test_concurrent_store_construction_migrates_once(tmp_path: Path) -> None:
    """Many simultaneous JobStore constructions converge on one schema."""
    db_path = tmp_path / "migrate.sqlite3"
    start = threading.Barrier(8)
    stores: list[JobStore] = []
    errors: list[BaseException] = []

    def construct() -> None:
        """Open the same fresh ledger as every other thread at once."""
        try:
            start.wait(timeout=30)
            stores.append(JobStore(db_path))
        except BaseException as exc:  # pragma: no cover - reported by the assert
            errors.append(exc)

    threads = [threading.Thread(target=construct) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert [str(error) for error in errors] == []
    assert len(stores) == 8
    with sqlite3.connect(str(db_path)) as connection:
        versions = connection.execute("SELECT version FROM schema_version").fetchall()
    assert len(versions) == 1
