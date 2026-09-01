"""Sequential job execution over the llm-scripting-kit completion seam."""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Collection, Mapping, Optional, Sequence

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
    ContractContext,
    Job,
    JobState,
    RunSnapshot,
    Usage,
    load_job_file,
)
from .select import SelectionError, select_endpoint
from .store import JobStore, StoreError, UnknownRunError
from .workspace import WorkspaceError, WorkspaceManager, WorkspaceResolution


DEFAULT_TIMEOUT_S = 900.0
CONTRACT_OUTPUT_LIMIT = 2000
HALT_UNREACHABLE = "unreachable"
_HALT_KINDS = frozenset(
    {HALT_AUTH, HALT_RATE_LIMIT, HALT_INSUFFICIENT_CREDIT, HALT_UNREACHABLE}
)

try:
    import openai as _openai
except ImportError:  # pragma: no cover - optional until an HTTP backend runs
    _openai = None


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


def run_contract(
    contract: Contract,
    *,
    directory: Optional[Path] = None,
    timeout_s: Optional[float] = None,
    response_text: str = "",
    context: Optional[ContractContext] = None,
) -> Acceptance:
    """Run a contract command and capture its observed result."""
    if timeout_s is not None and timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    working_directory = (directory or contract.directory or Path.cwd()).expanduser().resolve()
    environment = None
    if context is not None:
        environment = os.environ.copy()
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
    started = time.monotonic()
    outcome = "observed"
    try:
        result = subprocess.run(
            list(contract.command),
            cwd=str(working_directory),
            capture_output=True,
            input=response_text,
            env=environment,
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


def _halt_for_exception(backend: object, exc: BaseException) -> Optional[str]:
    """Classify a typed transport failure without inspecting its text."""
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
    """Accept only halt labels defined by llm-scripting-kit's taxonomy."""
    if isinstance(value, str) and value in _HALT_KINDS:
        return value
    return None


def _terminal_state_after_attempt(
    job: Job, attempt_no: int, outcome: JobState
) -> Optional[JobState]:
    """Terminalize an outcome only when this attempt exhausts the policy."""
    if outcome not in {
        JobState.ACCEPTED,
        JobState.REJECTED,
        JobState.FAILED,
        JobState.HALTED,
    }:
        raise ValueError(f"invalid attempt outcome: {outcome.value}")
    if outcome is JobState.ACCEPTED:
        return outcome
    return outcome if attempt_no >= job.max_attempts else None


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


def _require_disallowed_control(job: Job) -> Job:
    """Require the existing selection contract to honor a run deny floor."""
    requirements = dict(job.requirements)
    if "controls" in requirements:
        key = "controls"
    elif "execution_controls" in requirements:
        key = "execution_controls"
    else:
        key = "controls"
    existing = requirements.get(key)
    if isinstance(existing, Mapping):
        controls = dict(existing)
        controls["disallowed-tools"] = True
        requirements[key] = controls
    elif isinstance(existing, str):
        requirements[key] = (existing, "disallowed-tools")
    elif isinstance(existing, Sequence) and not isinstance(
        existing, (str, bytes, bytearray)
    ):
        requirements[key] = tuple(existing) + ("disallowed-tools",)
    else:
        requirements[key] = ("disallowed-tools",)
    return replace(job, requirements=requirements)


def _backend_options(
    job: Job,
    selection: BackendSelection,
    working_directory: Path,
    timeout_s: float,
    run_floor: Optional[str],
) -> BackendOptions:
    """Build seam options from one job and its run-level deny floor."""
    allowed_tools = _string_option(job.options, "allowed_tools", None)
    job_disallowed = _string_option(job.options, "disallowed_tools", None)
    system_prompt_mode = _string_option(
        job.options, "system_prompt_mode", "replace"
    )
    extras_value = job.options.get("extras", {})
    if extras_value is None:
        extras_value = {}
    if not isinstance(extras_value, Mapping):
        raise ValueError("job option extras must be a mapping")
    # Effort otherwise comes only from the endpoint registry entry, which is a
    # property of the ENDPOINT rather than of the work. A job that needs more
    # deliberation than its endpoint's default says so here; unset keeps the
    # registry value, so an existing job file emits the same argv.
    effort = _string_option(job.options, "effort", None)
    max_tokens_value = job.options.get("max_tokens", 4096)
    temperature_value = job.options.get("temperature", 0.3)
    return BackendOptions(
        timeout_s=float(timeout_s),
        cwd=working_directory,
        max_tokens=int(max_tokens_value),
        temperature=float(temperature_value),
        effort=effort if effort is not None else selection.effort,
        allowed_tools=allowed_tools,
        disallowed_tools=_merge_disallowed_tools(run_floor, job_disallowed),
        system_prompt_mode=(
            system_prompt_mode if system_prompt_mode is not None else "replace"
        ),
        log_prefix=f"[job:{job.id}]",
        extras=dict(extras_value),
    )


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
    exc: BaseException,
    workspace: WorkspaceResolution,
) -> tuple[Attempt, Optional[JobState]]:
    """Build the durable attempt record for a raised seam exception."""
    if isinstance(exc, AgentTimeoutError):
        # Job-kit owns this deadline, so its timeout is retryable, not a provider halt.
        halt_kind = None
    else:
        halt_kind = _halt_for_exception(selection.backend, exc)
    status = TIMEOUT if isinstance(exc, AgentTimeoutError) else ERROR
    dropped = (
        derive_dropped_params(capabilities, options)
        if capabilities is not None
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
        forwarded_params=None,
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
    )
    outcome = JobState.HALTED if halt_kind is not None else JobState.FAILED
    return attempt, _terminal_state_after_attempt(job, attempt_no, outcome)


