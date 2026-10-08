"""Job execution over the llm-scripting-kit completion seam, through a pool."""

from __future__ import annotations

import os
import random
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Collection, Iterator, Mapping, Optional, Sequence

# select.py is the guarded front door for llm_scripting_kit.completion: it
# probes for the symbols job-kit needs and raises SharedLibTooOldError with a
# named remediation when the linked shared lib predates them. Import it first
# so that guard fires before any unguarded import below can fail.
from . import select as _select  # noqa: F401

from llm_scripting_kit.completion import (
    AgentTimeoutError,
    BackendOptions,
    COMPLETED,
    ERROR,
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_QUOTA,
    HALT_RATE_LIMIT,
    HaltError,
    TIMEOUT,
    adapter_capabilities,
    create_backend,
    derive_dropped_params,
    utc_now_iso,
)
from llm_scripting_kit.completion.capabilities import Capabilities
from llm_scripting_kit.completion.factory import BackendSelection
from . import interrupts as _interrupts
from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    Continuation,
    Contract,
    ContractContext,
    InterruptRequest,
    Job,
    JobState,
    RunSnapshot,
    Usage,
    load_job_file,
    validate_max_parallel,
)
# Imported from the package .select probes, and AFTER it, so a too-old shared
# lib raises SharedLibTooOldError with its named remediation rather than a bare
# ImportError naming a symbol the user has never heard of.
from .select import (
    EffortUndeliverableError,
    NoCompatibleEndpointError,
    SelectionError,
    select_endpoint_with_readings,
)
import llm_scripting_kit.models as _lsk_models
import llm_scripting_kit.usage_budget as _lsk_usage_budget
import llm_scripting_kit.completion as _completion
from llm_scripting_kit.completion import subjects_for_disallowed_tools
from llm_scripting_kit.completion.requirements import arm_call
from .store import DuplicateJobError, JobStore, StoreError, UnknownRunError
from .workspace import WorkspaceError, WorkspaceManager, WorkspaceResolution


DEFAULT_TIMEOUT_S = 900.0
CONTRACT_OUTPUT_LIMIT = 2000
HALT_UNREACHABLE = "unreachable"
#: llm-scripting-kit's kind for transient endpoint overload (HaltError.kind,
#: or a response error code), carrying ``retry_after_s: float | None``.
HALT_BACKPRESSURE = "backpressure"
#: Halts that may clear within seconds: waited out before they count as halts.
_TRANSIENT_KINDS = frozenset({HALT_BACKPRESSURE, HALT_RATE_LIMIT})
_HALT_KINDS = frozenset(
    {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT, HALT_QUOTA, HALT_UNREACHABLE}
)
_PERSISTENT_HALT_KINDS = frozenset(
    {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT, HALT_QUOTA}
)
# A spent pool: the halted entry's pinned verdict is written back as
# OUT-OF-QUOTA until its reset, so later selections in this session skip it.
_QUOTA_HALT_KINDS = frozenset({HALT_QUOTA, HALT_INSUFFICIENT_CREDIT})

try:
    import openai as _openai
except ImportError:  # pragma: no cover - optional until an HTTP backend runs
    _openai = None


CapabilitiesProvider = Callable[[], Mapping[str, Capabilities]]
BackendFactory = Callable[..., BackendSelection]

#: The scratch directory, beside the ledger, that holds each contract run's
#: interrupt files while the contract runs. The ledger, not a file here, is
#: the durable record.
INTERRUPT_IO_DIRNAME = "interrupt-io"
_REQUEST_FILENAME = "request.json"
_RESOLUTION_FILENAME = "resolution.json"
#: Variables a contract reads to request or continue an interrupt. They are
#: always set by job-kit or removed, never inherited: a contract tells its
#: first run from a continuation by the presence of the resolution path.
_INTERRUPT_VARIABLES = (
    "JOB_KIT_INTERRUPT_REQUEST",
    "JOB_KIT_INTERRUPT_RESOLUTION",
    "JOB_KIT_INTERRUPT_ID",
    "JOB_KIT_CONTINUATION_NO",
)


@dataclass(frozen=True)
class InterruptIO:
    """The interrupt files and identity one contract run receives.

    ``request_path`` is where the contract may write an interrupt request
    (it does not exist when the contract starts). A continuation also gets
    the ``resolution_path`` of the resolution document job-kit wrote, the
    ``interrupt_id`` it continues, and its ``continuation_no``, a counter
    that changes on every re-run and is not an idempotency key.
    """

    request_path: Path
    resolution_path: Optional[Path] = None
    interrupt_id: Optional[str] = None
    continuation_no: Optional[int] = None

    def environment(self) -> dict[str, str]:
        """The ``JOB_KIT_INTERRUPT_*`` variables this run exports."""
        values = {"JOB_KIT_INTERRUPT_REQUEST": str(self.request_path)}
        if self.resolution_path is not None:
            values["JOB_KIT_INTERRUPT_RESOLUTION"] = str(self.resolution_path)
        if self.interrupt_id is not None:
            values["JOB_KIT_INTERRUPT_ID"] = str(self.interrupt_id)
        if self.continuation_no is not None:
            values["JOB_KIT_CONTINUATION_NO"] = str(self.continuation_no)
        return values


@contextmanager
def _interrupt_scratch(store: JobStore) -> Iterator[Path]:
    """A fresh scratch directory beside the ledger, removed afterwards."""
    parent = store.db_path.expanduser().resolve().parent / INTERRUPT_IO_DIRNAME
    parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=str(parent)))
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _ingest_request(
    acceptance: Acceptance, request_path: Path
) -> tuple[Acceptance, Optional[InterruptRequest], Optional[str]]:
    """Apply the interrupt-request outcome table to one contract result.

    Returns the acceptance to record, the validated request when the
    contract asked to wait, and the fault when a request makes the run fail.
    Only the request FILE is read; stdout and stderr are never parsed for
    state. A timed-out or unlaunched contract's file is not read at all, and
    the exit code keeps governing: a request beside a non-zero exit fails.
    """
    if acceptance.outcome != "observed" or not os.path.lexists(request_path):
        return acceptance, None, None
    # A refused request is recorded as ``request_refused``, never as the
    # observed exit: an exit-0 run whose request was refused is not accepted.
    refused = replace(acceptance, outcome="request_refused")
    if acceptance.exit_code != 0:
        return (
            refused,
            None,
            f"contract exited {acceptance.exit_code} after writing an interrupt request",
        )
    try:
        request = _interrupts.parse_request(request_path)
    except (_interrupts.InterruptRequestError, _interrupts.JsonSchemaSupportError) as exc:
        return refused, None, f"invalid interrupt request: {exc}"
    return replace(acceptance, outcome="interrupt_requested"), request, None


def default_store_path(project_root: Optional[str | Path] = None) -> Path:
    """Return the ephemeral project-data location for the run ledger.

    A run ledger fails the durable-project-data discriminator -- a teammate on
    a fresh clone does not need it -- so it belongs in the ephemeral twin
    ``<project>/.local-data/<marketplace>/<plugin>/`` rather than the tracked
    ``.plugin-data`` (see the bootstrap skill's durable-project-data
    reference). The ephemeral root is a fixed convention: only the durable
    directory is relocatable by project config, and that config itself lives
    under ``.local-data``.
    """
    root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path.cwd().resolve()
    )
    return root / ".local-data" / "plugins-kit" / "job-kit" / "runs.sqlite3"


