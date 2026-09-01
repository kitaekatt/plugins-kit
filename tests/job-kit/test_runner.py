"""Tests for contract acceptance, halt recording and resume."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest

from llm_scripting_kit.completion import (
    BackendSelection,
    Capabilities,
    HALT_RATE_LIMIT,
    HaltError,
    LLMResponse,
)

from job_kit.model import Contract, Job, JobState, Prompt
from job_kit.run import default_store_path, resume_run, run_contract, run_jobs
from job_kit.store import JobStore


class FakeBackend:
    """A hermetic backend with one call counter and typed failures."""

    name = "fake"

    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None) -> None:
        self.response = response or LLMResponse(
            text="model answer",
            model="fake-model",
            input_tokens=3,
            output_tokens=5,
            dropped_params=(),
            execution_controls_applied=(),
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-01T00:00:01Z",
        )
        self.error = error
        self.calls: list[tuple[str, str, str, object]] = []

    def complete(self, system: str, user: str, *, model: str, options: object = None) -> LLMResponse:
        """Record the seam call and return or raise the configured result."""
        self.calls.append((system, user, model, options))
        if self.error is not None:
            raise self.error
        return self.response

    def classify_halt(self, exc: BaseException) -> str | None:
        """Leave typed HaltError classification to the runner."""
        return None


def _job(tmp_path: Path, job_id: str = "job", exit_code: int = 0) -> Job:
    """Build a job whose contract uses the test interpreter."""
    code = f"import sys; print('contract'); sys.exit({exit_code})"
    return Job(
        id=job_id,
        prompt=Prompt(system="instructions", user=job_id),
        endpoint_preference=("fake-endpoint",),
        directory=tmp_path,
        contract=Contract(command=(sys.executable, "-c", code), directory=tmp_path),
    )


def _contract_job(tmp_path: Path, job_id: str, command: tuple[str, ...]) -> Job:
    """Build a fake-backed job with an explicitly supplied contract command."""
    return Job(
        id=job_id,
        prompt=Prompt(system="instructions", user=job_id),
        endpoint_preference=("fake-endpoint",),
        directory=tmp_path,
        contract=Contract(command=command, directory=tmp_path),
    )


def _advertisement() -> dict[str, Capabilities]:
    """Return the only capability record used by fake runs."""
    return {"fake": Capabilities(adapter="fake")}


def _factory_for(backend: FakeBackend) -> Callable[..., BackendSelection]:
    """Return a factory that accepts the runner's project-root argument."""
    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    return factory


def test_default_store_path_uses_ephemeral_project_data(tmp_path: Path) -> None:
    """The default ledger follows the bootstrap .local-data convention."""
    assert default_store_path(tmp_path) == (
        tmp_path / ".local-data" / "plugins-kit" / "job-kit" / "runs.sqlite3"
    )


def test_acceptance_exit_zero_accepts_and_nonzero_rejects(tmp_path: Path) -> None:
    """The contract records both exit-code paths and captured output."""
    accepted = run_contract(
        _job(tmp_path, "accept", 0).contract,
        directory=tmp_path,
    )
    rejected = run_contract(
        _job(tmp_path, "reject", 7).contract,
        directory=tmp_path,
    )
    assert accepted.exit_code == 0
    assert accepted.accepted is True
    assert accepted.outcome == "observed"
    assert "contract" in accepted.stdout
    assert rejected.exit_code == 7
    assert rejected.accepted is False
    assert rejected.outcome == "observed"
    assert "contract" in rejected.stdout


def test_acceptance_timeout_is_recorded_as_a_non_accepting_result(
    tmp_path: Path,
) -> None:
    """A hanging contract is bounded by the attempt timeout."""
    contract = Contract(
        command=(
            sys.executable,
            "-c",
            "while True: pass",
        ),
        directory=tmp_path,
    )

    result = run_contract(contract, timeout_s=0.01)

    assert result.exit_code is None
    assert result.accepted is False
    assert result.outcome == "timed_out"


def test_acceptance_not_run_records_the_launch_error(tmp_path: Path) -> None:
    """A contract that cannot launch is distinct from a timed-out check."""
    missing = tmp_path / "missing-contract-command"
    result = run_contract(
        Contract(command=(str(missing),), directory=tmp_path),
        directory=tmp_path,
    )

    assert result.exit_code is None
    assert result.accepted is False
    assert result.outcome == "not_run"
    assert result.stderr
    assert result.to_mapping()["outcome"] == "not_run"


def test_acceptance_output_is_recorded_as_a_tail(tmp_path: Path) -> None:
    """Large contract output is bounded while retaining its tail."""
    contract = Contract(
        command=(sys.executable, "-c", "print('x' * 3000)"),
        directory=tmp_path,
    )

    result = run_contract(contract)

    assert len(result.stdout) <= 2000
    assert result.stdout.endswith("x\n")


