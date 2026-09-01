"""Sequential job execution over the llm-scripting-kit completion seam."""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from llm_scripting_kit.completion import (
    AgentTimeoutError,
    BackendOptions,
    COMPLETED,
    ERROR,
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
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
from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    Contract,
    Job,
    JobState,
    RunSnapshot,
    Usage,
    load_job_file,
)
from .select import SelectionError, select_endpoint
from .store import JobStore, UnknownRunError


DEFAULT_TIMEOUT_S = 900.0
CONTRACT_OUTPUT_LIMIT = 2000


CapabilitiesProvider = Callable[[], Mapping[str, Capabilities]]
BackendFactory = Callable[..., BackendSelection]


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
    return (
        Path.home()
        / ".claude"
        / "plugins"
        / "data"
        / "plugins-kit"
        / "job-kit"
        / "workspaces"
        / run_id
    )


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


def run_contract(
    contract: Contract,
    *,
    directory: Optional[Path] = None,
    timeout_s: Optional[float] = None,
) -> Acceptance:
    """Run a contract command and capture its observed result."""
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    working_directory = (directory or contract.directory or Path.cwd()).expanduser().resolve()
    started = time.monotonic()
    outcome = "observed"
    try:
        result = subprocess.run(
            list(contract.command),
            cwd=str(working_directory),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout_s,
        )
        stdout = _output_text(result.stdout)
        stderr = _output_text(result.stderr)
        exit_code: Optional[int] = result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = _output_text(exc.stdout)
        stderr = _output_text(exc.stderr)
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


def _attempt_number(store: JobStore, run_id: str, job_id: str) -> int:
    """Return the append-only number for the next attempt."""
    return len(store.list_attempts(run_id, job_id)) + 1


def _halt_for_exception(backend: object, exc: Exception) -> Optional[str]:
    """Classify a typed transport failure without inspecting its text."""
    if isinstance(exc, HaltError):
        return exc.kind
    classifier = getattr(backend, "classify_halt", None)
    if callable(classifier):
        return classifier(exc)
    return None


def _capabilities_for(
    selection: BackendSelection,
    advertised: Mapping[str, Capabilities],
) -> Optional[Capabilities]:
    """Find the selected backend's advertisement."""
    backend_name = getattr(selection.backend, "name", None)
    if not isinstance(backend_name, str):
        return None
    return advertised.get(backend_name)


def _exception_attempt(
    *,
    run_id: str,
    job: Job,
    selection: BackendSelection,
    options: BackendOptions,
    capabilities: Optional[Capabilities],
    attempt_no: int,
    started_at: str,
    ended_at: str,
    exc: Exception,
) -> tuple[Attempt, JobState]:
    """Build the durable attempt record for a raised seam exception."""
    halt_kind = _halt_for_exception(selection.backend, exc)
    status = TIMEOUT if isinstance(exc, AgentTimeoutError) else ERROR
    dropped = (
        derive_dropped_params(capabilities, options)
        if capabilities is not None
        else None
    )
    message = exc.detail if isinstance(exc, HaltError) else str(exc)
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
        execution_controls_applied=None,
        usage=None,
        response_text="",
        workspace=None,
        acceptance=None,
    )
    return attempt, JobState.HALTED if halt_kind is not None else JobState.FAILED