def default_workspace_root(run_id: str) -> Path:
    """Return the plugin data location reserved for workspace isolation."""
    root = (
        Path.home()
        / ".claude"
        / "plugins"
        / "data"
        / "plugins-kit"
        / "job-kit"
        / "workspaces"
    ).resolve()
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    candidate = (root / run_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("run_id must stay within the workspace data directory") from exc
    return candidate


def _output_text(value: object) -> str:
    """Normalize subprocess output, including partial timeout bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _tail_output(value: str) -> str:
    """Keep the bounded tail recorded for a contract result."""
    if len(value) <= CONTRACT_OUTPUT_LIMIT:
        return value
    marker = "...[output truncated]..."
    return marker + value[-(CONTRACT_OUTPUT_LIMIT - len(marker)) :]


def _kill_contract_process_group(process: subprocess.Popen[str]) -> None:
    """Kill a timed-out contract and every process in its group."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            process.kill()
        return
    try:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def _timed_out_contract(contract: Contract, directory: Path) -> Acceptance:
    """Return the contract result for a budget spent before contract launch."""
    return Acceptance(
        command=contract.command,
        directory=directory,
        exit_code=None,
        stdout="",
        stderr="",
        wall_ms=0,
        accepted=False,
        outcome="timed_out",
    )


def run_contract(
    contract: Contract,
    *,
    directory: Optional[Path] = None,
    timeout_s: Optional[float] = None,
    response_text: str = "",
    context: Optional[ContractContext] = None,
    interrupt_io: Optional[InterruptIO] = None,
) -> Acceptance:
    """Run a contract command and capture its observed result.

    ``interrupt_io`` exports the interrupt request path (and, for a
    continuation, the resolution path, interrupt id and continuation number);
    any of those variables it does not set are removed from the inherited
    environment. Reading the request file is the caller's job.
    """
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    working_directory = (directory or contract.directory or Path.cwd()).expanduser().resolve()
    environment = None
    if context is not None or interrupt_io is not None:
        environment = os.environ.copy()
        for name in _INTERRUPT_VARIABLES:
            environment.pop(name, None)
    if context is not None:
        environment.update(
            {
                "JOB_KIT_RUN_ID": context.run_id,
                "JOB_KIT_JOB_ID": context.job_id,
                "JOB_KIT_ATTEMPT_NO": str(context.attempt_no),
                "JOB_KIT_ENDPOINT": context.endpoint,
                "JOB_KIT_BACKEND": context.backend,
                "JOB_KIT_MODEL": context.model,
            }
        )
    if interrupt_io is not None:
        environment.update(interrupt_io.environment())
    started = time.monotonic()
    outcome = "observed"
    try:
        popen_options: dict[str, object] = {}
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif os.name == "nt":
            popen_options["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        process = subprocess.Popen(
            list(contract.command),
            cwd=str(working_directory),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            **popen_options,
        )
        stdout, stderr = process.communicate(input=response_text, timeout=timeout_s)
        exit_code: Optional[int] = process.returncode
    except subprocess.TimeoutExpired as exc:
        _kill_contract_process_group(process)
        final_stdout, final_stderr = process.communicate()
        stdout = _output_text(final_stdout if final_stdout is not None else exc.stdout)
        stderr = _output_text(final_stderr if final_stderr is not None else exc.stderr)
        exit_code = None
        outcome = "timed_out"
    except OSError as exc:
        stdout = ""
        stderr = str(exc)
        exit_code = None
        outcome = "not_run"
    wall_ms = int((time.monotonic() - started) * 1000)
    return Acceptance(
        command=contract.command,
        directory=working_directory,
        exit_code=exit_code,
        stdout=_tail_output(stdout),
        stderr=_tail_output(stderr),
        wall_ms=wall_ms,
        accepted=exit_code == 0,
        outcome=outcome,
    )


def _is_own_deadline(exc: BaseException) -> bool:
    """True when the failure is job-kit's own timeout budget expiring.

    Job-kit passes its ``timeout_s`` down as the transport's timeout, so an
    expiry is this layer's deadline, not evidence about the endpoint. HTTP
    transports raise ``openai.APITimeoutError`` -- a SUBCLASS of
    ``APIConnectionError`` -- so it must be excluded before the unreachable
    test below, or a slow endpoint is wrongly excluded for the rest of the run.
    """
    if isinstance(exc, AgentTimeoutError):
        return True
    if _openai is not None:
        return isinstance(exc, getattr(_openai, "APITimeoutError", ()))
    return False


@dataclass(frozen=True)
class BackpressurePolicy:
    """How long run_job rides out a transient overload before falling back.

    ``cap_s`` bounds the total seconds waited for ONE seam call. A wait is
    the halt's ``retry_after_s`` when it carries one, else exponential
    backoff (``base_s`` doubling to ``max_s``) with +/- ``jitter`` spread.
    When the next wait would pass ``cap_s`` the overload is treated as the
    persistent rate-limit halt a 429 always was. ``cap_s`` 0 disables waiting.
    ``sleeper`` and ``rng`` are injection points for tests.
    """

    cap_s: float = 600.0
    base_s: float = 2.0
    max_s: float = 60.0
    jitter: float = 0.25
    sleeper: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random

    @classmethod
    def from_environment(cls) -> "BackpressurePolicy":
        raw = os.environ.get(BACKPRESSURE_CAP_ENV)
        if raw is None or not raw.strip():
            return cls()
        try:
            cap = float(raw)
        except ValueError as exc:
            raise ValueError(f"{BACKPRESSURE_CAP_ENV} must be a number of seconds") from exc
        if cap < 0:
            raise ValueError(f"{BACKPRESSURE_CAP_ENV} must not be negative")
        return cls(cap_s=cap)

    def wait_for(self, retry_no: int, retry_after_s: Optional[float]) -> float:
        if retry_after_s is not None and retry_after_s >= 0:
            return float(retry_after_s)
        raw = min(self.max_s, self.base_s * (2 ** (retry_no - 1)))
        return raw * (1 + self.jitter * (2 * self.rng() - 1))


#: Environment knob: total seconds one call may wait out backpressure.
BACKPRESSURE_CAP_ENV = "JOB_KIT_BACKPRESSURE_CAP_S"


def _retry_after(value: object) -> Optional[float]:
    seconds = getattr(value, "retry_after_s", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return float(seconds) if seconds >= 0 else None


def _backpressure_signal(
    backend: object, exc: BaseException
) -> tuple[bool, Optional[float]]:
    """Return (is_backpressure, retry_after_s) for a raised failure."""
    if isinstance(exc, HaltError):
        return exc.kind in _TRANSIENT_KINDS, _retry_after(exc)
    classifier = getattr(backend, "classify_halt", None)
    if callable(classifier) and classifier(exc) in _TRANSIENT_KINDS:
        return True, _retry_after(exc)
    return False, None


def _response_backpressure(response: object) -> tuple[bool, Optional[float]]:
    error = getattr(response, "error", None)
    if error is not None and str(getattr(error, "code", "")) in _TRANSIENT_KINDS:
        retry = _retry_after(error)
        return True, retry if retry is not None else _retry_after(response)
    return False, None


class _WaitBudget:
    """One seam call's backpressure waits, shared by the attempts that ride it.

    Each overloaded seam invocation is its own ledger attempt; this object only
    carries how long the call has waited so far and so when the cap is spent.
    """

    def __init__(self, policy: BackpressurePolicy) -> None:
        self.policy = policy
        self.waited = 0.0
        self.retry_no = 0

    def plan(self, retry_after_s: Optional[float]) -> Optional[float]:
        """Return the next wait, or None when it would pass the cap."""
        wait_s = self.policy.wait_for(self.retry_no + 1, retry_after_s)
        if self.waited + wait_s > self.policy.cap_s:
            return None
        return wait_s

    def record_and_sleep(
        self,
        store: JobStore,
        attempt: Attempt,
        wait_s: float,
        retry_after_s: Optional[float],
    ) -> None:
        self.retry_no += 1
        store.record_backpressure_wait(
            attempt.run_id,
            attempt.job_id,
            attempt.attempt_no,
            adapter=attempt.backend,
            model=attempt.model,
            retry_no=self.retry_no,
            wait_s=wait_s,
            retry_after_s=retry_after_s,
            waited_s=self.waited,
            at=utc_now_iso(),
        )
        self.policy.sleeper(wait_s)
        self.waited += wait_s


def _halt_for_exception(backend: object, exc: BaseException) -> Optional[str]:
    """Classify a typed transport failure without inspecting its text."""
    if _is_own_deadline(exc):
        return None
    if _openai is not None and isinstance(
        exc, getattr(_openai, "APIConnectionError", ())
    ):
        return HALT_UNREACHABLE
    if isinstance(exc, HaltError):
        return _known_halt_kind(exc.kind)
    classifier = getattr(backend, "classify_halt", None)
    if callable(classifier):
        return _known_halt_kind(classifier(exc))
    return None


def _known_halt_kind(value: object) -> Optional[str]:
    """Accept only halt labels job-kit can classify and record."""
    if value == HALT_BACKPRESSURE:
        # Backpressure (or a 429) that outlasted the wait cap is today's rate-limit halt.
        return HALT_RATE_LIMIT
    if isinstance(value, str) and value in _HALT_KINDS:
        return value
    return None


def _terminal_state_after_attempt(
    job: Job, budget_no: int, outcome: JobState
) -> Optional[JobState]:
    """Terminalize an outcome only when its budget allocation exhausts policy."""
    if outcome not in {
        JobState.ACCEPTED,
        JobState.REJECTED,
        JobState.FAILED,
        JobState.HALTED,
    }:
        raise ValueError(f"invalid attempt outcome: {outcome.value}")
    if outcome is JobState.ACCEPTED:
        return outcome
    return outcome if budget_no >= job.max_attempts else None


def _capabilities_for(
    selection: BackendSelection,
    advertised: Mapping[str, Capabilities],
) -> Optional[Capabilities]:
    """Find the selected backend's advertisement."""
    backend_name = getattr(selection.backend, "name", None)
    if not isinstance(backend_name, str):
        return None
    return advertised.get(backend_name)


def _validate_disallowed_tools(value: Optional[str]) -> Optional[str]:
    """Validate one run-level deny-list value."""
    if value is not None and not isinstance(value, str):
        raise ValueError("run disallowed_tools must be a string or null")
    return value


def _merge_disallowed_tools(
    floor: Optional[str], requested: Optional[str]
) -> Optional[str]:
    """Combine a run floor and a job deny-list without rewriting either value."""
    if floor is None:
        return requested
    if requested is None:
        return floor
    if not floor:
        return requested
    if not requested or floor == requested:
        return floor
    return f"{floor} {requested}"


def _string_option(
    options: Mapping[str, object], name: str, default: Optional[str]
) -> Optional[str]:
    """Read and validate one string-valued job option."""
    value = options.get(name, default)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"job option {name} must be a string or null")
    return value


def _requirements_with_subjects(
    requirements: Mapping[str, object], subjects: Collection[str]
) -> dict[str, object]:
    """Add required deny effects to one requirements mapping."""
    result = dict(requirements)
    existing = result.get("guarantees")
    if isinstance(existing, Mapping):
        merged = dict(existing)
        merged.update({subject: True for subject in sorted(subjects)})
        result["guarantees"] = merged
    elif isinstance(existing, str):
        result["guarantees"] = tuple(sorted({existing, *subjects}))
    elif isinstance(existing, Sequence) and not isinstance(
        existing, (str, bytes, bytearray)
    ):
        result["guarantees"] = tuple(sorted({*existing, *subjects}))
    else:
        result["guarantees"] = tuple(sorted(subjects))
    return result


def _require_floor_subjects(job: Job, run_floor: str) -> Job:
    """Require every entry to guarantee the EFFECTS the deny floor names.

    A deny floor states outcomes -- no filesystem write, no shell, no subagent
    -- and every adapter family reaches them differently: claude-cli through
    --disallowedTools, codex-cli through a read-only sandbox, opencode-cli
    through permission scalars, and a transport by having no tools at all.

    Requiring a control ID here instead ("disallowed-tools") silently restricted
    every floored run to claude-cli, because that is the one adapter spelling it
    that way. That is how the md-audit evidence pack shipped into a delivery
    path no endpoint it was measured for could be routed to: the pack is
    admitted for a local transport, and the floor admitted only Claude.

    The subjects are derived FROM the floor rather than assumed, so a floor of
    "Bash" asks for shell denial and nothing else -- and codex, which confines
    writes but keeps a shell, is still correctly refused.
    """
    subjects = subjects_for_disallowed_tools(run_floor)
    if not subjects:
        return job
    if job.uses_model_sidecars:
        model_requirements = {
            model_id: _requirements_with_subjects(
                job.effective_requirements(model_id), subjects
            )
            for model_id in job.models
        }
        # The effective maps now contain the job-level keys. Clear their
        # former source so Job's collision guard remains true after replace().
        return replace(
            job,
            requirements={},
            model_requirements=model_requirements,
        )
    return replace(
        job,
        requirements=_requirements_with_subjects(job.requirements, subjects),
    )


def _entry_effort(
    job: Job,
    selection: BackendSelection,
    advertised: Mapping[str, Capabilities],
) -> Optional[str]:
    """The per-entry effort the job states, checked deliverable.

    ``None`` when neither sidecar states an effort for this entry. Otherwise
    the selected entry's ``model_efforts`` or ``model_options.effort`` value,
    after confirming its adapter advertises a delivered ``effort`` param --
    the record specialized to this endpoint when the factory supplied one (a
    transport delivers effort only through its effort style), else the family
    advertisement. Raises
    :class:`~.select.EffortUndeliverableError` rather than dispatching at an
    effort nobody stated.
    """
    if job.model_efforts:
        effort = job.model_efforts.get(selection.endpoint)
        if effort is None:
            raise EffortUndeliverableError(
                f"job {job.id!r}: selected entry {selection.endpoint!r} has no "
                f"model_efforts entry (stated: {dict(job.model_efforts)})"
            )
    else:
        per_model = job.model_options.get(selection.endpoint, {})
        effort_value = per_model.get("effort")
        if effort_value is None:
            return None
        effort = str(effort_value)
    capabilities = selection.capabilities or _capabilities_for(selection, advertised)
    if capabilities is None or "effort" not in capabilities.params:
        raise EffortUndeliverableError(
            f"job {job.id!r}: entry {selection.endpoint!r} "
            f"({getattr(selection.backend, 'name', 'unknown')}) advertises no "
            f"delivered effort, so per-model effort {effort!r} would be dropped; "
            "give a transport entry an effort_style, or declare an entry that "
            "carries effort"
        )
    return effort


def _backend_options(
    run_id: str,
    job: Job,
    selection: BackendSelection,
    working_directory: Path,
    timeout_s: float,
    run_floor: Optional[str],
    entry_effort: Optional[str] = None,
) -> BackendOptions:
    """Build seam options for one selected entry, then apply the run floor."""
    effective = job.effective_options(selection.endpoint)
    allowed_tools = _string_option(effective, "allowed_tools", None)
    job_disallowed = _string_option(effective, "disallowed_tools", None)
    system_prompt_mode = _string_option(
        effective, "system_prompt_mode", "replace"
    )
    extras_value = effective.get("extras", {})
    if extras_value is None:
        extras_value = {}
    if not isinstance(extras_value, Mapping):
        raise ValueError("job option extras must be a mapping")
    # Effort otherwise comes only from the endpoint registry entry, which is a
    # property of the ENDPOINT rather than of the work. A job that needs more
    # deliberation than its endpoint's default says so here; unset keeps the
    # registry value, so an existing job file emits the same argv.
    effort = _string_option(effective, "effort", None)
    max_tokens_value = effective.get("max_tokens", 4096)
    temperature_value = effective.get("temperature")
    extras = dict(extras_value)

    return BackendOptions(
        timeout_s=float(timeout_s),
        cwd=working_directory,
        max_tokens=int(max_tokens_value),
        temperature=(
            float(temperature_value) if temperature_value is not None else None
        ),
        effort=(
            entry_effort
            if entry_effort is not None
            else effort if effort is not None else selection.effort
        ),
        allowed_tools=allowed_tools,
        disallowed_tools=_merge_disallowed_tools(run_floor, job_disallowed),
        system_prompt_mode=(
            system_prompt_mode if system_prompt_mode is not None else "replace"
        ),
        client_id=f"job-kit:{run_id}:{job.id}",
        log_prefix=f"[job:{job.id}]",
        extras=extras,
    )


def _exception_attempt(
    *,
    run_id: str,
    job: Job,
    selection: BackendSelection,
    options: BackendOptions,
    capabilities: Optional[Capabilities],
    attempt_no: int,
    budget_no: int,
    started_at: str,
    ended_at: str,
    exc: BaseException,
    workspace: WorkspaceResolution,
    pace_readings: Optional[tuple[Mapping[str, object], ...]] = None,
) -> tuple[Attempt, Optional[JobState]]:
    """Build the durable attempt record for a raised seam exception."""
    # Job-kit owns this deadline, so its timeout is retryable, not a provider halt.
    halt_kind = _halt_for_exception(selection.backend, exc)
    status = TIMEOUT if _is_own_deadline(exc) else ERROR
    params_report = getattr(selection.backend, "params_report", None)
    if callable(params_report):
        dropped, forwarded = params_report(options)
    else:
        report_capabilities = selection.capabilities or capabilities
        dropped = (
            derive_dropped_params(report_capabilities, options)
            if report_capabilities is not None
            else None
        )
        derive_forwarded = getattr(_completion, "derive_forwarded_params", None)
        forwarded = (
            derive_forwarded(report_capabilities, options)
            if selection.capabilities is not None and callable(derive_forwarded)
            else None
        )
    message = (
        exc.detail
        if isinstance(exc, HaltError)
        else str(exc) or exc.__class__.__name__
    )
    reasoning_value = getattr(exc, "reasoning", None)
    finish_reason_value = getattr(exc, "finish_reason", None)
    attempt = Attempt(
        run_id=run_id,
        job_id=job.id,
        attempt_no=attempt_no,
        endpoint=selection.endpoint,
        backend=selection.backend.name,
        model=selection.model,
        status=status,
        started_at=started_at,
        ended_at=ended_at,
        error=AttemptError(code=halt_kind or "execution", message=message),
        halt_kind=halt_kind,
        dropped_params=dropped,
        forwarded_params=forwarded,
        execution_controls_applied=None,
        usage=None,
        response_text="",
        workspace=workspace.path,
        base_ref=workspace.base_ref,
        workspace_status=workspace.status,
        workspace_reason=workspace.reason,
        acceptance=None,
        reasoning=(str(reasoning_value) if reasoning_value is not None else None),
        finish_reason=(
            str(finish_reason_value) if finish_reason_value is not None else None
        ),
        pace_readings=pace_readings,
    )
    outcome = JobState.HALTED if halt_kind is not None else JobState.FAILED
    return attempt, _terminal_state_after_attempt(job, budget_no, outcome)


def _response_attempt(
    *,
    run_id: str,
    job: Job,
    selection: BackendSelection,
    attempt_no: int,
    budget_no: int,
    response: object,
    workspace: WorkspaceResolution,
    pace_readings: Optional[tuple[Mapping[str, object], ...]] = None,
) -> tuple[Attempt, Optional[JobState]]:
    """Copy the truthful fields from one successful seam return."""
    response_error_value = getattr(response, "error", None)
    response_error = None
    if response_error_value is not None:
        response_error = AttemptError(
            code=str(getattr(response_error_value, "code", "execution")),
            message=str(getattr(response_error_value, "message", "")),
        )
    error_code = response_error.code if response_error is not None else None
    halt_kind = _known_halt_kind(error_code)
    status = str(getattr(response, "status", COMPLETED))
    dropped_value = getattr(response, "dropped_params", None)
    forwarded_value = getattr(response, "forwarded_params", None)
    controls_value = getattr(response, "execution_controls_applied", None)
    dropped = tuple(str(value) for value in dropped_value) if dropped_value is not None else None
    forwarded = (
        tuple(str(value) for value in forwarded_value)
        if forwarded_value is not None
        else None
    )
    controls = tuple(str(value) for value in controls_value) if controls_value is not None else None
    response_model = str(getattr(response, "model", selection.model))
    attempt = Attempt(
        run_id=run_id,
        job_id=job.id,
        attempt_no=attempt_no,
        endpoint=selection.endpoint,
        backend=selection.backend.name,
        model=response_model,
        status=status,
        started_at=getattr(response, "started_at", None),
        ended_at=getattr(response, "ended_at", None),
        error=response_error,
        halt_kind=halt_kind,
        dropped_params=dropped,
        forwarded_params=forwarded,
        execution_controls_applied=controls,
        usage=Usage.from_response(response),
        response_text=str(getattr(response, "text", "")),
        reasoning=(
            str(getattr(response, "reasoning"))
            if getattr(response, "reasoning", None) is not None
            else None
        ),
        finish_reason=(
            str(getattr(response, "finish_reason"))
            if getattr(response, "finish_reason", None) is not None
            else None
        ),
        workspace=workspace.path,
        base_ref=workspace.base_ref,
        workspace_status=workspace.status,
        workspace_reason=workspace.reason,
        acceptance=None,
        pace_readings=pace_readings,
    )
    if status != COMPLETED:
        outcome = JobState.HALTED if halt_kind is not None else JobState.FAILED
        return attempt, _terminal_state_after_attempt(job, budget_no, outcome)
    return attempt, None


def run_job(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    backpressure: Optional[BackpressurePolicy] = None,
    **kwargs: object,
) -> Attempt:
    """Execute one non-terminal job and return its last attempt.

    Every seam invocation is its own attempt row. An invocation that ends in a
    ``backpressure`` or ``rate_limit`` halt inside the wait cap is recorded as
    an attempt with that halt, a ``job-kit:backpressure-wait`` event follows
    it, and the same model is invoked again as a new attempt. Such an attempt
    does not count toward ``max_attempts`` and does not narrow dispatch. See
    :class:`BackpressurePolicy`; other arguments are those of
    :func:`_run_job_attempt`.
    """
    waits = _WaitBudget(backpressure or BackpressurePolicy.from_environment())
    while True:
        attempt, wait_s, retry_after_s = _run_job_attempt(
            store, run_id, job, waits=waits, **kwargs  # type: ignore[arg-type]
        )
        if wait_s is None:
            return attempt
        waits.record_and_sleep(store, attempt, wait_s, retry_after_s)


def _run_job_attempt(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    waits: _WaitBudget,
    halted_endpoints: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    disallowed_tools: Optional[str] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
    workspace_root: Optional[str | Path] = None,
    workspace_manager: Optional[WorkspaceManager] = None,
    reachability_cache: Optional[dict] = None,
) -> tuple[Attempt, Optional[float], Optional[float]]:
    """Execute one seam invocation as one attempt.

    Returns ``(attempt, wait_s, retry_after_s)``; ``wait_s`` is set only when
    the attempt ended in backpressure the caller should wait out and retry.

    The endpoint is the first usable entry of ``job.models`` in pace order
    (:func:`~.select.select_endpoint_with_readings`), with the run's halted
    endpoints and this job's own halted endpoints excluded. The pace readings
    it was chosen from are logged on the attempt. A quota or credit halt
    writes the entry's pinned verdict back as OUT-OF-QUOTA when the entry
    declares ``conserve_usage``. When no usable entry remains the typed floor,
    :class:`~.select.NoCompatibleEndpointError`, propagates.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    advertised = dict(
        (capabilities_provider or adapter_capabilities)()
    )
    run_record = store.get_run(run_id)
    run_floor = _merge_disallowed_tools(
        run_record.disallowed_tools if run_record is not None else None,
        _validate_disallowed_tools(disallowed_tools),
    )
    selection_job = (
        _require_floor_subjects(job, run_floor) if run_floor is not None else job
    )
    # An attempt whose halt was waited out and retried did not halt the endpoint.
    waited_out = store.waited_out_attempt_numbers(run_id, job.id)
    same_job_halts = {
        attempt.endpoint
        for attempt in store.list_attempts(run_id, job.id)
        if attempt.halt_kind is not None and attempt.attempt_no not in waited_out
    }
    selection, pace_readings = select_endpoint_with_readings(
        selection_job,
        halted_endpoints=frozenset(halted_endpoints) | same_job_halts,
        capabilities=advertised,
        backend_factory=backend_factory or create_backend,
        project_root=str(job.declared_directory),
        reachability_cache=reachability_cache if reachability_cache is not None else {},
    )
    # Before any reservation: an undeliverable per-entry effort terminalizes
    # the job as unroutable, so no attempt row claims it ran.
    entry_effort = _entry_effort(job, selection, advertised)
    manager = workspace_manager
    if manager is None:
        root = (
            Path(workspace_root).expanduser().resolve()
            if workspace_root is not None
            else (
                run_record.workspace_root
                if run_record is not None and run_record.workspace_root is not None
                else default_workspace_root(run_id)
            )
        )
        base_refs = (
            run_record.workspace_base_refs
            if run_record is not None
            else {}
        )
        try:
            store.ensure_workspace_root(run_id, root)
        except StoreError as exc:
            store.mark_failed(run_id, job.id, str(exc))
            raise
        manager = WorkspaceManager(root, (job,), base_refs=base_refs)
    else:
        try:
            store.ensure_workspace_root(run_id, manager.workspace_root)
        except StoreError as exc:
            store.mark_failed(run_id, job.id, str(exc))
            raise
    reservation = store.reserve_attempt(
        run_id,
        job.id,
        endpoint=selection.endpoint,
        backend=selection.backend.name,
        model=selection.model,
        reserved_at=utc_now_iso(),
    )
    attempt_no = reservation.attempt_no
    try:
        workspace_path = manager.allocate_path(job, attempt_no)
        reservation = store.record_reservation_workspace(
            run_id,
            job.id,
            attempt_no,
            workspace_path=workspace_path,
        )
        workspace = manager.create(job, workspace_path=workspace_path)
    except WorkspaceError as exc:
        store.resolve_reservation_before_invoke(
            run_id,
            job.id,
            attempt_no,
            reason=str(exc),
            at=utc_now_iso(),
        )
        raise
    except (KeyboardInterrupt, SystemExit) as exc:
        reason = str(exc) or exc.__class__.__name__
        store.resolve_reservation_before_invoke(
            run_id, job.id, attempt_no, reason=reason, at=utc_now_iso()
        )
        raise
    working_directory = (
        workspace.working_directory
        if workspace.path is not None
        else job.declared_directory
    )
    try:
        options = _backend_options(
            run_id,
            job,
            selection,
            working_directory,
            timeout_s,
            run_floor,
            entry_effort,
        )
        capabilities = _capabilities_for(selection, advertised)
        # Arm every request-sourced control the admitting guarantees rely on,
        # through the kit (text-only mode, or a disallowed_tools deny). A
        # subject with nothing to arm raises ValueError before anything runs.
        armed_call = arm_call(
            selection.backend,
            options,
            selection_job.effective_requirements(selection.endpoint),
            advertised.get(getattr(selection.backend, "name", None)),
        )
        options = armed_call.options
        if armed_call.backend is not selection.backend:
            selection = replace(selection, backend=armed_call.backend)
    except Exception as exc:
        store.resolve_reservation_before_invoke(
            run_id,
            job.id,
            attempt_no,
            reason=str(exc) or exc.__class__.__name__,
            at=utc_now_iso(),
        )
        raise
    except (KeyboardInterrupt, SystemExit) as exc:
        store.resolve_reservation_before_invoke(
            run_id,
            job.id,
            attempt_no,
            reason=str(exc) or exc.__class__.__name__,
            at=utc_now_iso(),
        )
        raise
    armed = store.arm_reservation(
        run_id,
        job.id,
        attempt_no,
        invoke_armed_at=utc_now_iso(),
    )
    started_at = armed.invoke_armed_at
    if started_at is None:  # pragma: no cover - arm_reservation guarantees this
        raise StoreError("armed reservation has no invoke_armed_at")
    completion_started = time.monotonic()
    try:
        response = selection.backend.complete(
            job.prompt.system,
            job.prompt.user,
            model=selection.model,
            options=options,
        )
        completion_elapsed_s = time.monotonic() - completion_started
    except Exception as exc:
        attempt, terminal_state = _exception_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            options=options,
            capabilities=capabilities,
            attempt_no=attempt_no,
            budget_no=reservation.budget_no,
            started_at=started_at,
            ended_at=utc_now_iso(),
            exc=exc,
            workspace=workspace,
            pace_readings=pace_readings,
        )
        wait_s = None
        limited, retry_after = _backpressure_signal(selection.backend, exc)
        if limited and attempt.halt_kind is not None:
            wait_s = waits.plan(retry_after)
        if wait_s is not None:
            terminal_state = None
        recorded = store.append_attempt(
            attempt,
            terminal_state=terminal_state,
            reason=_attempt_limit_reason(job, terminal_state),
        )
        if recorded.halt_kind in _QUOTA_HALT_KINDS:
            _record_quota_halt(job, recorded.endpoint, exc)
        return recorded, wait_s, retry_after
    except (KeyboardInterrupt, SystemExit) as exc:
        attempt, terminal_state = _exception_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            options=options,
            capabilities=capabilities,
            attempt_no=attempt_no,
            budget_no=reservation.budget_no,
            started_at=started_at,
            ended_at=utc_now_iso(),
            exc=exc,
            workspace=workspace,
            pace_readings=pace_readings,
        )
        store.append_attempt(attempt, terminal_state=terminal_state)
        raise

    try:
        attempt, terminal_state = _response_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            attempt_no=attempt_no,
            budget_no=reservation.budget_no,
            response=response,
            workspace=workspace,
            pace_readings=pace_readings,
        )
        attempt = replace(attempt, started_at=started_at)
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted_attempt, _ = _exception_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            options=options,
            capabilities=capabilities,
            attempt_no=attempt_no,
            budget_no=reservation.budget_no,
            started_at=started_at,
            ended_at=utc_now_iso(),
            exc=exc,
            workspace=workspace,
            pace_readings=pace_readings,
        )
        interrupted_attempt = replace(
            interrupted_attempt,
            error=AttemptError(
                code="response_interrupted",
                message=str(exc) or exc.__class__.__name__,
            ),
        )
        store.append_attempt(
            interrupted_attempt,
            terminal_state=_terminal_state_after_attempt(
                job, reservation.budget_no, JobState.FAILED
            ),
        )
        raise
    if armed_call.controls and attempt.status == COMPLETED:
        applied = set(attempt.execution_controls_applied or ())
        missing = [c for c in armed_call.controls if c not in applied]
        if missing:
            attempt = replace(
                attempt,
                status=ERROR,
                error=AttemptError(
                    code="armed_control_missing",
                    message=(
                        f"entry {selection.endpoint!r} was armed with {missing} "
                        "but its response did not report them applied"
                    ),
                ),
            )
            terminal_state = _terminal_state_after_attempt(
                job, reservation.budget_no, JobState.FAILED
            )
    if (
        entry_effort is not None
        and attempt.status == COMPLETED
        and "effort" in (attempt.dropped_params or ())
    ):
        # The adapter advertised effort but this call did not deliver it (an
        # effort already in extras wins, for instance). The response was
        # produced at some other effort, so it is not judged by the contract.
        attempt = replace(
            attempt,
            status=ERROR,
            error=AttemptError(
                code="effort_dropped",
                message=(
                    f"entry {selection.endpoint!r} reported model_efforts "
                    f"{entry_effort!r} as dropped; the attempt did not run at "
                    "the stated effort"
                ),
            ),
        )
        terminal_state = _terminal_state_after_attempt(
            job, reservation.budget_no, JobState.FAILED
        )
    if terminal_state is not None or attempt.status != COMPLETED:
        wait_s = None
        limited, retry_after = _response_backpressure(response)
        if limited and attempt.halt_kind is not None and attempt.status != COMPLETED:
            wait_s = waits.plan(retry_after)
        if wait_s is not None:
            terminal_state = None
        recorded = store.append_attempt(
            attempt,
            terminal_state=terminal_state,
            reason=_attempt_limit_reason(job, terminal_state),
        )
        if recorded.halt_kind in _QUOTA_HALT_KINDS:
            _record_quota_halt(job, recorded.endpoint, response)
        return recorded, wait_s, retry_after

    interrupt_request: Optional[InterruptRequest] = None
    request_fault: Optional[str] = None
    try:
        contract_timeout_s = timeout_s - completion_elapsed_s
        if contract_timeout_s <= 0:
            acceptance = _timed_out_contract(job.contract, working_directory)
        else:
            with _interrupt_scratch(store) as scratch:
                request_path = scratch / _REQUEST_FILENAME
                acceptance = run_contract(
                    job.contract,
                    directory=working_directory,
                    timeout_s=contract_timeout_s,
                    response_text=attempt.response_text or "",
                    context=ContractContext(
                        run_id=run_id,
                        job_id=job.id,
                        attempt_no=attempt_no,
                        endpoint=attempt.endpoint,
                        backend=attempt.backend,
                        model=attempt.model,
                    ),
                    interrupt_io=InterruptIO(request_path=request_path),
                )
                acceptance, interrupt_request, request_fault = _ingest_request(
                    acceptance, request_path
                )
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted_attempt = replace(
            attempt,
            error=AttemptError(
                code="contract_interrupted",
                message=str(exc) or exc.__class__.__name__,
            ),
        )
        store.append_attempt(
            interrupted_attempt,
            terminal_state=_terminal_state_after_attempt(
                job, reservation.budget_no, JobState.FAILED
            ),
        )
        raise
    except Exception as exc:
        failed_attempt = replace(
            attempt,
            error=AttemptError(
                code="contract",
                message=str(exc) or exc.__class__.__name__,
            ),
        )
        return (
            store.append_attempt(
                failed_attempt,
                terminal_state=_terminal_state_after_attempt(
                    job, reservation.budget_no, JobState.FAILED
                ),
            ),
            None,
            None,
        )
    try:
        attempt = replace_attempt_acceptance(attempt, acceptance)
        if request_fault is not None:
            attempt = replace(
                attempt,
                error=AttemptError(code="interrupt_request", message=request_fault),
            )
        if interrupt_request is not None:
            terminal_state = None
        else:
            terminal_state = _terminal_state_after_attempt(
                job, reservation.budget_no, _contract_outcome(acceptance, request_fault)
            )
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted_attempt = replace(
            attempt,
            error=AttemptError(
                code="acceptance_interrupted",
                message=str(exc) or exc.__class__.__name__,
            ),
        )
        store.append_attempt(
            interrupted_attempt,
            terminal_state=_terminal_state_after_attempt(
                job, reservation.budget_no, JobState.FAILED
            ),
        )
        raise
    if interrupt_request is not None:
        # The job waits on an operator. The reservation completes with this
        # attempt, so no live reservation spans the wait.
        return store.append_attempt(attempt, interrupt=interrupt_request), None, None
    return (
        store.append_attempt(
            attempt, terminal_state=terminal_state, reason=request_fault
        ),
        None,
        None,
    )


def _contract_outcome(acceptance: Acceptance, fault: Optional[str]) -> JobState:
    """Map one contract result that did not request a wait to its outcome."""
    if fault is not None or acceptance.outcome == "not_run":
        return JobState.FAILED
    if acceptance.accepted:
        return JobState.ACCEPTED
    return JobState.REJECTED


def _attempt_limit_reason(job: Job, terminal_state: Optional[JobState]) -> Optional[str]:
    """Name the attempt limit when a halt spent the job's last attempt.

    ``max_attempts`` bounds executions, not the routing pool: a halt that uses
    the last attempt ends the job at the limit even if other entries remain
    usable, so the job says so rather than reading like an empty pool (the
    floor, :class:`~.select.NoCompatibleEndpointError`, is only for that).
    """
    if terminal_state is not JobState.HALTED:
        return None
    return (
        f"job {job.id!r} halted: attempt limit reached ({job.max_attempts}); "
        "the last attempt ended in a halt"
    )


def _record_quota_halt(job: Job, endpoint: str, exc: BaseException) -> None:
    """Write an observed quota/credit halt back as the entry's pinned verdict.

    Only an entry that declares ``conserve_usage`` has a pinned verdict; any
    other entry is excluded by the halt ledger alone. The reset time is the
    halt's own when it carried one, else llm-scripting-kit's bounded default.
    Passing the registry spends every entry sharing the halted entry's quota
    pool too, so a sibling on the same account is not dispatched into the
    same spent pool.
    """
    entries = _lsk_models.discover_model_entries(
        project_root=str(job.declared_directory)
    )
    spec = getattr(entries.get(endpoint), "conserve_usage", None)
    if spec is None:
        return
    _lsk_usage_budget.record_observed_halt(
        endpoint, spec, entries=entries, resets_at=getattr(exc, "resets_at", None)
    )


def replace_attempt_acceptance(attempt: Attempt, acceptance: Acceptance) -> Attempt:
    """Return an attempt with its observed contract result attached."""
    return replace(attempt, acceptance=acceptance)


def continue_job(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Continuation:
    """Re-run the contract of a job whose interrupt was answered.

    No model call, no reservation and no attempt row: the owning attempt's
    contract runs again with that attempt's recorded response on stdin, in
    its recorded working directory, with its six ``JOB_KIT_*`` identity
    variables, plus ``JOB_KIT_INTERRUPT_ID``, ``JOB_KIT_CONTINUATION_NO``, a
    fresh ``JOB_KIT_INTERRUPT_REQUEST`` path and ``JOB_KIT_INTERRUPT_RESOLUTION``
    naming the canonical resolution document. That document and the id are
    byte-identical on every re-run, so a contract can key an idempotent side
    effect on them; job-kit cannot make the side effect idempotent itself.

    The outcome follows the attempt's own policy against the owning
    attempt's budget number: accepted; waiting on a follow-up request;
    rejected or failed when that attempt was the last budgeted one, else
    pending for a fresh attempt. Ctrl-C returns the job to waiting with its
    answer intact, so a continuation runs at least once after an answer.
    """
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    records = store.list_interrupts(run_id, job.id)
    if not records:
        raise StoreError(f"{run_id!r}/{job.id!r} has no interrupt to continue")
    record = records[-1]
    attempt_no = record.attempt_no
    owning = next(
        (
            item
            for item in store.list_attempts(run_id, job.id)
            if item.attempt_no == attempt_no
        ),
        None,
    )
    if owning is None:
        raise StoreError(
            f"interrupt {record.id} names attempt {attempt_no} of "
            f"{run_id!r}/{job.id!r}, which has no attempt row"
        )
    reservation = store.get_reservation(run_id, job.id, attempt_no)
    budget_no = reservation.budget_no if reservation is not None else attempt_no
    # begin_continuation re-checks, in its own transaction, that this latest
    # interrupt is answered, that the job waits, and that none is live.
    continuation = store.begin_continuation(run_id, job.id)
    number = continuation.continuation_no

    def finish(**outcome: object) -> Continuation:
        return store.finish_continuation(
            run_id, job.id, attempt_no, number, **outcome
        )

    try:
        directory = (
            owning.acceptance.directory if owning.acceptance is not None else None
        )
        request: Optional[InterruptRequest] = None
        fault: Optional[str] = None
        if directory is None or not directory.is_dir():
            fault = (
                f"continuation working directory {directory} no longer exists"
                if directory is not None
                else "the owning attempt recorded no contract working directory"
            )
            acceptance = Acceptance(
                command=job.contract.command,
                directory=directory or job.declared_directory,
                exit_code=None,
                stdout="",
                stderr=fault,
                wall_ms=0,
                accepted=False,
                outcome="not_run",
            )
        else:
            with _interrupt_scratch(store) as scratch:
                request_path = scratch / _REQUEST_FILENAME
                resolution_path = scratch / _RESOLUTION_FILENAME
                resolution_path.write_bytes(
                    _interrupts.resolution_document(record).encode("ascii")
                )
                acceptance = run_contract(
                    job.contract,
                    directory=directory,
                    timeout_s=timeout_s,
                    response_text=owning.response_text or "",
                    context=ContractContext(
                        run_id=run_id,
                        job_id=job.id,
                        attempt_no=attempt_no,
                        endpoint=owning.endpoint,
                        backend=owning.backend,
                        model=owning.model,
                    ),
                    interrupt_io=InterruptIO(
                        request_path=request_path,
                        resolution_path=resolution_path,
                        interrupt_id=record.id,
                        continuation_no=number,
                    ),
                )
                acceptance, request, fault = _ingest_request(acceptance, request_path)
    except (KeyboardInterrupt, SystemExit):
        finish(disposition="interrupted")
        raise
    except Exception as exc:
        return finish(
            terminal_state=_terminal_state_after_attempt(
                job, budget_no, JobState.FAILED
            ),
            reason=str(exc) or exc.__class__.__name__,
        )
    if request is not None:
        return finish(acceptance=acceptance, interrupt=request)
    if fault is None and acceptance.outcome == "not_run":
        fault = acceptance.stderr or "the contract could not be run"
    return finish(
        acceptance=acceptance,
        terminal_state=_terminal_state_after_attempt(
            job, budget_no, _contract_outcome(acceptance, fault)
        ),
        reason=fault,
    )


def _answered_and_waiting(store: JobStore, run_id: str, job_id: str) -> bool:
    """Whether a waiting job's latest interrupt is answered (continuable)."""
    records = store.list_interrupts(run_id, job_id)
    if not records:
        return False
    resolution = records[-1].resolution
    return resolution is not None and resolution.outcome == "answered"


def _store_object(store: JobStore | str | Path) -> JobStore:
    """Normalize a store object or database path."""
    return store if isinstance(store, JobStore) else JobStore(store)


def _selection_halted_reason(
    job: Job,
    attempts: Sequence[Attempt],
    halted_endpoints: Collection[str],
    floor: Optional[NoCompatibleEndpointError] = None,
) -> str:
    """Explain the endpoint exclusions that followed a recorded attempt.

    ``floor`` appends the itemised disposition of every declared id -- the
    one surface where an id that selection skipped silently may be named.
    """
    reason = _exclusion_summary(job, attempts, halted_endpoints)
    if floor is None:
        return reason
    return f"{reason}; no usable routing target remains:\n{floor.dispositions_text()}"


def _exclusion_summary(
    job: Job,
    attempts: Sequence[Attempt],
    halted_endpoints: Collection[str],
) -> str:
    """Name the exclusions recorded against a job's declared entries."""
    exclusions: list[str] = []
    described: set[str] = set()
    for attempt in attempts:
        if attempt.halt_kind is None or attempt.endpoint in described:
            continue
        exclusions.append(f"{attempt.endpoint!r} ({attempt.halt_kind})")
        described.add(attempt.endpoint)
    for endpoint in job.models:
        if endpoint in halted_endpoints and endpoint not in described:
            exclusions.append(f"{endpoint!r} (excluded after a confirming probe or persistent halt)")
            described.add(endpoint)
    if exclusions:
        return (
            f"job {job.id!r} halted: endpoint(s) {', '.join(exclusions)} "
            "were excluded after a confirming probe, persistent halt, or same-job halt"
        )
    return (
        f"job {job.id!r} halted: no endpoint remained after "
        f"{len(attempts)} attempt(s); the remaining endpoints were excluded"
    )


class _HaltedEndpoints:
    """Union the durable halt record with halts observed in this process.

    The durable read is authoritative for everything a previous process wrote,
    so it is never replaced -- a resumed run must keep seeing prior-process
    halts. The in-memory set only adds what workers in THIS process observed,
    which keeps narrowing monotonic for jobs dispatched afterwards.
    """

    def __init__(self, store: JobStore, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._lock = threading.Lock()
        self._observed: set[str] = set()

    def current(self) -> frozenset[str]:
        """Return the durable halts unioned with this process's observations."""
        durable = self._store.halted_endpoints(self._run_id)
        with self._lock:
            self._observed.update(durable)
            return frozenset(self._observed)

    def record(self, endpoint: str) -> None:
        """Note an endpoint whose attempt carried a persistent halt kind."""
        with self._lock:
            self._observed.add(endpoint)


def _drive_job(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    halts: _HaltedEndpoints,
    timeout_s: float,
    run_floor: Optional[str],
    capabilities_provider: Optional[CapabilitiesProvider],
    backend_factory: Optional[BackendFactory],
    workspace_root: Path,
    workspace_manager: WorkspaceManager,
    reachability_cache: Optional[dict] = None,
) -> None:
    """Drive one job to a terminal state or to its attempt budget.

    This is the unit the worker pool submits. Attempts within a job stay
    strictly sequential, which is what keeps the attempt sequence append-only
    without a lease: parallelism is across jobs only.

    A ``waiting`` job ends the drive: a wait holds no reservation and cannot
    take one. The one exception is a job found waiting on an ANSWERED
    interrupt when the drive starts, whose contract is continued first. A
    job that starts waiting during this drive, or an answer that arrives
    during it, is continued by the next pass.
    """
    starting = True
    while True:
        current = store.get_job(run_id, job.id)
        if current is None or current.terminal:
            return
        if current.state is JobState.WAITING:
            if not starting or not _answered_and_waiting(store, run_id, job.id):
                return
            starting = False
            continue_job(store, run_id, job, timeout_s=timeout_s)
            continue
        starting = False
        halted_endpoints = halts.current()
        try:
            attempt = run_job(
                store,
                run_id,
                job,
                halted_endpoints=halted_endpoints,
                timeout_s=timeout_s,
                disallowed_tools=run_floor,
                capabilities_provider=capabilities_provider,
                backend_factory=backend_factory,
                workspace_root=workspace_root,
                workspace_manager=workspace_manager,
                reachability_cache=reachability_cache,
            )
        except EffortUndeliverableError as exc:
            # Named in full whatever came before: the effort, not a halt, is
            # why this job cannot route.
            store.mark_unroutable(run_id, job.id, str(exc))
            return
        except SelectionError as exc:
            floor = exc if isinstance(exc, NoCompatibleEndpointError) else None
            attempts = store.list_attempts(run_id, job.id)
            if attempts:
                store.mark_halted(
                    run_id,
                    job.id,
                    _selection_halted_reason(job, attempts, halted_endpoints, floor),
                )
            elif set(job.models) & set(halted_endpoints):
                store.mark_unroutable(
                    run_id,
                    job.id,
                    _selection_halted_reason(job, attempts, halted_endpoints, floor),
                )
            else:
                store.mark_unroutable(run_id, job.id, str(exc))
            return
        except WorkspaceError:
            return
        if attempt.halt_kind in _PERSISTENT_HALT_KINDS:
            halts.record(attempt.endpoint)


def _run_pending(
    store: JobStore,
    run_id: str,
    *,
    workspace_root: Optional[str | Path],
    timeout_s: float,
    capabilities_provider: Optional[CapabilitiesProvider],
    backend_factory: Optional[BackendFactory],
    workspace_manager: Optional[WorkspaceManager] = None,
    max_parallel: int = 1,
) -> RunSnapshot:
    """Process pending and interrupted jobs through a bounded worker pool.

    Jobs are submitted in declaration order. At ``max_parallel`` 1 they are
    driven inline, in that order, exactly as a sequential run always did.
    """
    bound = validate_max_parallel(max_parallel)
    store.recover_reservations(run_id)
    # Record every lapsed interrupt BEFORE the read that builds the dispatch
    # list, so a job that has just expired is never submitted to a worker.
    store.expire_interrupts(run_id, time.time())
    records = store.list_jobs(run_id)
    root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else default_workspace_root(run_id)
    )
    store.ensure_workspace_root(run_id, root)
    run_record = store.get_run(run_id)
    run_floor = run_record.disallowed_tools if run_record is not None else None
    manager = workspace_manager
    if manager is None:
        base_refs = run_record.workspace_base_refs if run_record is not None else {}
        manager = WorkspaceManager(
            root,
            tuple(record.job for record in records if not record.terminal),
            base_refs=base_refs,
        )
    halts = _HaltedEndpoints(store, run_id)
    pending = [record.job for record in records if not record.terminal]
    dispatch: dict[str, object] = dict(
        halts=halts,
        # One reachability probe per entry per run, shared by every worker.
        reachability_cache={},
        timeout_s=timeout_s,
        run_floor=run_floor,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
        workspace_root=root,
        workspace_manager=manager,
    )
    if bound == 1 or len(pending) < 2:
        for job in pending:
            _drive_job(store, run_id, job, **dispatch)
        return store.snapshot(run_id)

    # Two workers on one job would break the append-only attempt sequence, and
    # nothing but this submission loop guards it: every pending job is
    # submitted exactly once, so the invariant is asserted here rather than
    # left to a lease column the resume path could not honor.
    job_ids = [job.id for job in pending]
    if len(job_ids) != len(set(job_ids)):
        raise DuplicateJobError(
            f"run {run_id!r} lists a repeated job id; refusing to dispatch it twice"
        )
    store.scale_busy_timeout(bound)
    executor = ThreadPoolExecutor(max_workers=bound, thread_name_prefix="job-kit")
    futures: dict[Future[None], str] = {}
    failure: Optional[BaseException] = None
    try:
        for job in pending:
            futures[executor.submit(_drive_job, store, run_id, job, **dispatch)] = job.id
        for future in as_completed(futures):
            # A worker exception that is neither SelectionError nor
            # WorkspaceError is unexpected: at max_parallel 1 it aborts the run
            # loudly, and a pool must not turn it into a normal-looking
            # snapshot with a job stranded non-terminal.
            error = future.exception()
            if error is not None:
                failure = error
                break
    except (KeyboardInterrupt, SystemExit):
        # In-flight attempts are never cancelled: an aborted invocation cannot
        # be truthfully recorded. Stop dispatching and join what is running.
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    if failure is not None:
        executor.shutdown(wait=True, cancel_futures=True)
        raise failure
    executor.shutdown(wait=True)
    return store.snapshot(run_id)


def run_jobs(
    jobs: Sequence[Job],
    store: JobStore | str | Path,
    *,
    run_id: Optional[str] = None,
    jobs_path: Optional[str | Path] = None,
    max_parallel: int = 1,
    workspace_root: Optional[str | Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    disallowed_tools: Optional[str] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Create and execute a flat run of jobs through a bounded pool."""
    # Probe the interrupt validator before the ledger is opened, migrated or
    # written: a run that could not validate a request must not start.
    _interrupts._schema_validator()
    store_object = _store_object(store)
    identifier = run_id or uuid.uuid4().hex
    root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else default_workspace_root(identifier)
    )
    job_values = tuple(jobs)
    bound = validate_max_parallel(max_parallel)
    run_floor = _validate_disallowed_tools(disallowed_tools)
    workspace_manager = WorkspaceManager(root, job_values)
    store_object.create_run(
        identifier,
        job_values,
        jobs_path=jobs_path,
        max_parallel=bound,
        workspace_root=root,
        workspace_base_refs=workspace_manager.base_refs,
        disallowed_tools=run_floor,
    )
    return _run_pending(
        store_object,
        identifier,
        workspace_root=root,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
        workspace_manager=workspace_manager,
        max_parallel=bound,
    )


def run_job_file(
    jobs_path: str | Path,
    *,
    store_path: Optional[str | Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    run_id: Optional[str] = None,
    max_parallel: Optional[int] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Load a jobs YAML file, create its ledger, and execute it.

    ``run_id`` preassigns the ledger's run id so the caller knows it before
    the run starts and can resume it after an interruption. ``max_parallel``
    overrides the file's own bound, and the override is what the ledger
    records, so a later resume of this run inherits it."""
    _interrupts._schema_validator()
    path = Path(jobs_path).expanduser().resolve()
    job_file = load_job_file(path)
    store = JobStore(store_path or default_store_path())
    return run_jobs(
        job_file.jobs,
        store,
        jobs_path=path,
        max_parallel=(
            job_file.max_parallel
            if max_parallel is None
            else validate_max_parallel(max_parallel)
        ),
        workspace_root=job_file.workspace_root,
        disallowed_tools=job_file.disallowed_tools,
        timeout_s=timeout_s,
        run_id=run_id,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
    )


def resume_run(
    run_id: str,
    store: JobStore | str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_parallel: Optional[int] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Reopen a ledger and execute its non-terminal jobs.

    The pool width comes from the ledger's recorded ``max_parallel``. An
    explicit ``max_parallel`` applies to this pass only and is never written
    back: the ledger records what the run was created with.
    """
    # Before recovery, expiry or the migrating open of the ledger.
    _interrupts._schema_validator()
    store_object = _store_object(store)
    run = store_object.get_run(run_id)
    if run is None:
        raise UnknownRunError(run_id)
    root = run.workspace_root or default_workspace_root(run_id)
    bound = (
        run.max_parallel
        if max_parallel is None
        else validate_max_parallel(max_parallel)
    )
    return _run_pending(
        store_object,
        run_id,
        workspace_root=root,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
        max_parallel=bound,
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CONTRACT_OUTPUT_LIMIT",
    "HALT_UNREACHABLE",
    "INTERRUPT_IO_DIRNAME",
    "InterruptIO",
    "continue_job",
    "default_store_path",
    "default_workspace_root",
    "run_contract",
    "run_job",
    "replace_attempt_acceptance",
    "run_jobs",
    "run_job_file",
    "resume_run",
]
