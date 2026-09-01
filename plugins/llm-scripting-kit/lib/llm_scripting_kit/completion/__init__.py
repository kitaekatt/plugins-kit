"""llm_scripting_kit.completion -- the completion seam.

One ``complete()`` protocol over four transports, so a pipeline can run the
same completion-shaped task against an OpenAI-compatible HTTP endpoint, the
local ``claude -p`` CLI, the local ``codex exec`` CLI, or the local
``opencode run`` CLI purely by configuration:

    from llm_scripting_kit.completion import (
        LLMResponse, BackendOptions, LLMBackend,
        OpenRouterBackend, ClaudeCliBackend, CodexCliBackend,
        HaltError, classify_halt_text,
    )

The seam types (:class:`LLMResponse`, :class:`BackendOptions`,
:class:`LLMBackend`) and the halt taxonomy are stdlib-only; the ``claude -p``
subprocess runner is stdlib-only too. Only :class:`OpenRouterBackend` reaches
for the ``openai`` SDK, and only lazily -- the claude-cli transport works with
no ``openai`` installed. :class:`CodexCliBackend` likewise defers its one
non-stdlib import (``bootstrap_lib.codex``, the argv single source of truth) to
call time, so importing this package never requires the shared lib. The
OpenCode adapter resolves its launcher only when a call is dispatched.
"""
from __future__ import annotations

from .backends import ClaudeCliBackend, OpenRouterBackend
from .claude_runner import (
    AgentTimeoutError,
    HARD_STOP_STDERR_MARKERS,
    looks_like_hard_stop,
    run_claude_streaming,
    run_cli_streaming,
)
from .codex_backend import (
    CODEX_EXTRA_KEYS,
    PROMPT_SEPARATOR,
    CodexCliBackend,
    CodexRunError,
    compose_prompt,
)
from .halt import (
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    HaltError,
    classify_claude_exception,
    classify_codex_exception,
    classify_codex_text,
    classify_halt_text,
    classify_opencode_exception,
    classify_openai_exception,
)
from .adapter_capabilities import ADAPTER_CAPABILITIES, adapter_capabilities
from .capabilities import (
    Capabilities,
    ExecutionControl,
    ParamCapability,
    StructuredOutputCapability,
    SystemPromptCapability,
)
from .requirements import match_capabilities
from .results import (
    caller_set_params,
    check_applied_controls,
    derive_dropped_params,
    derive_extras_report,
    derive_forwarded_params,
    fixed_control_ids,
    utc_now_iso,
)
from .types import (
    COMPLETED,
    ERROR,
    TIMEOUT,
    BackendOptions,
    EmptyCompletionError,
    LLMBackend,
    LLMResponse,
    ResponseError,
)
from .factory import BackendSelection, create_backend
from .opencode_backend import (
    DEFAULT_OPENCODE_TIMEOUT_S,
    OPENCODE_FILESYSTEM_POSTURE,
    OPENCODE_PROMPT_SEPARATOR,
    OpencodeCliBackend,
    OpencodeRunError,
)

__all__ = [
    # seam types
    "LLMResponse",
    "EmptyCompletionError",
    "ResponseError",
    "COMPLETED",
    "TIMEOUT",
    "ERROR",
    # per-call truthfulness derivations
    "utc_now_iso",
    "caller_set_params",
    "derive_dropped_params",
    "derive_extras_report",
    "derive_forwarded_params",
    "fixed_control_ids",
    "check_applied_controls",
    # capability advertisement
    "Capabilities",
    "ParamCapability",
    "ExecutionControl",
    "StructuredOutputCapability",
    "SystemPromptCapability",
    "ADAPTER_CAPABILITIES",
    "adapter_capabilities",
    "match_capabilities",
    "BackendOptions",
    "LLMBackend",
    "BackendSelection",
    "create_backend",
    # halt taxonomy
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "HaltError",
    "classify_halt_text",
    "classify_openai_exception",
    "classify_claude_exception",
    "classify_codex_text",
    "classify_codex_exception",
    "classify_opencode_exception",
    # CLI runner core
    "AgentTimeoutError",
    "HARD_STOP_STDERR_MARKERS",
    "looks_like_hard_stop",
    "run_cli_streaming",
    "run_claude_streaming",
    # backends
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "CodexCliBackend",
    "CodexRunError",
    "OpencodeCliBackend",
    "OpencodeRunError",
    "DEFAULT_OPENCODE_TIMEOUT_S",
    "OPENCODE_FILESYSTEM_POSTURE",
    "OPENCODE_PROMPT_SEPARATOR",
    "CODEX_EXTRA_KEYS",
    "PROMPT_SEPARATOR",
    "compose_prompt",
]
