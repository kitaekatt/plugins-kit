"""Tests for contract acceptance, halt recording and resume."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence

import pytest

import job_kit.run as run_module

from llm_scripting_kit.completion import (
    AgentTimeoutError,
    BackendSelection,
    Capabilities,
    EmptyCompletionError,
    ExecutionControl,
    HALT_RATE_LIMIT,
    HaltError,
    LLMResponse,
)
from llm_scripting_kit.completion.adapter_capabilities import CODEX_CAPABILITIES

from job_kit.model import Contract, ContractContext, Job, JobState, Prompt
from job_kit.run import (
    default_store_path,
    resume_run,
    run_contract,
    run_job_file,
    run_jobs,
    HALT_UNREACHABLE,
)
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


class SequenceBackend(FakeBackend):
    """A fake backend whose one-call outcomes are consumed in order."""

    def __init__(self, outcomes: Sequence[object]) -> None:
        super().__init__()
        self.outcomes = list(outcomes)

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: object = None,
    ) -> LLMResponse:
        """Return or raise exactly the next configured outcome."""
        self.calls.append((system, user, model, options))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return self.response
        if isinstance(outcome, LLMResponse):
            return outcome
        raise TypeError(f"unsupported fake outcome: {outcome!r}")


class TimeoutSequenceBackend(SequenceBackend):
    """A fake backend whose classifier gives timeouts the upstream halt label."""

    def classify_halt(self, exc: BaseException) -> str | None:
        """Mirror the seam mapping that job-kit must override for its deadline."""
        return HALT_RATE_LIMIT


class MarkerSequenceBackend(SequenceBackend):
    """A sequence backend that makes acceptance change on a later attempt."""

    def __init__(self, outcomes: Sequence[object], marker: Path) -> None:
        super().__init__(outcomes)
        self.marker = marker

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: object = None,
    ) -> LLMResponse:
        """Write rejection first and acceptance on the third seam call."""
        call_number = len(self.calls) + 1
        if call_number == 1:
            self.marker.write_text("reject", encoding="utf-8")
        elif call_number == 3:
            self.marker.write_text("accept", encoding="utf-8")
        return super().complete(
            system,
            user,
            model=model,
            options=options,
        )


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


def _deny_advertisement() -> dict[str, Capabilities]:
    """Return fake capabilities that can emit the deny-floor control."""
    return {
        "fake": Capabilities(
            adapter="fake",
            execution_controls=(
                ExecutionControl(
                    id="disallowed-tools",
                    emits="fake deny control",
                    effect="deny",
                    source="request",
                    parameter="disallowed_tools",
                ),
            ),
        )
    }


def _factory_for(backend: FakeBackend) -> Callable[..., BackendSelection]:
    """Return a factory that accepts the runner's project-root argument."""
    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "fake", backend, "fake-model")

    return factory


def _agent_timeout() -> AgentTimeoutError:
    """Build a typed timeout with the seam exception's required diagnostics."""
    return AgentTimeoutError(
        "job-kit deadline exceeded",
        cmd=["fake-agent"],
        elapsed_s=17,
        stdout="",
        stderr="",
    )


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


def test_contract_receives_completion_text_and_attempt_context(
    tmp_path: Path,
) -> None:
    """The contract subprocess receives stdin and all six JOB_KIT_* values."""
    command = (
        sys.executable,
        "-c",
        "import os, sys; print(sys.stdin.read(), end=''); print('|'.join("
        "os.environ[name] for name in ('JOB_KIT_RUN_ID', 'JOB_KIT_JOB_ID', "
        "'JOB_KIT_ATTEMPT_NO', 'JOB_KIT_ENDPOINT', 'JOB_KIT_BACKEND', "
        "'JOB_KIT_MODEL')))"
    )
    job = replace(
        _job(tmp_path),
        contract=Contract(command=command, directory=tmp_path),
    )

    snapshot = run_jobs(
        [job],
        tmp_path / "context.sqlite3",
        run_id="context-run",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(FakeBackend()),
    )

    acceptance = snapshot.attempts[0].acceptance
    assert acceptance is not None
    assert acceptance.stdout == (
        "model answer"
        "context-run|job|1|fake-endpoint|fake|fake-model\n"
    )