def test_runner_sets_timeout_and_records_truthful_response(tmp_path: Path) -> None:
    """One successful seam call produces one accepted attempt row."""
    backend = FakeBackend()
    snapshot = run_jobs(
        [_job(tmp_path)],
        tmp_path / "run.sqlite3",
        timeout_s=17.0,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].status == "completed"
    assert snapshot.attempts[0].usage is not None
    assert snapshot.attempts[0].usage.input_tokens == 3
    assert snapshot.attempts[0].acceptance is not None
    assert snapshot.attempts[0].acceptance.exit_code == 0
    assert len(backend.calls) == 1
    assert backend.calls[0][3].timeout_s == 17.0


def test_runner_marks_not_run_failed_and_timeout_rejected(tmp_path: Path) -> None:
    """The runner treats an unavailable check as failure, but a timeout as rejection."""
    backend = FakeBackend()
    not_run = run_jobs(
        [
            _contract_job(
                tmp_path,
                "not-run",
                (str(tmp_path / "missing-contract-command"),),
            )
        ],
        tmp_path / "not-run.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert not_run.jobs[0].state is JobState.FAILED
    assert not_run.attempts[0].acceptance is not None
    assert not_run.attempts[0].acceptance.outcome == "not_run"

    timed_out = run_jobs(
        [
            _contract_job(
                tmp_path,
                "timed-out",
                (sys.executable, "-c", "while True: pass"),
            )
        ],
        tmp_path / "timed-out.sqlite3",
        timeout_s=0.01,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert timed_out.jobs[0].state is JobState.REJECTED
    assert timed_out.attempts[0].acceptance is not None
    assert timed_out.attempts[0].acceptance.outcome == "timed_out"


def test_typed_halt_is_recorded_without_contract_invocation(tmp_path: Path) -> None:
    """A persistent typed halt is stored as halt data, not parsed text."""
    backend = FakeBackend(error=HaltError(HALT_RATE_LIMIT, "quota"))
    snapshot = run_jobs(
        [_job(tmp_path)],
        tmp_path / "halt.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    attempt = snapshot.attempts[0]
    assert snapshot.jobs[0].state is JobState.HALTED
    assert attempt.halt_kind == HALT_RATE_LIMIT
    assert attempt.error_code == HALT_RATE_LIMIT
    assert attempt.acceptance is None
    assert len(backend.calls) == 1


def test_unroutable_job_is_terminal_with_reason_and_survives_resume(
    tmp_path: Path,
) -> None:
    """Selection failure is durable without pretending the seam was invoked."""
    backend = FakeBackend()
    factory_calls: list[str] = []

    def unroutable_factory(endpoint: str, **_: object) -> BackendSelection:
        factory_calls.append(endpoint)
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    def no_capabilities() -> dict[str, Capabilities]:
        return {}

    snapshot = run_jobs(
        [_job(tmp_path, "unroutable")],
        tmp_path / "unroutable.sqlite3",
        run_id="unroutable-run",
        capabilities_provider=no_capabilities,
        backend_factory=unroutable_factory,
    )
    record = snapshot.jobs[0]
    reason = "job 'unroutable' has no compatible endpoint in preference order ['fake-endpoint']"
    assert record.state is JobState.UNROUTABLE
    assert record.error == reason
    assert snapshot.to_mapping()["jobs"][0]["error"] == reason
    assert snapshot.counts[JobState.UNROUTABLE.value] == 1
    assert snapshot.attempts == ()
    assert factory_calls == ["fake-endpoint"]

    def should_not_select() -> dict[str, Capabilities]:
        raise AssertionError("terminal unroutable job was selected on resume")

    resumed = resume_run(
        "unroutable-run",
        tmp_path / "unroutable.sqlite3",
        capabilities_provider=should_not_select,
        backend_factory=unroutable_factory,
    )
    assert resumed.jobs[0].state is JobState.UNROUTABLE
    assert resumed.jobs[0].error == reason
    assert resumed.attempts == ()
    assert factory_calls == ["fake-endpoint"]


def test_two_job_run_resumes_after_interruption_before_next_seam_call(tmp_path: Path) -> None:
    """Accepted work is skipped and the pending job runs once after resume."""
    first = _job(tmp_path, "first")
    second = _job(tmp_path, "second")
    db_path = tmp_path / "resume.sqlite3"
    run_id = "resume-demo"
    factory_calls = 0
    backend = FakeBackend()

    def interrupting_factory(endpoint: str, **_: object) -> BackendSelection:
        nonlocal factory_calls
        factory_calls += 1
        if factory_calls == 2:
            raise KeyboardInterrupt
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [first, second],
            db_path,
            run_id=run_id,
            capabilities_provider=_advertisement,
            backend_factory=interrupting_factory,
        )

    interrupted = JobStore(db_path).snapshot(run_id)
    assert interrupted.jobs[0].state is JobState.ACCEPTED
    assert interrupted.jobs[1].state is JobState.PENDING
    assert len(interrupted.attempts) == 1

    resumed = resume_run(
        run_id,
        db_path,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )
    assert [job.state for job in resumed.jobs] == [JobState.ACCEPTED, JobState.ACCEPTED]
    assert [attempt.job_id for attempt in resumed.attempts] == ["first", "second"]
    assert len(backend.calls) == 2
