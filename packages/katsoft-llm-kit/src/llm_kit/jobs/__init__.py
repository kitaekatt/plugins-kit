"""llm_kit.jobs: flat jobs, the schema-12 ledger and its migration loop, selection, the runner, workspaces, events and interrupts.

The job ledger keeps job-kit's registered operating rule: one process per
ledger. Two active processes on one ledger are unsupported and undetected, and
nothing here adds a lock, a lease or a refusal signal.

Depends only on ``llm_kit.completion``, ``llm_kit.registry``,
``llm_kit.dispatch``, ``llm_kit.execution`` and ``llm_kit.storage`` (plus the
CLI-owned ``llm_kit.bootstrap_guard``). The names below are this package's own
public surface; nothing is re-exported from the ``llm_kit`` root.
"""

__version__ = "0.13.0"

try:
    import llm_kit.dispatch.declaration  # noqa: F401
except ImportError as exc:
    from llm_kit.bootstrap_guard import is_provisioned, require_bootstrap

    # An import failure while bootstrap never provisioned this plugin is a
    # provisioning problem, named as one; once provisioned it is a real defect
    # and propagates with its own traceback.
    if not is_provisioned("llm-kit"):
        require_bootstrap(
            "llm-kit",
            feature="job execution",
            missing=exc.name or "llm_kit",
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
    "__version__",
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