def test_run_contract_exports_context_over_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Context values replace only the six reserved variables."""
    monkeypatch.setenv("JOB_KIT_MODEL", "inherited")
    contract = Contract(
        command=(
            sys.executable,
            "-c",
            "import os, sys; print(sys.stdin.read(), end=''); print(os.environ['JOB_KIT_MODEL'])",
        ),
        directory=tmp_path,
    )
    result = run_contract(
        contract,
        response_text="completion",
        context=ContractContext("run", "job", 2, "endpoint", "backend", "model"),
    )

    assert result.accepted is True
    assert result.stdout == "completionmodel\n"


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


def test_runner_carries_forwarded_params_through_acceptance(
    tmp_path: Path,
) -> None:
    """The response field remains present after the contract is attached."""
    backend = FakeBackend(
        response=LLMResponse(
            text="model answer",
            model="fake-model",
            dropped_params=("temperature",),
            forwarded_params=("extras.top_k",),
            execution_controls_applied=("sandbox-mode",),
            reasoning="internal reasoning",
            finish_reason="stop",
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-01T00:00:01Z",
        )
    )

    snapshot = run_jobs(
        [_job(tmp_path)],
        tmp_path / "forwarded.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    attempt = snapshot.attempts[0]
    assert attempt.dropped_params == ("temperature",)
    assert attempt.forwarded_params == ("extras.top_k",)
    assert attempt.execution_controls_applied == ("sandbox-mode",)
    assert attempt.reasoning == "internal reasoning"
    assert attempt.finish_reason == "stop"


def test_job_file_floor_reaches_the_run(tmp_path: Path) -> None:
    """The top-level job-file floor reaches the seam and the run ledger."""
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        """disallowed_tools: Bash
jobs:
  - id: file-floor
    prompt: run
    endpoint_preference: [fake-endpoint]
    directory: .
    contract:
      command: [true]
