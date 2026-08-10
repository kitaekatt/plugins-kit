"""llm_scripting_kit.completion -- the completion seam.

One ``complete()`` protocol over three transports, so a pipeline can run the
same completion-shaped task against an OpenAI-compatible HTTP endpoint, the
local ``claude -p`` CLI, or the local ``codex exec`` CLI (both CLIs
subscription-billed) purely by configuration:

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
call time, so importing this package never requires the shared lib.
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
    classify_openai_exception,
)
from .types import BackendOptions, LLMBackend, LLMResponse

__all__ = [
    # seam types
    "LLMResponse",
    "BackendOptions",
    "LLMBackend",
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
    "CODEX_EXTRA_KEYS",
    "PROMPT_SEPARATOR",
    "compose_prompt",
]
