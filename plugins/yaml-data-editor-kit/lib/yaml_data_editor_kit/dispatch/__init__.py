"""The planner and content-pipeline-kit binding for editor dispatch."""

from .request import (
    DispatchRequest,
    DispatchRequestSet,
    DispatchSelection,
    RequestSet,
    load_request,
)

# Every module below reaches the content_pipeline shared library. A consumer whose
# venv lacks it gets one actionable message naming the remedy, rather than a bare
# ImportError from whichever submodule happened to be imported first. A nested
# ModuleNotFoundError raised from inside content_pipeline is a different fault and
# is re-raised untouched, so a real packaging bug is not mislabelled.
try:
    from .planner import (
        PLANNER_SYSTEM,
        AgenticCommentPlanner,
        CommentPlanStore,
        CommentPlanner,
        MechanicalCommentPlanner,
        PlannerPolicy,
        parse_grouping,
    )
    from .background import (
        BackgroundCommandRunner,
        BackgroundDispatchOptions,
        BackgroundDispatchStatus,
        BackgroundRef,
        BackgroundStagesRequiredError,
        BackgroundWaveSummary,
        DispatchInput,
        PreparedBackgroundDispatch,
        finalize_background_dispatch,
        get_background_dispatch_status,
        load_background_dispatch,
        prepare_background_dispatch,
        run_background_wave,
    )
    from .protocol import (
        QUESTION_FAILURE_PREFIX,
        QuestionProtocolError,
        build_dispatch_handlers,
        encode_question_failure,
        materialize_failed_questions,
    )
    from .run import RunSummary, StaleSliceError, dispatch, effective_result
    from .worker_mount import build_worker_command, main
except ImportError as exc:
    if getattr(exc, "name", "") not in ("content_pipeline", None) and not str(
        exc
    ).startswith("No module named 'content_pipeline'"):
        raise
    raise ModuleNotFoundError(
        "yaml-data-editor-kit dispatch requires the content_pipeline shared library; "
        "run the bootstrap dependency convergence action",
        name="content_pipeline",
    ) from exc

__all__ = [
    "PLANNER_SYSTEM",
    "QUESTION_FAILURE_PREFIX",
    "AgenticCommentPlanner",
    "BackgroundCommandRunner",
    "BackgroundDispatchOptions",
    "BackgroundDispatchStatus",
    "BackgroundRef",
    "BackgroundStagesRequiredError",
    "BackgroundWaveSummary",
    "CommentPlanStore",
    "CommentPlanner",
    "DispatchInput",
    "DispatchRequest",
    "DispatchRequestSet",
    "DispatchSelection",
    "MechanicalCommentPlanner",
    "PlannerPolicy",
    "PreparedBackgroundDispatch",
    "QuestionProtocolError",
    "RequestSet",
    "RunSummary",
    "StaleSliceError",
    "build_dispatch_handlers",
    "build_worker_command",
    "dispatch",
    "effective_result",
    "encode_question_failure",
    "finalize_background_dispatch",
    "get_background_dispatch_status",
    "load_background_dispatch",
    "load_request",
    "main",
    "materialize_failed_questions",
    "parse_grouping",
    "prepare_background_dispatch",
    "run_background_wave",
]