""",
        encoding="utf-8",
    )
    backend = FakeBackend()

    snapshot = run_job_file(
        jobs_path,
        store_path=tmp_path / "job-file.sqlite3",
        capabilities_provider=_deny_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert backend.calls[0][3].disallowed_tools == "Bash"
    assert snapshot.run.disallowed_tools == "Bash"


def test_run_deny_floor_beats_a_job_allow_list(tmp_path: Path) -> None:
    """The floor remains denied when a job allows the same tool."""
    backend = FakeBackend()
    job = replace(_job(tmp_path), options={"allowed_tools": "Bash"})

    snapshot = run_jobs(
        [job],
        tmp_path / "floor.sqlite3",
        disallowed_tools="Bash",
        capabilities_provider=_deny_advertisement,
        backend_factory=_factory_for(backend),
    )

    options = backend.calls[0][3]
    assert options.allowed_tools == "Bash"
    assert options.disallowed_tools == "Bash"
    assert snapshot.run.disallowed_tools == "Bash"


def test_job_disallowed_tools_are_added_to_the_run_floor(tmp_path: Path) -> None:
    """A job deny-list extends the floor and cannot replace it."""
    backend = FakeBackend()
    job = replace(_job(tmp_path), options={"disallowed_tools": "Edit"})

    run_jobs(
        [job],
        tmp_path / "union.sqlite3",
        disallowed_tools="Bash",
        capabilities_provider=_deny_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert backend.calls[0][3].disallowed_tools == "Bash Edit"


def test_run_floor_rejects_a_backend_that_drops_it(tmp_path: Path) -> None:
    """A concrete deny floor cannot run through an adapter that drops it."""
    backend = FakeBackend()
    backend.name = "codex-cli"

    def codex_advertisement() -> dict[str, Capabilities]:
        return {"codex-cli": CODEX_CAPABILITIES}

    snapshot = run_jobs(
        [_job(tmp_path)],
        tmp_path / "unsupported-floor.sqlite3",
        disallowed_tools="Bash",
        capabilities_provider=codex_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.UNROUTABLE
    assert snapshot.attempts == ()
    assert backend.calls == []


def test_job_extras_reach_a_codex_shaped_backend(tmp_path: Path) -> None:
    """The job options channel passes codex-specific extras to the backend."""
    backend = FakeBackend()
    backend.name = "codex-cli"
    extras = {
        "sandbox": "read-only",
        "network": False,
        "scratch_dir": str(tmp_path / "scratch"),
    }
    job = replace(_job(tmp_path), options={"extras": extras})

    def codex_advertisement() -> dict[str, Capabilities]:
        return {"codex-cli": CODEX_CAPABILITIES}

    run_jobs(
        [job],
        tmp_path / "codex-extras.sqlite3",
        capabilities_provider=codex_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert backend.calls[0][3].extras == extras


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


def test_empty_completion_diagnostics_are_recorded(tmp_path: Path) -> None:
    """A typed empty completion keeps its reasoning and finish reason."""
    backend = FakeBackend(
        error=EmptyCompletionError(
            "empty completion",
            reasoning="provider reasoning",
            finish_reason="length",
        )
    )

    snapshot = run_jobs(
        [_job(tmp_path)],
        tmp_path / "empty-completion.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    attempt = snapshot.attempts[0]
    assert attempt.reasoning == "provider reasoning"
    assert attempt.finish_reason == "length"


def test_job_timeout_remains_eligible_on_the_same_endpoint(
    tmp_path: Path,
) -> None:
    """A job-owned timeout does not exclude its endpoint from the next attempt."""
    backend = TimeoutSequenceBackend([_agent_timeout(), None])
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "timeout-retry.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert [attempt.attempt_no for attempt in snapshot.attempts] == [1, 2]
    assert [attempt.endpoint for attempt in snapshot.attempts] == [
        "fake-endpoint",
        "fake-endpoint",
    ]
    assert snapshot.attempts[0].status == "timeout"
    assert snapshot.attempts[0].halt_kind is None
    assert len(backend.calls) == 2


def test_job_timeout_uses_the_entire_attempt_budget(
    tmp_path: Path,
) -> None:
    """Repeated job-owned timeouts create one row and seam call per attempt."""
    backend = TimeoutSequenceBackend([_agent_timeout() for _ in range(3)])
    job = replace(_job(tmp_path), max_attempts=3)

    snapshot = run_jobs(
        [job],
        tmp_path / "timeout-budget.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.FAILED
    assert [attempt.attempt_no for attempt in snapshot.attempts] == [1, 2, 3]
    assert [attempt.status for attempt in snapshot.attempts] == [
        "timeout",
        "timeout",
        "timeout",
    ]
    assert all(attempt.halt_kind is None for attempt in snapshot.attempts)
    assert len(backend.calls) == 3


def test_job_effort_option_overrides_the_endpoint_default(
    tmp_path: Path,
) -> None:
    """A job may request more deliberation than its endpoint's registry entry."""
    backend = SequenceBackend([None])
    job = replace(_job(tmp_path), options={"effort": "xhigh"})

    run_jobs(
        [job],
        tmp_path / "effort-override.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert backend.calls[0][3].effort == "xhigh"


def test_unset_job_effort_keeps_the_endpoint_default(
    tmp_path: Path,
) -> None:
    """Omitting effort preserves the endpoint's own value, argv unchanged."""
    backend = SequenceBackend([None])

    run_jobs(
        [_job(tmp_path)],
        tmp_path / "effort-default.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert backend.calls[0][3].effort != "xhigh"


def test_transient_failure_retries_on_the_same_endpoint_as_a_new_attempt(
    tmp_path: Path,
) -> None:
    """A retryable exception creates a second ledger row and seam call."""
    backend = SequenceBackend([RuntimeError("temporary transport failure"), None])
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "transient-retry.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert [attempt.attempt_no for attempt in snapshot.attempts] == [1, 2]
    assert [attempt.endpoint for attempt in snapshot.attempts] == [
        "fake-endpoint",
        "fake-endpoint",
    ]
    assert snapshot.attempts[0].error_code == "execution"
    assert [call[2] for call in backend.calls] == ["fake-model", "fake-model"]


def test_noncompleted_response_is_recorded_before_the_next_attempt(
    tmp_path: Path,
) -> None:
    """A failed response never reaches acceptance in the same attempt."""
    backend = SequenceBackend(
        [
            LLMResponse(text="", model="fake-model", status="error"),
            None,
        ]
    )
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "response-retry.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert [attempt.status for attempt in snapshot.attempts] == ["error", "completed"]
    assert snapshot.attempts[0].acceptance is None


def test_accepted_attempt_terminates_before_the_retry_budget_is_spent(
    tmp_path: Path,
) -> None:
    """Acceptance is terminal even when a job has unused retry capacity."""
    backend = SequenceBackend([None, RuntimeError("must not be called")])
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "accepted-early.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert len(snapshot.attempts) == 1
    assert len(backend.calls) == 1


def test_persistent_halt_falls_back_to_the_next_preference(
    tmp_path: Path,
) -> None:
    """A taxonomy halt excludes only its endpoint for the next attempt."""
    first_backend = SequenceBackend([HaltError(HALT_RATE_LIMIT, "quota")])
    second_backend = SequenceBackend([None])
    backends = {"first": first_backend, "second": second_backend}
    factory_calls: list[str] = []

    def factory(endpoint: str, **_: object) -> BackendSelection:
        factory_calls.append(endpoint)
        return BackendSelection(endpoint, "fake", backends[endpoint], "fake-model")

    job = replace(
        _job(tmp_path),
        endpoint_preference=("first", "second"),
        max_attempts=2,
    )
    snapshot = run_jobs(
        [job],
        tmp_path / "fallback.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=factory,
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert [attempt.attempt_no for attempt in snapshot.attempts] == [1, 2]
    assert [attempt.endpoint for attempt in snapshot.attempts] == ["first", "second"]
    assert first_backend.calls and second_backend.calls
    assert factory_calls == ["first", "second"]


def test_openai_connection_failure_rotates_to_the_next_preference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OpenAI API connection failure excludes its endpoint for the run."""
    monkeypatch.setattr(
        run_module,
        "_openai",
        type("OpenAISurface", (), {"APIConnectionError": ConnectionError})(),
    )
    first_backend = SequenceBackend([ConnectionError("connection refused")])
    second_backend = SequenceBackend([None])
    backends = {"first": first_backend, "second": second_backend}

    def factory(endpoint: str, **_: object) -> BackendSelection:
        return BackendSelection(endpoint, "fake", backends[endpoint], "fake-model")

    job = replace(
        _job(tmp_path),
        endpoint_preference=("first", "second"),
        max_attempts=2,
    )
    snapshot = run_jobs(
        [job],
        tmp_path / "unreachable.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=factory,
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert [attempt.endpoint for attempt in snapshot.attempts] == ["first", "second"]
    assert snapshot.attempts[0].halt_kind == HALT_UNREACHABLE
    assert snapshot.attempts[0].error_code == HALT_UNREACHABLE


def test_attempts_exhausted_terminate_after_retryable_failures(
    tmp_path: Path,
) -> None:
    """A job becomes failed after its declared attempt budget is spent."""
    backend = SequenceBackend(
        [RuntimeError("temporary one"), RuntimeError("temporary two")]
    )
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "exhausted.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.FAILED
    assert [attempt.attempt_no for attempt in snapshot.attempts] == [1, 2]
    assert len(backend.calls) == 2


def test_running_out_of_endpoints_halts_after_a_recorded_attempt(
    tmp_path: Path,
) -> None:
    """A persistent halt with no surviving preference reaches HALTED."""
    backend = SequenceBackend([HaltError(HALT_RATE_LIMIT, "quota")])
    job = replace(
        _job(tmp_path),
        endpoint_preference=("only-endpoint",),
        max_attempts=3,
    )

    snapshot = run_jobs(
        [job],
        tmp_path / "no-endpoints.sqlite3",
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.HALTED
    assert snapshot.jobs[0].error is not None
    assert "excluded" in snapshot.jobs[0].error
    assert HALT_RATE_LIMIT in snapshot.jobs[0].error
    assert "no compatible endpoint" not in snapshot.jobs[0].error
    assert len(snapshot.attempts) == 1
    assert snapshot.attempts[0].halt_kind == HALT_RATE_LIMIT
    assert len(backend.calls) == 1


def test_resume_mid_retry_uses_the_next_append_only_attempt_number(
    tmp_path: Path,
) -> None:
    """An interrupted second attempt resumes at attempt three."""
    marker = tmp_path / "acceptance-state"
    contract = Contract(
        command=(
            sys.executable,
            "-c",
            f"from pathlib import Path; import sys; "
            f"sys.exit(0 if Path({str(marker)!r}).read_text() == 'accept' else 1)",
        ),
        directory=tmp_path,
    )
    job = replace(
        _job(tmp_path),
        max_attempts=3,
        contract=contract,
    )
    backend = MarkerSequenceBackend(
        [None, KeyboardInterrupt(), None],
        marker,
    )
    db_path = tmp_path / "mid-retry.sqlite3"
    run_id = "mid-retry"

    with pytest.raises(KeyboardInterrupt):
        run_jobs(
            [job],
            db_path,
            run_id=run_id,
            capabilities_provider=_advertisement,
            backend_factory=_factory_for(backend),
        )

    interrupted = JobStore(db_path).snapshot(run_id)
    assert interrupted.jobs[0].state is JobState.PENDING
    assert [attempt.attempt_no for attempt in interrupted.attempts] == [1, 2]
    assert interrupted.attempts[0].acceptance is not None
    assert interrupted.attempts[0].acceptance.accepted is False
    assert interrupted.attempts[1].acceptance is None

    resumed = resume_run(
        run_id,
        db_path,
        capabilities_provider=_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert resumed.jobs[0].state is JobState.ACCEPTED
    assert [attempt.attempt_no for attempt in resumed.attempts] == [1, 2, 3]
    assert len(backend.calls) == 3


def test_run_deny_floor_is_applied_to_each_retry(
    tmp_path: Path,
) -> None:
    """The run-level deny floor is forwarded on every attempt."""
    backend = SequenceBackend([RuntimeError("temporary"), None])
    job = replace(_job(tmp_path), max_attempts=2)

    snapshot = run_jobs(
        [job],
        tmp_path / "retry-floor.sqlite3",
        disallowed_tools="Bash",
        capabilities_provider=_deny_advertisement,
        backend_factory=_factory_for(backend),
    )

    assert snapshot.jobs[0].state is JobState.ACCEPTED
    assert len(backend.calls) == 2
    assert [call[3].disallowed_tools for call in backend.calls] == ["Bash", "Bash"]


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