def _response_attempt(
    *,
    run_id: str,
    job: Job,
    selection: BackendSelection,
    attempt_no: int,
    response: object,
    workspace: WorkspaceResolution,
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
    halt_kind = error_code if error_code in _HALT_KINDS else None
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
    )
    if status != COMPLETED:
        outcome = JobState.HALTED if halt_kind is not None else JobState.FAILED
        return attempt, _terminal_state_after_attempt(job, attempt_no, outcome)
    return attempt, None


def run_job(
    store: JobStore,
    run_id: str,
    job: Job,
    *,
    halted_endpoints: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
    disallowed_tools: Optional[str] = None,
    capabilities_provider: Optional[CapabilitiesProvider] = None,
    backend_factory: Optional[BackendFactory] = None,
    workspace_root: Optional[str | Path] = None,
    workspace_manager: Optional[WorkspaceManager] = None,
) -> Attempt:
    """Execute one non-terminal job with exactly one seam invocation."""
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
    selection_job = _require_disallowed_control(job) if run_floor is not None else job
    selection = select_endpoint(
        selection_job,
        halted_endpoints=halted_endpoints,
        capabilities=advertised,
        backend_factory=backend_factory or create_backend,
        project_root=str(job.declared_directory),
    )
    store.mark_running(run_id, job.id)
    attempt_no = _attempt_number(store, run_id, job.id)
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
    try:
        workspace = manager.prepare(job, attempt_no)
    except WorkspaceError as exc:
        store.mark_failed(run_id, job.id, str(exc))
        raise
    except (KeyboardInterrupt, SystemExit) as exc:
        reason = str(exc) or exc.__class__.__name__
        store.mark_failed(run_id, job.id, reason)
        raise
    working_directory = (
        workspace.working_directory
        if workspace.path is not None
        else job.declared_directory
    )
    options = _backend_options(
        job,
        selection,
        working_directory,
        timeout_s,
        run_floor,
    )
    capabilities = _capabilities_for(selection, advertised)
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
            workspace=workspace,
        )
        return store.append_attempt(attempt, terminal_state=terminal_state)
    except (KeyboardInterrupt, SystemExit) as exc:
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
            workspace=workspace,
        )
        store.append_attempt(attempt, terminal_state=terminal_state)
        raise

    try:
        attempt, terminal_state = _response_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            attempt_no=attempt_no,
            response=response,
            workspace=workspace,
        )
    except (KeyboardInterrupt, SystemExit) as exc:
        interrupted_attempt, _ = _exception_attempt(
            run_id=run_id,
            job=job,
            selection=selection,
            options=options,
            capabilities=capabilities,
            attempt_no=attempt_no,
            started_at=started_at,
            ended_at=utc_now_iso(),
            exc=exc,
            workspace=workspace,
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
                job, attempt_no, JobState.FAILED
            ),
        )
        raise
    if terminal_state is not None or attempt.status != COMPLETED:
        return store.append_attempt(attempt, terminal_state=terminal_state)

    try:
        acceptance = run_contract(
            job.contract,
            directory=working_directory,
            timeout_s=timeout_s,
            response_text=attempt.response_text or "",
            context=ContractContext(
                run_id=run_id,
                job_id=job.id,
                attempt_no=attempt_no,
                endpoint=attempt.endpoint,
                backend=attempt.backend,
                model=attempt.model,
            ),
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
                job, attempt_no, JobState.FAILED
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
        return store.append_attempt(
            failed_attempt,
            terminal_state=_terminal_state_after_attempt(
                job, attempt_no, JobState.FAILED
            ),
        )
    try:
        attempt = replace_attempt_acceptance(attempt, acceptance)
        if acceptance.outcome == "not_run":
            outcome = JobState.FAILED
        elif acceptance.accepted:
            outcome = JobState.ACCEPTED
        else:
            outcome = JobState.REJECTED
        terminal_state = _terminal_state_after_attempt(job, attempt_no, outcome)
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
                job, attempt_no, JobState.FAILED
            ),
        )
        raise
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
        reasoning=attempt.reasoning,
        finish_reason=attempt.finish_reason,
        workspace=attempt.workspace,
        base_ref=attempt.base_ref,
        workspace_status=attempt.workspace_status,
        workspace_reason=attempt.workspace_reason,
        workspace_removed_at=attempt.workspace_removed_at,
        workspace_removal_forced=attempt.workspace_removal_forced,
        forwarded_params=attempt.forwarded_params,
        acceptance=acceptance,
        id=attempt.id,
    )


