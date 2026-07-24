"""openrouter_kit.completion -- the completion seam.

One ``complete()`` protocol over two transports, so a pipeline can run the same
completion-shaped task against either an OpenAI-compatible HTTP endpoint or the
local ``claude -p`` CLI (subscription-billed) purely by configuration:

    from openrouter_kit.completion import (
        LLMResponse, BackendOptions, LLMBackend,
        OpenRouterBackend, ClaudeCliBackend,
        HaltError, classify_halt_text,
    )

The seam types (:class:`LLMResponse`, :class:`BackendOptions`,
:class:`LLMBackend`) and the halt taxonomy are stdlib-only; the ``claude -p``
subprocess runner is stdlib-only too. Only :class:`OpenRouterBackend` reaches
for the ``openai`` SDK, and only lazily -- the claude-cli transport works with
no ``openai`` installed.
"""
from __future__ import annotations

from .backends import ClaudeCliBackend, OpenRouterBackend
from .claude_runner import (
    AgentTimeoutError,
    HARD_STOP_STDERR_MARKERS,
    looks_like_hard_stop,
    run_claude_streaming,
)
from .halt import (
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    HaltError,
    classify_claude_exception,
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
    # claude -p runner core
    "AgentTimeoutError",
    "HARD_STOP_STDERR_MARKERS",
    "looks_like_hard_stop",
    "run_claude_streaming",
    # backends
    "OpenRouterBackend",
    "ClaudeCliBackend",
]