def _response_attempt(
    *,
    run_id: str,
    job: Job,
    selection: BackendSelection,
    attempt_no: int,
    response: object,
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
    halt_kinds = {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT}
    halt_kind = error_code if error_code in halt_kinds else None
    status = str(getattr(response, "status", COMPLETED))
    dropped_value = getattr(response, "dropped_params", None)
    controls_value = getattr(response, "execution_controls_applied", None)
    dropped = tuple(str(value) for value in dropped_value) if dropped_value is not None else None
    controls = tuple(str(value) for value in controls_value) if controls_value is not None else None
    attempt = Attempt(
        run_id=run_id,
        job_id=job.id,
        attempt_no=attempt_no,
        endpoint=selection.endpoint,
        backend=selection.backend.name,
        model=str(getattr(response, "model", selection.model)),
        status=status,
        started_at=getattr(response, "started_at", None),
        ended_at=getattr(response, "ended_at", None),
        error=response_error,
        halt_kind=halt_kind,
        dropped_params=dropped,
        execution_controls_applied=controls,
        usage=Usage.from_response(response),
        response_text=str(getattr(response, "text", "")),
        workspace=None,
        acceptance=None,
    )
    if status != COMPLETED:
        return attempt, JobState.HALTED if halt_kind is not None else JobState.FAILED
    return attempt, None


def run_job(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    halted_endpoints: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> Attempt:
    """Execute one non-terminal job with exactly one seam invocation."""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    advertised = dict(
        (capabilities_provider or adapter_capabilities)()
    )
    selection = select_endpoint(
        job,
        halted_endpoints=halted_endpoints,
        capabilities=advertised,
        backend_factory=backend_factory or create_backend,
        project_root=str(job.declared_directory),
    )
    store.mark_running(run_id, job.id)
    options = BackendOptions(
        timeout_s=float(timeout_s),
        cwd=job.declared_directory,
        effort=selection.effort,
        log_prefix=f"[job:{job.id}]",
    )
    capabilities = _capabilities_for(selection, advertised)
    attempt_no = _attempt_number(store, run_id, job.id)
    started_at = utc_now_iso()
    try:
        response = selection.backend.complete(
            job.prompt.system,
            job.prompt.user,
            model=selection.model,
            options=options,
        )
    except Exception as exc:
        attempt, terminal_state = _exception_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            options=options,
            capabilities=capabilities,
            attempt_no=attempt_no,
            started_at=started_at,
            ended_at=utc_now_iso(),
            exc=exc,
        )
        return store.append_attempt(attempt, terminal_state=terminal_state)

    attempt, terminal_state = _response_attempt(
        run_id=run_id,
        job=job,
        selection=selection,
        attempt_no=attempt_no,
        response=response,
    )
    if terminal_state is not None:
        return store.append_attempt(attempt, terminal_state=terminal_state)

    acceptance = run_contract(
        job.contract,
        directory=job.declared_directory,
        timeout_s=timeout_s,
    )
    attempt = replace_attempt_acceptance(attempt, acceptance)
    if acceptance.outcome == "not_run":
        terminal_state = JobState.FAILED
    elif acceptance.accepted:
        terminal_state = JobState.ACCEPTED
    else:
        terminal_state = JobState.REJECTED
    return store.append_attempt(attempt, terminal_state=terminal_state)


def replace_attempt_acceptance(attempt: Attempt, acceptance: Acceptance) -> Attempt:
    """Return an attempt with its observed contract result attached."""
    return Attempt(
        run_id=attempt.run_id,
        job_id=attempt.job_id,
        attempt_no=attempt.attempt_no,
        endpoint=attempt.endpoint,
        backend=attempt.backend,
        model=attempt.model,
        status=attempt.status,
        started_at=attempt.started_at,
        ended_at=attempt.ended_at,
        error=attempt.error,
        halt_kind=attempt.halt_kind,
        dropped_params=attempt.dropped_params,
        execution_controls_applied=attempt.execution_controls_applied,
        usage=attempt.usage,
        response_text=attempt.response_text,
        workspace=attempt.workspace,
        acceptance=acceptance,
        id=attempt.id,
    )


def _store_object(store: JobStore | str | Path) -> JobStore:
    """Normalize a store object or database path."""
    return store if isinstance(store, JobStore) else JobStore(store)


def _run_pending(
    store: JobStore,
    run_id: str,
    *,
    timeout_s: float,
    capabilities_provider: Optional[CapabilitiesProvider],
    backend_factory: Optional[BackendFactory],
) -> RunSnapshot:
    """Process pending and interrupted jobs in declaration order."""
    for record in store.list_jobs(run_id):
        if record.terminal:
            continue
        try:
            run_job(
                store,
                run_id,
                record.job,
                halted_endpoints=store.halted_endpoints(run_id),
                timeout_s=timeout_s,
                capabilities_provider=capabilities_provider,
                backend_factory=backend_factory,
            )
        except SelectionError as exc:
            store.mark_unroutable(run_id, record.job.id, str(exc))
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
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Create and execute a flat sequential run of jobs."""
    store_object = _store_object(store)
    identifier = run_id or uuid.uuid4().hex
    root = workspace_root or default_workspace_root(identifier)
    store_object.create_run(
        identifier,
        tuple(jobs),
        jobs_path=jobs_path,
        max_parallel=max_parallel,
        workspace_root=root,
    )
    return _run_pending(
        store_object,
        identifier,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
    )


def run_job_file(
    jobs_path: str | Path,
    *,
    store_path: Optional[str | Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Load a jobs YAML file, create its ledger, and execute it."""
    path = Path(jobs_path).expanduser().resolve()
    job_file = load_job_file(path)
    store = JobStore(store_path or default_store_path())
    return run_jobs(
        job_file.jobs,
        store,
        jobs_path=path,
        max_parallel=job_file.max_parallel,
        workspace_root=job_file.workspace_root,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
    )


def resume_run(
    run_id: str,
    store: JobStore | str | Path,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
) -> RunSnapshot:
    """Reopen a ledger and execute its non-terminal jobs."""
    store_object = _store_object(store)
    if store_object.get_run(run_id) is None:
        raise UnknownRunError(run_id)
    return _run_pending(
        store_object,
        run_id,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CONTRACT_OUTPUT_LIMIT",
    "default_store_path",
    "default_workspace_root",
    "run_contract",
    "run_job",
    "replace_attempt_acceptance",
    "run_jobs",
    "run_job_file",
    "resume_run",
]
