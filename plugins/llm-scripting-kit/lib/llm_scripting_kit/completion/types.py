"""Seam types shared by every completion transport.

The normalized response, the per-call options bundle, and the backend
protocol -- stdlib-only so a consumer can import them without the ``openai``
SDK. All per-call knobs live on :class:`BackendOptions` (some understood by
only one transport, documented as ignored elsewhere) so the
:class:`LLMBackend` protocol signature stays uniform across transports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@dataclass
class LLMResponse:
    """Normalized result of one completion call.

    - ``text`` -- assistant message content ("" when the provider returned
      nothing).
    - ``model`` -- the model id that ACTUALLY served the call.
    - ``input_tokens`` / ``output_tokens`` -- usage reported by the provider.
    - ``cache_hit_tokens`` -- prompt-cache hit tokens the provider surfaced
      (0 when unsupported).
    - ``total_tokens`` -- a TOTAL-ONLY usage figure a provider surfaces with no
      input/output split (the codex-cli transport; 0 for every other backend).
      Deliberately a SEPARATE field rather than folded into ``output_tokens``:
      the input/output fields feed per-directional cost formulas elsewhere
      (e.g. content-pipeline-kit's cost estimator multiplies ``output_tokens``
      by an output-token price), and stuffing an undifferentiated total into
      ``output_tokens`` would silently misprice a call. A consumer that sums
      ``input_tokens + output_tokens`` across backends is unaffected -- this
      field simply stays 0 there -- but summing it in blindly alongside those
      two would double-count on any backend that also reports a split, so a
      caller that wants "however many tokens this call used, however the
      backend reports it" must read whichever of ``total_tokens`` or
      ``input_tokens + output_tokens`` is nonzero, not both.
    - ``wall_ms`` -- wall-clock duration of the live call in milliseconds. On a
      cache hit this is the ORIGINAL live call's duration.
    - ``attempts`` -- number of completion attempts made (1 on first-try
      success; 1 on a cache hit).
    - ``from_cache`` -- True when served from a response cache.
    """

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    total_tokens: int = 0
    wall_ms: int = 0
    attempts: int = 1
    from_cache: bool = False


@dataclass(frozen=True)
class BackendOptions:
    """Per-call knobs, some understood by only one transport.

    ``max_tokens`` / ``temperature`` are common to every completion transport.
    The remaining fields are transport-specific and documented as ignored by
    transports that do not honor them (documented, not silent):

    - ``timeout_s`` -- per-call wall-clock cap. ``None`` uses the backend
      default.
    - ``cache_salt`` -- per-attempt salt for malformed-response retry loops so a
      retry does not replay a cached bad response. 0 (default) keeps a caller's
      cache key stable. The claude-cli transport has no response cache and
      ignores it.
    - ``user_cache_prefix`` -- static-across-cells leading block of the user
      message, used for a second prompt-cache breakpoint (OpenRouter only).
    - ``effort`` -- thinking-budget flag ``--effort low|medium|high``
      (claude-cli only).
    - ``allowed_tools`` -- claude-cli ``--allowedTools`` value. ``None`` (the
      default) means a pure completion (no tools). Only read-only vision use
      (``"Read"``) is sanctioned; agentic tool sets are out of scope here.
    - ``cwd`` -- claude-cli working directory. ``None`` uses the process cwd.
    - ``log_prefix`` -- stderr tag so mixed logs from parallel runs stay
      attributable.
    - ``extras`` -- open map for consumer-specific knobs a backend may read.
    """

    max_tokens: int = 4096
    temperature: float = 0.3
    timeout_s: Optional[float] = None
    cache_salt: int = 0
    user_cache_prefix: str = ""
    effort: Optional[str] = None
    allowed_tools: Optional[str] = None
    cwd: Optional[Path] = None
    log_prefix: str = "[llm]"
    extras: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMBackend(Protocol):
    """A transport that runs one completion and classifies its failures."""

    name: str  # audit provider label, e.g. "openrouter" / "claude-cli"

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse: ...

    def classify_halt(self, exc: BaseException) -> Optional[str]: ...


__all__ = ["LLMResponse", "BackendOptions", "LLMBackend"]
