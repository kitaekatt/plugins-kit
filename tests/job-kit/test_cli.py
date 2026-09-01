"""Tests for the status CLI surface without a model transport."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from job_kit import cli
from job_kit.model import Contract, Job, JobRecord, JobState, Prompt, RunRecord, RunSnapshot, RunState
from job_kit.store import JobStore


def test_status_reads_a_run_from_an_explicit_store(
    tmp_path: Path, capsys: Any
) -> None:
    """The status verb emits the durable run snapshot as JSON."""
    store_path = tmp_path / "status.sqlite3"
    job = Job(
        id="status-job",
        prompt=Prompt(user="status"),
        endpoint_preference=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    JobStore(store_path).create_run("status-run", [job])

    assert cli.main(["status", "status-run", "--store", str(store_path)]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == "status-run"
    assert payload["jobs"][0]["id"] == "status-job"
    assert payload["store"] == str(store_path.resolve())


def test_status_does_not_create_a_missing_store(tmp_path: Path, capsys: Any) -> None:
    """The read-only status verb reports a missing store without creating it."""
    store_path = tmp_path / "missing" / "status.sqlite3"

    assert cli.main(["status", "run-1", "--store", str(store_path)]) == cli.EXIT_RUNNER_FAILURE
    captured = capsys.readouterr()
    assert f"store does not exist: {store_path.resolve()}" in captured.err
    assert "unknown run" not in captured.err.lower()
    assert not store_path.exists()
    assert not store_path.parent.exists()


def _accepted_snapshot(tmp_path: Path) -> RunSnapshot:
    """Build a small accepted snapshot for CLI transport tests."""
    job = Job(
        id="cli-job",
        prompt=Prompt(user="run"),
        endpoint_preference=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    record = JobRecord(job=job, state=JobState.ACCEPTED, created_at=0.0, updated_at=0.0)
    run = RunRecord(
        id="cli-run",
        created_at=0.0,
        jobs_path=None,
        max_parallel=1,
        workspace_root=None,
        status=RunState.COMPLETED,
    )
    return RunSnapshot(run=run, jobs=(record,), attempts=())


def test_run_and_resume_cli_delegate_to_runner(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The two mutating CLI verbs pass through without a live backend."""
    snapshot = _accepted_snapshot(tmp_path)
    store_path = tmp_path / "cli.sqlite3"
    calls: list[tuple[str, Path]] = []

    def fake_run(
        jobs_path: Path, *, store_path: Path, timeout_s: float
    ) -> RunSnapshot:
        calls.append(("run", store_path))
        return snapshot

    def fake_resume(
        run_id: str, store: Path, *, timeout_s: float
    ) -> RunSnapshot:
        calls.append(("resume", store))
        return snapshot

    monkeypatch.setattr(cli, "run_job_file", fake_run)
    monkeypatch.setattr(cli, "resume_run", fake_resume)

    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_OK
    assert cli.main(["resume", "cli-run", "--store", str(store_path)]) == cli.EXIT_OK
    capsys.readouterr()
    assert calls == [("run", store_path.resolve()), ("resume", store_path.resolve())]


def test_cli_exit_codes_distinguish_job_outcome_from_runner_failure(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """Accepted, not-accepted, and runner-error results have distinct codes."""
    accepted = _accepted_snapshot(tmp_path)
    rejected = replace(
        accepted,
        jobs=(replace(accepted.jobs[0], state=JobState.REJECTED),),
    )
    current = accepted
    store_path = tmp_path / "exit-codes.sqlite3"

    def fake_run(
        jobs_path: Path, *, store_path: Path, timeout_s: float
    ) -> RunSnapshot:
        return current

    monkeypatch.setattr(cli, "run_job_file", fake_run)
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_OK
    capsys.readouterr()

    current = rejected
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_FAILURE
    capsys.readouterr()

    def broken_run(
        jobs_path: Path, *, store_path: Path, timeout_s: float
    ) -> RunSnapshot:
        raise RuntimeError("runner boom")

    monkeypatch.setattr(cli, "run_job_file", broken_run)
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_RUNNER_FAILURE
    assert "runner boom" in capsys.readouterr().err