def _store_object(store: JobStore | str | Path) -> JobStore:
    """Normalize a store object or database path."""
    return store if isinstance(store, JobStore) else JobStore(store)


def _selection_halted_reason(
    job: Job,
    attempts: Sequence[Attempt],
    halted_endpoints: Collection[str],
) -> str:
    """Explain the endpoint exclusions that followed a recorded attempt."""
    exclusions: list[str] = []
    described: set[str] = set()
    for attempt in attempts:
        if attempt.halt_kind is None or attempt.endpoint in described:
            continue
        exclusions.append(f"{attempt.endpoint!r} ({attempt.halt_kind})")
        described.add(attempt.endpoint)
    for endpoint in job.endpoint_preference:
        if endpoint in halted_endpoints and endpoint not in described:
            exclusions.append(f"{endpoint!r} (persistent halt)")
            described.add(endpoint)
    if exclusions:
        return (
            f"job {job.id!r} halted: endpoint(s) {', '.join(exclusions)} "
            "were excluded by persistent halt classification"
        )
    return (
        f"job {job.id!r} halted: no endpoint remained after "
        f"{len(attempts)} attempt(s); the remaining endpoints were excluded"
    )


def _run_pending(
    store: JobStore,
    run_id: str,
    *,
    workspace_root: Optional[str | Path],
    timeout_s: float,
    capabilities_provider: Optional[CapabilitiesProvider],
    backend_factory: Optional[BackendFactory],
    workspace_manager: Optional[WorkspaceManager] = None,
) -> RunSnapshot:
    """Process pending and interrupted jobs in declaration order."""
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
    for record in records:
        if record.terminal:
            continue
        while True:
            current = store.get_job(run_id, record.job.id)
            if current is None or current.terminal:
                break
            halted_endpoints = store.halted_endpoints(run_id)
            try:
                run_job(
                    store,
                    run_id,
                    record.job,
                    halted_endpoints=halted_endpoints,
                    timeout_s=timeout_s,
                    disallowed_tools=run_floor,
                    capabilities_provider=capabilities_provider,
                    backend_factory=backend_factory,
                    workspace_root=root,
                    workspace_manager=manager,
                )
            except SelectionError as exc:
                attempts = store.list_attempts(run_id, record.job.id)
                if attempts:
                    store.mark_halted(
                        run_id,
                        record.job.id,
                        _selection_halted_reason(
                            record.job, attempts, halted_endpoints
                        ),
                    )
                else:
                    store.mark_unroutable(run_id, record.job.id, str(exc))
                break
            except WorkspaceError:
                break
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
    """Create and execute a flat sequential run of jobs."""
    store_object = _store_object(store)
    identifier = run_id or uuid.uuid4().hex
    root = (
        Path(workspace_root).expanduser().resolve()
        if workspace_root is not None
        else default_workspace_root(identifier)
    )
    job_values = tuple(jobs)
    run_floor = _validate_disallowed_tools(disallowed_tools)
    workspace_manager = WorkspaceManager(root, job_values)
    store_object.create_run(
        identifier,
        job_values,
        jobs_path=jobs_path,
        max_parallel=max_parallel,
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
        disallowed_tools=job_file.disallowed_tools,
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
    run = store_object.get_run(run_id)
    if run is None:
        raise UnknownRunError(run_id)
    root = run.workspace_root or default_workspace_root(run_id)
    return _run_pending(
        store_object,
        run_id,
        workspace_root=root,
        timeout_s=timeout_s,
        capabilities_provider=capabilities_provider,
        backend_factory=backend_factory,
    )


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "CONTRACT_OUTPUT_LIMIT",
    "HALT_UNREACHABLE",
    "default_store_path",
    "default_workspace_root",
    "run_contract",
    "run_job",
    "replace_attempt_acceptance",
    "run_jobs",
    "run_job_file",
    "resume_run",
]
