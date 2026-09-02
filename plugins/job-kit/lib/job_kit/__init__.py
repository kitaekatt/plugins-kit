"""job-kit: a durable runner for heterogeneous agent jobs."""

_TOP_LEVEL_SHARED_LIBS = frozenset({"bootstrap_lib", "llm_scripting_kit"})

try:
    import bootstrap_lib
    import llm_scripting_kit.completion
except ImportError as exc:
    from bootstrap_guard import is_provisioned, require_bootstrap

    if exc.name in _TOP_LEVEL_SHARED_LIBS:
        require_bootstrap(
            "job-kit",
            feature="job execution",
            missing=exc.name or "bootstrap_lib or llm_scripting_kit",
            force=True,
        )
    elif not is_provisioned("job-kit"):
        require_bootstrap(
            "job-kit",
            feature="job execution",
            missing=exc.name or "bootstrap_lib or llm_scripting_kit",
        )
    raise

from .model import (
    Acceptance,
    Attempt,
    AttemptError,
    COMPLETED,
    Contract,
    ContractContext,
    ERROR,
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
    NoCompatibleEndpointError,
    SelectionError,
    choose_endpoint,
    requirements_match,
    select_endpoint,
)
from .store import (
    DEFAULT_BUSY_TIMEOUT_MS,
    DuplicateJobError,
    JobStore,
    StoreError,
    StoreNotFoundError,
    TerminalStateError,
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
    "ERROR",
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
    "NoCompatibleEndpointError",
    "SelectionError",
    "choose_endpoint",
    "requirements_match",
    "select_endpoint",
    "DEFAULT_BUSY_TIMEOUT_MS",
    "DuplicateJobError",
    "JobStore",
    "StoreError",
    "StoreNotFoundError",
    "TerminalStateError",
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
