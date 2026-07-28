"""Completion backends behind one protocol, selected by process-level routing.

Three transports implement :class:`~content_pipeline.llm.platform.LLMBackend`:

- :class:`OpenRouterBackend` -- an OpenAI-compatible HTTP completion.
- :class:`ClaudeCliBackend` -- the local ``claude -p`` CLI (subscription-billed,
  no per-call metering).
- :class:`MockBackend` -- deterministic, scriptable responses. The always-wins
  test seam: no network, no subprocess, no shared lib.

The two live transports are THIN ADAPTERS over ``llm_scripting_kit.completion``
(from llm-scripting-kit): that shared lib owns the actual completion
transport -- the ``claude -p`` subprocess runner, retry, timeout, hard-stop
detection, and the OpenAI-compatible client + prompt-cache message shaping.
This module keeps only the content-pipeline-specific glue; it does NOT
reimplement the transport. ``llm_scripting_kit`` is a LAZY / optional import: it is
reached for only when a live backend's ``complete`` / ``classify_halt`` actually
runs, so this module (and the MockBackend path, and process import graph
guards) load with no shared lib and no ``openai`` SDK installed. The per-call
options and the normalized response are adapted across the seam so this module's
:class:`~content_pipeline.llm.platform.BackendOptions` /
:class:`~content_pipeline.llm.platform.LLMResponse` stay the pipeline-facing
types regardless of provider.

:func:`route` reads a process-level env var (``CONTENT_PIPELINE_LLM_BACKEND``)
and returns the active backend -- backend selection is one process-wide switch
rather than a parameter threaded through every call site. The returned
response's ``model`` reflects the model that ACTUALLY ran, so audit stamping
stays truthful.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from content_pipeline.llm import platform
from content_pipeline.llm.platform import BackendOptions, LLMResponse

# The message a live backend raises when the shared lib is missing. The
# claude-cli / openrouter transports genuinely require llm_scripting_kit; only the
# MockBackend path is hermetic.
_MISSING_LIB_MSG = (
    "needs the 'llm_scripting_kit' shared lib (from llm-scripting-kit). Declare it "
    "via the plugin's shared_lib_imports, or use MockBackend for tests."
)


def _to_completion_options(opts: BackendOptions) -> Any:
    """Build an ``llm_scripting_kit.completion.BackendOptions`` from ours.

    Field-for-field: the two option bundles are intentionally identical, but the
    conversion is explicit so a future field drift surfaces here rather than
    silently mis-binding across the seam. Lazy import -- only reached once a live
    backend is actually driven.
    """
    from llm_scripting_kit.completion import BackendOptions as _CompletionOptions

    return _CompletionOptions(
        max_tokens=opts.max_tokens,
        temperature=opts.temperature,
        timeout_s=opts.timeout_s,
        cache_salt=opts.cache_salt,
        user_cache_prefix=opts.user_cache_prefix,
        effort=opts.effort,
        allowed_tools=opts.allowed_tools,
        cwd=opts.cwd,
        log_prefix=opts.log_prefix,
        extras=opts.extras,
    )


def _from_completion_response(resp: Any) -> LLMResponse:
    """Adapt an ``llm_scripting_kit.completion.LLMResponse`` into ours."""
    return LLMResponse(
        text=resp.text,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cache_hit_tokens=resp.cache_hit_tokens,
        wall_ms=resp.wall_ms,
        attempts=resp.attempts,
        from_cache=resp.from_cache,
    )


# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible HTTP) backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterBackend:
    """OpenAI-compatible HTTP completion, delegated to ``llm_scripting_kit``.

    ``endpoint`` / ``project_root`` / ``client`` are forwarded to
    ``llm_scripting_kit.completion.OpenRouterBackend`` (a caller may inject a
    pre-built ``client`` as the test seam). The delegate is built lazily on
    first use, so constructing this adapter never requires the shared lib.
    """

    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    name: str = field(default="openrouter", init=False)
    _delegate: Any = field(default=None, init=False, repr=False, compare=False)

    def _backend(self) -> Any:
        if self._delegate is None:
            try:
                from llm_scripting_kit.completion import (  # noqa: PLC0415
                    OpenRouterBackend as _CompletionOpenRouter,
                )
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ImportError(f"OpenRouterBackend {_MISSING_LIB_MSG}") from exc
            self._delegate = _CompletionOpenRouter(
                endpoint=self.endpoint,
                project_root=self.project_root,
                client=self.client,
            )
        return self._delegate

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        opts = options or BackendOptions()
        resp = self._backend().complete(
            system, user, model=model, options=_to_completion_options(opts)
        )
        return _from_completion_response(resp)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self._backend().classify_halt(exc)


# ---------------------------------------------------------------------------
# Claude CLI backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCliBackend:
    """Local ``claude -p`` completion, delegated to ``llm_scripting_kit``.

    Spawns the CLI in pure-completion mode (JSON output, no tools by default),
    enforces a per-call timeout, retries transient 5xx envelopes, and maps
    rate-limit / auth markers to a hard stop -- all inside
    ``llm_scripting_kit.completion.ClaudeCliBackend``. Cost is flat zero (the CLI
    bills at its own subscription, not per call).

    Config fields forward to the delegate; ``runner`` is the subprocess seam
    (``None`` uses the shared lib's battle-tested ``run_claude_streaming``). The
    delegate is built lazily so constructing this adapter never requires the
    shared lib.
    """

    default_timeout_s: float = 900.0
    retry_max_attempts: int = 3
    retry_cooldown_s: float = 60.0
    diagnostics_dir: Optional[Path] = None
    executable: Optional[str] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None
    name: str = field(default="claude-cli", init=False)
    _delegate: Any = field(default=None, init=False, repr=False, compare=False)

    def _backend(self) -> Any:
        if self._delegate is None:
            try:
                from llm_scripting_kit.completion import (  # noqa: PLC0415
                    ClaudeCliBackend as _CompletionClaudeCli,
                )
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise ImportError(f"ClaudeCliBackend {_MISSING_LIB_MSG}") from exc
            kwargs: Dict[str, Any] = {
                "default_timeout_s": self.default_timeout_s,
                "retry_max_attempts": self.retry_max_attempts,
                "retry_cooldown_s": self.retry_cooldown_s,
                "diagnostics_dir": self.diagnostics_dir,
                "executable": self.executable,
            }
            if self.runner is not None:
                kwargs["runner"] = self.runner
            self._delegate = _CompletionClaudeCli(**kwargs)
        return self._delegate

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        opts = options or BackendOptions()
        resp = self._backend().complete(
            system, user, model=model, options=_to_completion_options(opts)
        )
        return _from_completion_response(resp)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self._backend().classify_halt(exc)


# ---------------------------------------------------------------------------
# Mock backend (test seam)
# ---------------------------------------------------------------------------


@dataclass
class MockBackend:
    """Deterministic, scriptable completion backend for tests.

    Scriptable three ways:

    - ``responses`` -- a FIFO queue drained one per call. Each entry is a
      ``str`` (the response text), a ``dict`` (``{text, model?, input_tokens?,
      ...}``), an :class:`LLMResponse` (served verbatim), or an ``Exception``
      instance (raised, to exercise retry / halt paths).
    - ``keyed_responses`` -- a ``{substring: entry}`` map: the first key found
      in the user prompt wins (order-independent concurrency tests).
    - both empty -- every call raises ``RuntimeError("MockBackend exhausted")``.

    ``classify_halt`` maps a raised entry to a halt kind when its message
    carries a marker, so a scripted ``HaltError``-shaped exception halts. Every
    call's kwargs are recorded on ``self.calls``.
    """

    responses: Optional[List[Any]] = None
    keyed_responses: Optional[Dict[str, Any]] = None
    default_model: str = "mock-model"
    name: str = field(default="mock", init=False)
    calls: List[Dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._queue: List[Any] = list(self.responses) if self.responses else []

    def _coerce(self, entry: Any, model: str) -> LLMResponse:
        if isinstance(entry, LLMResponse):
            return entry
        if isinstance(entry, str):
            return LLMResponse(text=entry, model=model)
        if isinstance(entry, dict):
            return LLMResponse(
                text=str(entry.get("text", "")),
                model=str(entry.get("model", model)),
                input_tokens=int(entry.get("input_tokens", 0)),
                output_tokens=int(entry.get("output_tokens", 0)),
                cache_hit_tokens=int(entry.get("cache_hit_tokens", 0)),
                wall_ms=int(entry.get("wall_ms", 0)),
            )
        raise TypeError(
            f"MockBackend: unsupported response entry {type(entry).__name__}"
        )

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        self.calls.append(
            {"system": system, "user": user, "model": model, "options": options}
        )
        effective_model = model or self.default_model
        if self.keyed_responses is not None:
            for substring, entry in self.keyed_responses.items():
                if substring in user:
                    if isinstance(entry, BaseException):
                        raise entry
                    return self._coerce(entry, effective_model)
            raise RuntimeError(
                f"MockBackend keyed_responses: no key matched (keys: "
                f"{list(self.keyed_responses)})"
            )
        if not self._queue:
            raise RuntimeError("MockBackend exhausted")
        entry = self._queue.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return self._coerce(entry, effective_model)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        if isinstance(exc, platform.HaltError):
            return exc.kind
        return platform.classify_halt_text(str(exc))


# ---------------------------------------------------------------------------
# Process-level routing
# ---------------------------------------------------------------------------

BACKEND_ENV = "CONTENT_PIPELINE_LLM_BACKEND"
"""Process-level backend selection env var (empty / unset => openrouter)."""

MODEL_ENV = "CONTENT_PIPELINE_LLM_MODEL"
"""Optional override for the model a routed non-openrouter backend runs."""


def active_backend_name() -> str:
    """The process-active backend name (``"openrouter"`` when unset)."""
    return os.environ.get(BACKEND_ENV, "").strip() or "openrouter"


def set_active_backend(name: Optional[str]) -> None:
    """Set (or clear, with ``None`` / ``"openrouter"``) the active backend.

    Writes the env var rather than module state so worker subprocesses inherit
    the selection.
    """
    if name and name != "openrouter":
        os.environ[BACKEND_ENV] = name
    else:
        os.environ.pop(BACKEND_ENV, None)


def route(
    *,
    openrouter: Optional[Any] = None,
    claude_cli: Optional[Any] = None,
    mock: Optional[Any] = None,
) -> Any:
    """Return the process-active backend instance.

    Reads :data:`BACKEND_ENV`. A caller-supplied instance for the active name
    wins (the mock seam always wins so tests never route to a live transport);
    otherwise a default instance is constructed.
    """
    name = active_backend_name()
    if name == "mock":
        return mock if mock is not None else MockBackend()
    if name == "claude-cli":
        return claude_cli if claude_cli is not None else ClaudeCliBackend()
    return openrouter if openrouter is not None else OpenRouterBackend()


def routed_model(requested_model: str, *, backend_name: Optional[str] = None) -> str:
    """Resolve the model a routed call should run, with truthful substitution.

    An OpenRouter-style slug means nothing to the claude-cli transport, so a
    routed non-openrouter call substitutes :data:`MODEL_ENV` when the requested
    id does not already name that backend's family. The substituted id is what
    lands on ``LLMResponse.model`` and therefore on audit records.
    """
    name = backend_name or active_backend_name()
    if name == "claude-cli" and not requested_model.startswith("claude"):
        return os.environ.get(MODEL_ENV, "").strip() or requested_model
    return requested_model


__all__ = [
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "MockBackend",
    "BACKEND_ENV",
    "MODEL_ENV",
    "active_backend_name",
    "set_active_backend",
    "route",
    "routed_model",
]
