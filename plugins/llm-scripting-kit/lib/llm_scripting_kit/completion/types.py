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
from typing import Any, Dict, Mapping, Optional, Protocol, Tuple, runtime_checkable


# -- call status -----------------------------------------------------------

COMPLETED = "completed"
"""The adapter produced a result; ``error`` is None."""

TIMEOUT = "timeout"
"""The per-call wall-clock budget was spent and the target was killed."""

ERROR = "error"
"""The call failed for any other reason; ``error`` carries the detail."""


@dataclass(frozen=True)
class ResponseError:
    """A failure reported AS DATA rather than raised.

    ``code`` is a stable, machine-branchable token (the halt taxonomy's own
    labels where one applies, else a short adapter-neutral slug). ``message``
    is human text and is never parsed by this layer.

    Where this appears is deliberate and narrow: the PACKAGE API keeps raising
    typed exceptions -- every existing consumer branches on them, and turning a
    raise into a return would make a failure read as a success at call sites
    that never asked for the new contract. The error-as-data envelope is
    emitted at the CLI surface, which is a new protocol with no such history.
    """

    code: str
    message: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


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
      success; 1 on a cache hit). Under run-once this is 1 unless the CALLER
      opted a backend into a retry budget, so a value above 1 is now evidence
      of caller policy rather than of hidden adapter behavior.
    - ``from_cache`` -- True when served from a response cache.

    Truthfulness fields -- what actually happened on this call, as opposed to
    what the adapter can do in general (that is the advertisement's job):

    - ``status`` -- :data:`COMPLETED`, :data:`TIMEOUT` or :data:`ERROR`. The
      package API still RAISES on failure, so a response handed back by
      ``complete()`` always reads ``completed``; the other two are produced
      where failures become data (the CLI envelope, a consumer that adapts a
      caught exception into a result).
    - ``error`` -- the :class:`ResponseError` detail, present iff ``status`` is
      not ``completed``.
    - ``dropped_params`` -- the params this CALL requested that the adapter does
      not read, so nothing is silently ignored. Derived from the adapter's own
      advertised ``dropped_params`` (see :mod:`.results`), never from a second
      hand-maintained list, and narrowed to params the caller actually set:
      reporting a default the caller never touched would be noise, not truth.
      An ``extras`` key appears here per key (``extras.foo``), never as the bare
      field name, because on an adapter that reads some extras keys and not
      others the field name has no single answer.
    - ``forwarded_params`` -- params the adapter sent downstream WITHOUT
      validating them: openrouter copies every ``extras`` key into the request
      as a top-level parameter and makes no claim the provider accepts any of
      them. Deliberately not folded into ``dropped_params``, because the two
      carry opposite advice -- a dropped param had no effect and should be
      removed, a forwarded one reached the provider and may be doing its job.
      Merging them would tell a caller to delete a param that works.
    - ``execution_controls_applied`` -- ids of the advertised
      :class:`~.capabilities.ExecutionControl` records this call emitted. It
      reports EMISSION only, exactly as the advertisement does -- never that the
      target complied.
    - ``structured`` -- parsed schema-backed output, present only where the
      adapter advertises ``result: parsed`` AND a schema was honored natively.
      ``None`` everywhere else, including where a schema was sent but the result
      came back as text.
    - ``started_at`` / ``ended_at`` -- ISO-8601 UTC timestamps bracketing the
      live call (``None`` on a cache hit, where no live call happened).

    A note for anyone adding another field here, learned from ``total_tokens``
    above: think about what a consumer will SUM or COUNT. None of the fields
    added above is summable across backends, which is deliberate --
    ``dropped_params``, ``forwarded_params`` and ``execution_controls_applied``
    are sets of names, not magnitudes, so no downstream aggregate can
    double-count them.
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
    status: str = COMPLETED
    error: Optional[ResponseError] = None
    dropped_params: Tuple[str, ...] = ()
    forwarded_params: Tuple[str, ...] = ()
    execution_controls_applied: Tuple[str, ...] = ()
    structured: Optional[Any] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


@dataclass(frozen=True)
class BackendOptions:
    """Per-call knobs, some understood by only one transport.

    ``max_tokens`` / ``temperature`` are common to the protocol shape, but a
    CLI may not expose either flag. The remaining fields are transport-specific
    and documented as ignored by transports that do not honor them (documented,
    not silent):

    - ``timeout_s`` -- per-call wall-clock cap. ``None`` uses the backend
      default.
    - ``cache_salt`` -- per-attempt salt for malformed-response retry loops so a
      retry does not replay a cached bad response. 0 (default) keeps a caller's
      cache key stable. The claude-cli transport has no response cache and
      ignores it.
    - ``user_cache_prefix`` -- static-across-cells leading block of the user
      message, used for a second prompt-cache breakpoint (OpenRouter only).
    - ``effort`` -- thinking-budget / provider-variant flag (claude-cli,
      codex-cli, and opencode; the spelling is transport-specific).
    - ``allowed_tools`` -- claude-cli ``--allowedTools`` value. ``None`` (the
      default) means a pure completion (no tools). Only read-only vision use
      (``"Read"``) is sanctioned; agentic tool sets are out of scope here.
    - ``cwd`` -- CLI working directory. ``None`` uses the process cwd; an
      OpenCode ``--dir`` is not a filesystem-confinement boundary.
    - ``log_prefix`` -- stderr tag so mixed logs from parallel runs stay
      attributable.
    - ``extras`` -- open map for consumer-specific knobs a backend may read.
      What happens to a key is per-adapter and advertised per key: codex reads
      a named set (``output_schema``, ``sandbox``, ...), openrouter forwards
      every key unvalidated, and claude-cli and opencode-cli read none. A key
      that reaches nothing is reported in ``LLMResponse.dropped_params``; one
      forwarded without validation is reported in ``forwarded_params``.
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


__all__ = [
    "LLMResponse",
    "BackendOptions",
    "LLMBackend",
    "ResponseError",
    "COMPLETED",
    "TIMEOUT",
    "ERROR",
]
