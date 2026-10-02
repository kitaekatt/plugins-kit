"""job-kit: a durable runner for heterogeneous agent jobs."""

_TOP_LEVEL_SHARED_LIBS = frozenset({"llm_scripting_kit"})

try:
    import llm_scripting_kit.completion
except ImportError as exc:
    import sys as _sys

    from bootstrap_guard import EXIT_BOOTSTRAP_MISSING, is_provisioned, require_bootstrap

    if exc.name in _TOP_LEVEL_SHARED_LIBS:
        if is_provisioned("job-kit"):
            # bootstrap DID provision job-kit; the missing piece is the owning
            # sibling plugin, so name that remedy rather than blaming
            # job-kit's own provisioning.
            print(
                "[job-kit] llm-scripting-kit is required but not installed "
                f"or enabled (missing: {exc.name or 'llm_scripting_kit'}). "
                "Run `claude plugin install llm-scripting-kit@plugins-kit`.",
                file=_sys.stderr,
            )
            _sys.exit(EXIT_BOOTSTRAP_MISSING)
        require_bootstrap(
            "job-kit",
            feature="job execution",
            missing=exc.name or "llm_scripting_kit",
            force=True,
        )
    elif not is_provisioned("job-kit"):
        require_bootstrap(
            "job-kit",
            feature="job execution",
            missing=exc.name or "llm_scripting_kit",
        )
    raise

from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    COMPLETED,
    Contract,
    ContractContext,
    Continuation,
    ERROR,
    InterruptRecord,
    InterruptRequest,
    InterruptResolution,
    Job,
    JobFile,
    JobRecord,
    JobState,
    Prompt,
    RunRecord,
    RunSnapshot,
    RunState,
    TERMINAL_STATES,
    TIMEOUT,
    Usage,
    WorkspaceSpec,
    load_job_file,
)
from .run import (
    DEFAULT_TIMEOUT_S,
    default_store_path,
    default_workspace_root,
    resume_run,
    run_contract,
    run_job,
    run_job_file,
    run_jobs,
)
from .select import (
    EffortUndeliverableError,
    NoCompatibleEndpointError,
    SelectionError,
    SharedLibTooOldError,
    choose_endpoint,
    requirements_match,
    select_endpoint,
)
from .events import ExecutionEventSupportError
from .interrupts import (
    REQUEST_ENVELOPE_V1,
    RESOLUTION_ENVELOPE_V1,
    InterruptInputError,
    InterruptRequestError,
    JsonSchemaSupportError,
    parse_request,
    resolution_document,
)
from .store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DuplicateJobError,
    EventsNotRecordedError,
    InterruptExpiredError,
    JobStore,
    LedgerReader,
    ResolutionConflictError,
    ResolutionInputError,
    StoreError,
    StoreNotFoundError,
    TerminalStateError,
    UnknownInterruptError,
    UnknownJobError,
    UnknownRunError,
)
from .workspace import (
    WORKSPACE_REASON_DECLINED,
    WORKSPACE_REASON_NONE,
    WORKSPACE_STATUSES,
    WorkspaceCreationError,
    WorkspaceDetectionError,
    WorkspaceError,
    WorkspaceGCEntry,
    WorkspaceGCReport,
    WorkspaceManager,
    WorkspaceResolution,
    create_worktree,
    detect_repo_root,
    gc_workspaces,
    prepare_workspace,
    resolve_base_ref,
)

__all__ = [
    "Acceptance",
    "Attempt",
    "AttemptError",
    "COMPLETED",
    "Contract",
    "ContractContext",
    "Continuation",
    "ERROR",
    "InterruptRecord",
    "InterruptRequest",
    "InterruptResolution",
    "Job",
    "JobFile",
    "JobRecord",
    "JobState",
    "Prompt",
    "RunRecord",
    "RunSnapshot",
    "RunState",
    "TERMINAL_STATES",
    "TIMEOUT",
    "Usage",
    "WorkspaceSpec",
    "load_job_file",
    "DEFAULT_TIMEOUT_S",
    "default_store_path",
    "default_workspace_root",
    "resume_run",
    "run_contract",
    "run_job",
    "run_job_file",
    "run_jobs",
    "EffortUndeliverableError",
    "NoCompatibleEndpointError",
    "SelectionError",
    "SharedLibTooOldError",
    "choose_endpoint",
    "requirements_match",
    "select_endpoint",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DuplicateJobError",
    "EventsNotRecordedError",
    "ExecutionEventSupportError",
    "REQUEST_ENVELOPE_V1",
    "RESOLUTION_ENVELOPE_V1",
    "InterruptInputError",
    "InterruptRequestError",
    "JsonSchemaSupportError",
    "parse_request",
    "resolution_document",
    "InterruptExpiredError",
    "JobStore",
    "LedgerReader",
    "ResolutionConflictError",
    "ResolutionInputError",
    "StoreError",
    "StoreNotFoundError",
    "TerminalStateError",
    "UnknownInterruptError",
    "UnknownJobError",
    "UnknownRunError",
    "WORKSPACE_REASON_NONE",
    "WORKSPACE_REASON_DECLINED",
    "WORKSPACE_STATUSES",
    "WorkspaceCreationError",
    "WorkspaceDetectionError",
    "WorkspaceError",
    "WorkspaceGCEntry",
    "WorkspaceGCReport",
    "WorkspaceManager",
    "WorkspaceResolution",
    "create_worktree",
    "detect_repo_root",
    "gc_workspaces",
    "prepare_workspace",
    "resolve_base_ref",
]
