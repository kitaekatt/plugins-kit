"""LLM platform shim: transport / retry / cache / cost / budget / validate-loop.

The pipeline-shaped layer every batch LLM call site runs through. It owns the
concerns that are the SAME regardless of which provider serves the call --
retry, halt classification, a per-call timeout budget, a content-addressed
response cache, cost accounting, a token/cost budget guard, and the
validate-until-valid submission loop -- and delegates the actual completion to
a :class:`LLMBackend` (see ``backends``). It does NOT decide which backend or
model to call; that is ``backends``'s job.

Key resolution, the model registry, and the ready-made OpenAI-compatible
client are NOT reimplemented here -- ``backends.OpenRouterBackend`` consumes
them from ``llm_scripting_kit`` (an ImportError-tolerant optional seam). This
module is stdlib-only plus the two permitted cross-package imports
(``freshness.hashing`` for the cache key, ``validate.contract`` for the
submission loop).

Public surface:

- :class:`LLMResponse` -- the normalized completion result.
- :class:`EmptyCompletionError` -- an empty completion after its retry budget
  is exhausted.
- :class:`BackendOptions` -- per-call knobs (some understood by only one
  transport, documented on the field).
- :class:`LLMBackend` -- the completion protocol.
- :class:`PipelineHaltError` (aliased as ``HaltError`` for compatibility) and
  :func:`classify_openai_exception` / :func:`classify_halt_text` -- the shared
  halt taxonomy. A *halt* is a failure that persists across subsequent calls
  (rate-limit / auth / insufficient-credit), so a bulk runner catching
  :class:`PipelineHaltError` stops cleanly instead of burning the rest of the
  corpus. Distinct from and unrelated to
  ``llm_scripting_kit.completion.halt.HaltError``, which shares only the old
  name -- see ``plugins/CLAUDE.md`` ("Duplicated seam types across a
  shared-lib boundary").
- :func:`call_llm` -- the single entry point: budget guard, cache, retry,
  halt-mapping, cost accounting.
- :func:`is_likely_reasoning_exhaustion` / :func:`describe_likely_reasoning_exhaustion`
  -- distinguish an empty-but-token-spending response (reasoning model ran out
  of ``max_tokens`` before emitting visible text) from a genuinely empty one.
  Surfaced on :class:`LLMResponse` as ``likely_reasoning_exhausted``.
- :func:`build_cache_key` / :class:`ResponseCache` -- the content-addressed
  cache (pluggable directory; empty responses never cached).
- :func:`estimate_cost` / :func:`response_cost` / :func:`load_pricing` -- cost
  against a pricing table; an unknown model is a hard :class:`KeyError` by
  design (a typo must never silently price at 0.0).
- :func:`estimate_request_tokens` / :func:`check_request_fits` /
  :class:`CostBudget` / :class:`BudgetExceededError` -- the budget guards.
- :func:`submit_validated` / :class:`SubmitResult` -- the validate-until-valid
  loop, taking validators from ``validate.contract``.
- :func:`evaluate_submission` / :class:`ValidationSpec` /
  :class:`EvaluationResult` -- the pure parse-then-validate judgment
  ``submit_validated`` drives its loop with, extracted so an out-of-process
  worker can reuse the exact same verdict logic without the retry/backend/
  cache machinery around it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import (
    Any,
    Callable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from content_pipeline.freshness import hashing
from content_pipeline.validate import contract

# ---------------------------------------------------------------------------
# Response / options / protocol
# ---------------------------------------------------------------------------


@dataclass
class LLMResponse:
    """Normalized result of one completion call.

    - ``text`` -- the assistant message content ("" when the provider
      returned nothing).
    - ``model`` -- the model id that ACTUALLY served the call (the routing
      layer substitutes here so audit records stay truthful; see
      ``backends.route``).
    - ``input_tokens`` / ``output_tokens`` -- usage reported by the provider.
    - ``cache_hit_tokens`` -- prompt-cache hit tokens the provider surfaced
      (0 when unsupported).
    - ``wall_ms`` -- wall-clock duration of the live call in milliseconds. On
      a cache hit this is the ORIGINAL live call's duration, not the lookup
      time, so latency totals stay meaningful under cache-resume.
    - ``attempts`` -- number of completion attempts made (1 on first-try
      success; 1 on a cache hit).
    - ``from_cache`` -- True when served from :class:`ResponseCache`.
    - ``total_tokens`` -- an UNDIFFERENTIATED total, reported by a transport
      that surfaces no input/output split (codex-cli). 0 everywhere else. It is
      a separate field rather than a value folded into ``output_tokens``
      because the cost estimator multiplies that field by a per-output-token
      price -- a total placed there would compute a fabricated dollar figure.
      For the same reason do NOT add it to ``input_tokens + output_tokens``;
      it is an alternative to them, not a component.
    - ``likely_reasoning_exhausted`` -- True when ``text`` is empty (or
      whitespace-only) while ``output_tokens`` is nonzero. That shape is the
      signature of a reasoning model that spent its entire ``max_tokens``
      budget on hidden reasoning before emitting any visible text -- it is a
      STRONG INFERENCE from the token/text shape, not a certainty. It is
      distinct from a genuinely empty response (``output_tokens == 0``, e.g.
      the provider returned nothing at all), which leaves this False; only
      the former is plausibly fixed by raising ``max_tokens``. Set
      automatically by :func:`call_llm` (and therefore
      :func:`submit_validated`) after each LIVE completion, via
      :func:`is_likely_reasoning_exhaustion`; defaults to False so existing
      construction sites (a cache hit, a hand-built ``LLMResponse`` in a
      test) are unaffected. Use :func:`describe_likely_reasoning_exhaustion`
      for a human-readable diagnostic naming the likely cause and remedy.
    - ``status`` / ``error`` -- the result status and optional structured error
      detail from the completion seam.
    - ``dropped_params`` -- options requested by the caller but ignored by the
      adapter. An ``extras`` key appears per key (``extras.foo``).
    - ``forwarded_params`` -- options the adapter sent downstream without
      validating them. Kept separate from ``dropped_params`` because the two
      carry opposite advice: a dropped param had no effect, a forwarded one
      reached the provider and may be working.
    - ``execution_controls_applied`` -- execution controls emitted by the
      adapter for this call.
    - ``structured`` -- schema-backed parsed output when the adapter provides
      it, otherwise ``None``.
    - ``started_at`` / ``ended_at`` -- ISO-8601 UTC timestamps for a live call;
      both are ``None`` on a cache hit.
    - ``reasoning`` / ``finish_reason`` -- optional transport diagnostics,
      retained so empty responses can be logged without reaching through the
      adapter boundary.
    """

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    wall_ms: int = 0
    attempts: int = 1
    from_cache: bool = False
    total_tokens: int = 0
    likely_reasoning_exhausted: bool = False
    status: str = "completed"
    error: Optional[Any] = None
    dropped_params: tuple[str, ...] = ()
    forwarded_params: tuple[str, ...] = ()
    execution_controls_applied: tuple[str, ...] = ()
    structured: Optional[Any] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    # Trailing fields preserve existing positional construction sites.
    reasoning: str = ""
    finish_reason: Optional[str] = None


class EmptyCompletionError(RuntimeError):
    """A priced completion attempt produced no visible assistant text.

    ``llm_scripting_kit`` classifies ordinary empty responses and leaves the
    halt decision to this run-level consumer. This local type keeps the
    pipeline import-safe when that shared library is absent, while carrying
    the diagnostic data operators see in the stderr record.
    """

    def __init__(
        self,
        message: str,
        *,
        model: str,
        finish_reason: Optional[str],
        input_tokens: int,
        output_tokens: int,
        attempt: int,
        max_attempts: int,
        likely_reasoning_exhausted: bool,
        reasoning: str = "",
        reasoning_tail: str = "",
    ) -> None:
        super().__init__(message)
        self.model = model
        self.finish_reason = finish_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.likely_reasoning_exhausted = likely_reasoning_exhausted
        self.reasoning = reasoning
        self.reasoning_tail = reasoning_tail

    def response(self) -> LLMResponse:
        """Return the failed attempt as an audit response for validation."""
        return LLMResponse(
            text="",
            model=self.model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            attempts=self.attempt,
            likely_reasoning_exhausted=self.likely_reasoning_exhausted,
            status="error",
            error={"code": "empty_completion", "message": str(self)},
            reasoning=self.reasoning,
            finish_reason=self.finish_reason,
        )


@dataclass(frozen=True)
class BackendOptions:
    """Per-call knobs, some understood by only one transport.

    ``max_tokens`` / ``temperature`` are common to every completion transport.
    The remaining fields are transport-specific and documented as ignored by
    transports that do not honor them (documented, not silent):

    - ``temperature`` -- optional sampling control. ``None`` lets the
      server/model choose its mode-aware default; an explicit value is sent.
    - ``timeout_s`` -- per-call wall-clock cap. ``None`` uses the backend
      default.
    - ``cache_salt`` -- per-attempt salt for malformed-response retry loops so
      a retry does not replay the cached bad response. 0 (default) keeps the
      cache key stable.
    - ``user_cache_prefix`` -- static-across-cells leading block of the user
      message, used for a second prompt-cache breakpoint (OpenRouter only).
      When set it also participates in the response-cache key so distinct
      prefixes never collide on one cached response.
    - ``effort`` -- thinking-budget flag (claude-cli only).
    - ``allowed_tools`` -- claude-cli ``--allowedTools`` value. ``None`` means
      a pure completion (no tools).
    - ``cwd`` -- claude-cli working directory.
    - ``log_prefix`` -- stderr tag so mixed logs from parallel runs stay
      attributable.
    - ``extras`` -- open map for consumer-specific knobs a backend may read.
    """

    max_tokens: int = 4096
    temperature: Optional[float] = None
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

    name: str  # audit provider label, e.g. "openrouter" / "claude-cli" / "mock"

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        ...

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        ...


# ---------------------------------------------------------------------------
# Halt taxonomy
# ---------------------------------------------------------------------------

HALT_AUTH = "auth"
"""Bad or missing credentials (HTTP 401 / logged-out CLI)."""

HALT_RATE_LIMIT = "rate_limit"
"""Quota exhausted (HTTP 429 / provider cap); clears after a window."""

HALT_INSUFFICIENT_CREDIT = "insufficient_credit"
"""Account credit exhausted (402) or suspended (403)."""

HALT_UNREACHABLE = "unreachable"
"""The backend's endpoint could not be reached at all (connection refused,
DNS failure, timeout on connect).

A halt rather than a retryable error BECAUSE of what this registry actually
holds: manually-run servers. A cloud provider blipping is transient and worth
a retry; a llama.cpp server that is not running does not start itself, so
every subsequent unit in the run would burn its own timeout discovering the
same thing. Should a registry entry ever point at a managed always-on service,
that entry -- not this constant -- is where the exception belongs.
"""


class PipelineHaltError(Exception):
    """A failure that persists across subsequent calls -- stop the bulk run.

    Carries a machine-readable ``kind`` (one of :data:`HALT_AUTH`,
    :data:`HALT_RATE_LIMIT`, :data:`HALT_INSUFFICIENT_CREDIT`) so a bulk
    runner can halt-and-resume without parsing the message text. ``call_llm``
    raises this when a backend classifies the underlying exception as a halt;
    a non-halt failure propagates as its original exception type.

    Named distinctly from ``llm_scripting_kit.completion.halt.HaltError`` --
    the two share no relationship beyond the pre-rename name, and a caller
    that caught the old name around an ``llm_scripting_kit`` call while
    meaning this class (or vice versa) would get a handler that silently
    never fired. See ``plugins/CLAUDE.md`` ("Duplicated seam types across a
    shared-lib boundary").
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


#: Compatibility alias for the pre-rename name. Prefer
#: :class:`PipelineHaltError` in new code; this alias exists so importers of
#: the old ``content_pipeline.llm.platform.HaltError`` name keep working.
HaltError = PipelineHaltError


# Marker substrings signalling a text-channel hard stop. Two shape families
# exist in the wild: JSON-quoted (``"api_error_status":429``) and bare
# (``api_error_status:429``). All matching is lowercased.
_RATE_LIMIT_MARKERS = (
    "hit your limit",
    '"api_error_status":429',
    "api_error_status:429",
)
_AUTH_MARKERS = (
    '"api_error_status":401',
    "api_error_status:401",
    "authentication_error",
    "invalid authentication credentials",
)


def classify_halt_text(text: str) -> Optional[str]:
    """Map a provider text channel (error body / stderr) to a halt kind.

    Rate-limit markers are checked before auth markers, so a message
    carrying both classifies as :data:`HALT_RATE_LIMIT`. Returns ``None``
    when no marker matches.
    """
    if not text:
        return None
    lower = text.lower()
    for marker in _RATE_LIMIT_MARKERS:
        if marker in lower:
            return HALT_RATE_LIMIT
    for marker in _AUTH_MARKERS:
        if marker in lower:
            return HALT_AUTH
    return None


def _classify_openai_exception_local(exc: BaseException) -> Optional[str]:
    """CPK's own OpenAI-SDK exception classifier (no delegation).

    Returns :data:`HALT_AUTH`, :data:`HALT_RATE_LIMIT`, or
    :data:`HALT_INSUFFICIENT_CREDIT` for the known persistent failures;
    ``None`` otherwise. The ``openai`` import is optional -- when absent, the
    text-marker fallback still catches the common shapes. Recurses on
    ``__cause__`` so a wrapped SDK exception is still classified.

    This is the fallback :func:`classify_openai_exception` uses when
    ``llm_scripting_kit`` is not importable, and its own logic when it is --
    the two are kept as separate functions so the delegation in
    :func:`classify_openai_exception` is a single, obvious try/except rather
    than interleaved with the classification rules themselves.
    """
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        openai = None  # type: ignore[assignment]
    if openai is not None:
        auth_error = getattr(openai, "AuthenticationError", None)
        if auth_error is not None and isinstance(exc, auth_error):
            return HALT_AUTH
        rate_error = getattr(openai, "RateLimitError", None)
        if rate_error is not None and isinstance(exc, rate_error):
            return HALT_RATE_LIMIT
        status_error = getattr(openai, "APIStatusError", None)
        if status_error is not None and isinstance(exc, status_error):
            if getattr(exc, "status_code", None) in (402, 403):
                return HALT_INSUFFICIENT_CREDIT
    from_text = classify_halt_text(str(exc))
    if from_text is not None:
        return from_text
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _classify_openai_exception_local(cause)
    return None


def classify_openai_exception(exc: BaseException) -> Optional[str]:
    """Map an OpenAI-SDK exception (or one wrapping it) to a halt kind.

    Returns :data:`HALT_AUTH`, :data:`HALT_RATE_LIMIT`, or
    :data:`HALT_INSUFFICIENT_CREDIT` for the known persistent failures;
    ``None`` otherwise.

    Delegates to ``llm_scripting_kit.completion.halt.classify_openai_exception``
    when that shared lib is importable -- the two classifiers implement the
    same rules against the same category vocabulary (``HALT_AUTH == "auth"``,
    ``HALT_RATE_LIMIT == "rate_limit"``, ``HALT_INSUFFICIENT_CREDIT ==
    "insufficient_credit"`` on both sides, verified against
    ``tests/llm-scripting-kit/test_completion_halt.py`` and this module's own
    tests), so delegating changes no observable behaviour today and avoids
    maintaining the duplicate. The import is lazy and optional, matching the
    pattern used elsewhere in this module (see
    :func:`_classify_openai_exception_local` above and
    ``backends._is_connection_error``): when ``llm_scripting_kit`` is absent,
    this falls back to CPK's own classifier so content-pipeline-kit keeps
    working without the shared lib installed.
    """
    try:
        from llm_scripting_kit.completion.halt import (  # noqa: PLC0415
            classify_openai_exception as _lsk_classify_openai_exception,
        )
    except ImportError:
        _lsk_classify_openai_exception = None  # type: ignore[assignment]
    if _lsk_classify_openai_exception is not None:
        return _lsk_classify_openai_exception(exc)
    return _classify_openai_exception_local(exc)


# ---------------------------------------------------------------------------
# Reasoning-exhaustion diagnosability
# ---------------------------------------------------------------------------
#
# A reasoning model given a low `max_tokens` can spend its ENTIRE output
# budget on hidden reasoning and return `text == ""` with `output_tokens`
# nonzero. `evaluate_submission` / `submit_validated` correctly reject that
# (D1: submit-time adjudication is authoritative, fail-closed is right) --
# this section does not change that. It only makes the empty-but-spent shape
# distinguishable from a genuinely empty response (`output_tokens == 0`), so
# a caller does not mistake a `max_tokens` tuning problem for a parser bug.


def is_likely_reasoning_exhaustion(text: str, output_tokens: int) -> bool:
    """True when ``text`` is empty/whitespace-only but ``output_tokens`` is nonzero.

    That shape is the signature of a reasoning model that spent its entire
    ``max_tokens`` budget on hidden reasoning tokens before emitting any
    visible content. It is a STRONG INFERENCE from the token/text shape, not
    a certainty -- other causes could in principle produce the same shape.

    Distinguishing this from a genuinely empty response
    (``output_tokens == 0``, e.g. the provider returned nothing at all) is
    the whole point: only THIS case is plausibly fixed by raising
    ``max_tokens``; a zero-token empty response needs a different remedy.
    """
    return output_tokens > 0 and not (text or "").strip()


def describe_likely_reasoning_exhaustion(response: "LLMResponse") -> Optional[str]:
    """Human-readable diagnostic for ``response.likely_reasoning_exhausted``.

    Returns ``None`` when the flag is not set (including the
    ``output_tokens == 0`` case, which is NOT this failure mode). Names the
    likely cause and the remedy in plain terms without asserting either as
    certain -- pair with the raw ``output_tokens`` value when logging, since
    this function does not repeat it.
    """
    if not response.likely_reasoning_exhausted:
        return None
    return (
        f"response text is empty but output_tokens={response.output_tokens} > 0 "
        "-- the output budget appears to have been consumed before any "
        "visible text was emitted (likely a reasoning model spending "
        "max_tokens on hidden reasoning); consider raising max_tokens. This "
        "is a strong inference, not a certainty."
    )


def _reasoning_tail(reasoning: Any, *, limit: int = 200) -> str:
    """Flatten and cap reasoning for a single-line operator diagnostic."""
    flattened = str(reasoning or "").replace("\r", " ").replace("\n", " ")
    return flattened[-limit:].replace("'", "\\'")


def _empty_completion_line(
    response: LLMResponse, *, attempt: int, max_attempts: int
) -> str:
    """Render the stable one-line diagnostic for an empty attempt."""
    tail = _reasoning_tail(response.reasoning)
    return (
        "empty_completion "
        f"model={response.model} "
        f"finish_reason={response.finish_reason} "
        f"output_tokens={response.output_tokens} "
        f"input_tokens={response.input_tokens} "
        f"attempt={attempt}/{max_attempts} "
        f"likely_reasoning_exhausted={response.likely_reasoning_exhausted} "
        f"reasoning_tail='{tail}'"
    )


# ---------------------------------------------------------------------------
# Cost accounting
# ---------------------------------------------------------------------------


def load_pricing(path: Union[str, Path]) -> dict:
    """Load a pricing table from a YAML file into a plain dict.

    Kept separate from :func:`estimate_cost` so the pricing lookup itself is
    pure over an already-loaded mapping (the byte-for-byte testable core).
    Requires ``pyyaml``.
    """
    import yaml  # noqa: PLC0415

    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hit_tokens: int = 0,
    *,
    pricing: Mapping[str, Any],
) -> float:
    """Return the USD cost of one call against a per-1M-token pricing table.

    Each ``pricing[model]`` entry carries ``input`` and ``output`` rates and
    an optional ``cache_hit`` rate (falling back to ``input`` when absent).
    Formula::

        non_cached_in = max(0, input_tokens - cache_hit_tokens)
        cost = (non_cached_in * input + cache_hit_tokens * cache_hit
                + output_tokens * output) / 1e6

    An unknown model raises :class:`KeyError` BY DESIGN -- a typo must never
    silently return 0.0. Negative token counts clamp to 0; cache tokens are
    clamped to a subset of input tokens so they are never double-counted.
    """
    if model not in pricing:
        raise KeyError(f"Unknown model {model!r} in pricing table")
    entry = pricing[model]
    input_rate = float(entry["input"])
    output_rate = float(entry["output"])
    cache_hit_rate = float(entry["cache_hit"]) if "cache_hit" in entry else input_rate

    safe_input = max(0, int(input_tokens))
    safe_output = max(0, int(output_tokens))
    safe_cache = min(max(0, int(cache_hit_tokens)), safe_input)
    non_cached_in = safe_input - safe_cache
    return (
        non_cached_in * input_rate
        + safe_cache * cache_hit_rate
        + safe_output * output_rate
    ) / 1_000_000.0


def response_cost(
    model: str,
    response: LLMResponse,
    *,
    pricing: Mapping[str, Any],
) -> float:
    """USD charged for one ``response`` on THIS run.

    Cache-served responses (``from_cache``) cost nothing on this run -- the
    spend happened on the original live call. Live responses are priced from
    their token counts via :func:`estimate_cost`.
    """
    if response.from_cache:
        return 0.0
    return estimate_cost(
        model,
        response.input_tokens,
        response.output_tokens,
        response.cache_hit_tokens,
        pricing=pricing,
    )


def _charge_exception(
    exc: BaseException,
    *,
    model: str,
    pricing: Optional[Mapping[str, Any]],
    cost_budget: Optional[CostBudget],
    identifier: str,
) -> None:
    """Charge a transport exception when it reports token usage."""
    output_tokens = getattr(exc, "output_tokens", None)
    if pricing is None or output_tokens is None:
        return
    input_tokens = int(getattr(exc, "input_tokens", 0) or 0)
    exception_model = str(getattr(exc, "model", None) or model)
    cost = estimate_cost(
        exception_model,
        input_tokens,
        int(output_tokens or 0),
        int(getattr(exc, "cache_hit_tokens", 0) or 0),
        pricing=pricing,
    )
    if cost_budget is not None:
        cost_budget.charge(cost, identifier=identifier or model)


def model_alias(model: str, *, pricing: Optional[Mapping[str, Any]] = None) -> str:
    """Short alias for ``model`` from the pricing table, or ``model`` itself.

    Unknown models -- and a ``None`` table -- fall back to ``model``; only
    :func:`estimate_cost` enforces the every-model-priced invariant.
    """
    if not pricing:
        return model
    entry = pricing.get(model)
    if not isinstance(entry, Mapping):
        return model
    alias = entry.get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    return model


# ---------------------------------------------------------------------------
# Budget guards
# ---------------------------------------------------------------------------


class LLMUnavailableError(Exception):
    """The selected backend cannot be used, established BEFORE any work ran.

    Distinct from :class:`PipelineHaltError`, which reports a run that started and then
    hit a wall. This is raised at selection time -- by :func:`route` after a
    reachability probe fails -- so no unit has been attempted and none needs
    resuming. The message names the remedy in terms of the env vars a consumer
    controls.
    """


class BudgetExceededError(Exception):
    """Raised when a request exceeds an input-token or running-cost budget.

    Carries enough metadata for an operator to act on the specific overflow:
    ``identifier`` (a caller-supplied label), ``measured`` (the measured
    size), ``budget`` (the cap), and ``model``.
    """

    def __init__(
        self,
        *,
        identifier: str,
        measured: float,
        budget: float,
        model: str = "",
    ) -> None:
        self.identifier = identifier
        self.measured = measured
        self.budget = budget
        self.model = model
        super().__init__(
            f"budget exceeded for {identifier!r}: measured {measured} > "
            f"budget {budget}" + (f" (model {model!r})" if model else "")
        )


def estimate_request_tokens(system: str, user: str) -> int:
    """Estimate the request token count as ``(len(system)+len(user)+3)//4``.

    The 4-characters-per-token heuristic both source systems use for
    budget-packing decisions -- conservative enough without a real tokenizer.
    """
    return (len(system) + len(user) + 3) // 4


def check_request_fits(
    *,
    system: str,
    user: str,
    model: str,
    budgets: Optional[Mapping[str, int]] = None,
    identifier: str = "",
) -> int:
    """Validate an assembled request against ``model``'s input-token budget.

    Returns the measured token count on success (so callers can log it).
    Raises :class:`BudgetExceededError` on overflow. A model with no entry in
    ``budgets`` (or ``budgets is None``) passes through unconditionally -- the
    caller can add a model without calibrating a budget upfront.
    """
    tokens = estimate_request_tokens(system, user)
    budget = None if budgets is None else budgets.get(model)
    if budget is not None and tokens > budget:
        raise BudgetExceededError(
            identifier=identifier or model,
            measured=tokens,
            budget=budget,
            model=model,
        )
    return tokens


@dataclass
class CostBudget:
    """A running USD-spend guard.

    ``charge`` accumulates and raises :class:`BudgetExceededError` once the
    running total would exceed ``limit``. A ``None`` limit disables the guard
    (spend is still tracked on ``spent``).
    """

    limit: Optional[float] = None
    spent: float = 0.0

    def charge(self, cost: float, *, identifier: str = "") -> float:
        """Add ``cost`` to the running total; raise if it exceeds ``limit``."""
        prospective = self.spent + cost
        if self.limit is not None and prospective > self.limit:
            raise BudgetExceededError(
                identifier=identifier or "cost_budget",
                measured=prospective,
                budget=self.limit,
            )
        self.spent = prospective
        return self.spent


# ---------------------------------------------------------------------------
# Content-addressed response cache
# ---------------------------------------------------------------------------


def build_cache_key(
    *,
    backend: str,
    model: str,
    system: str,
    user: str,
    options: Optional[BackendOptions] = None,
) -> str:
    """Content hash over ``(backend, model, system, user, options)``.

    Byte-identical requests collapse to one digest regardless of dict
    ordering at the call site (``freshness.hashing.content_hash`` canonicalizes
    via sorted-key ASCII JSON). ``temperature`` is included even when ``None``
    because inheriting the server/model default is a distinct request from
    explicitly selecting a value. ``cache_salt`` and ``user_cache_prefix``
    participate only when set, so the no-salt / no-prefix key stays stable.
    Whitespace inside the prompts is significant (a trailing-newline
    difference is a real input difference the provider would see).
    """
    opts = options or BackendOptions()
    payload = {
        "backend": backend,
        "model": model,
        "system": system,
        "user": user,
        "temperature": opts.temperature,
        "max_tokens": opts.max_tokens,
    }
    if opts.cache_salt:
        payload["cache_salt"] = opts.cache_salt
    if opts.user_cache_prefix:
        payload["user_cache_prefix"] = opts.user_cache_prefix
    return hashing.content_hash(payload, length=hashing.FULL_DIGEST_LENGTH)


def _serialize_response_error(error: Any) -> Any:
    """Convert a seam error to JSON data without importing the optional seam."""
    if error is None:
        return None
    to_json = getattr(error, "to_json", None)
    return to_json() if callable(to_json) else error


class ResponseCache:
    """File-per-key content-addressed cache for :class:`LLMResponse`.

    One JSON file per cache key under a pluggable directory -- no database,
    no project path baked in. Empty / whitespace-only responses are NEVER
    stored: the caller's retry layer must stay free to try again, and caching
    an empty body would freeze the flake forever.
    """

    def __init__(self, cache_dir: Union[str, Path]) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def lookup(self, key: str) -> Optional[LLMResponse]:
        """Return the cached response for ``key``, or ``None``."""
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError):
            return None
        return LLMResponse(
            text=data["text"],
            model=data["model"],
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
            cache_hit_tokens=int(data.get("cache_hit_tokens", 0)),
            wall_ms=int(data.get("wall_ms", 0)),
            attempts=1,
            from_cache=True,
            status=data.get("status", "completed"),
            error=data.get("error"),
            dropped_params=tuple(data.get("dropped_params", ())),
            forwarded_params=tuple(data.get("forwarded_params", ())),
            execution_controls_applied=tuple(
                data.get("execution_controls_applied", ())
            ),
            structured=data.get("structured"),
            # These timestamps bracket a live call, not a cache lookup.
            started_at=None,
            ended_at=None,
            reasoning=data.get("reasoning", ""),
            finish_reason=data.get("finish_reason"),
        )

    def store(self, key: str, response: LLMResponse) -> bool:
        """Persist ``response`` under ``key`` atomically; skip empty bodies.

        Returns True when a row was written, False when the response was
        empty (and therefore skipped).

        Writes go through a same-directory temp file plus ``os.replace``
        rather than a direct ``write_text``, so a reader (a concurrent
        ``lookup``, or a second writer) never observes a partially written
        file -- it sees either the prior complete entry or the new complete
        one, never a truncated or interleaved one. Two details are
        load-bearing, not stylistic:

        - The temp file is created in ``self.cache_dir`` (not a system temp
          directory) because ``os.replace`` is only atomic within one
          filesystem/device; a cross-device replace can fail or, worse,
          silently fall back to a non-atomic copy on some platforms.
        - ``os.replace`` (not ``os.rename``) is required for Windows, where
          ``rename`` raises ``FileExistsError`` on an existing target instead
          of replacing it; ``os.replace`` overwrites unconditionally on every
          platform this runs on.

        A write failure (temp-file write or the replace itself) removes the
        temp file and re-raises, so a crash never leaves a stray ``.tmp``
        file next to the cache, and the caller sees the original failure
        rather than a swallowed one.

        Does not change the cache KEY -- :func:`build_cache_key` is untouched
        and cache-key stability across this change is a settled decision
        (plan D3). This is a write-path hardening only.
        """
        if not response.text or not response.text.strip():
            return False
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "text": response.text,
            "model": response.model,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "cache_hit_tokens": response.cache_hit_tokens,
            "wall_ms": response.wall_ms,
            "status": response.status,
            "error": _serialize_response_error(response.error),
            "dropped_params": response.dropped_params,
            "forwarded_params": response.forwarded_params,
            "execution_controls_applied": response.execution_controls_applied,
            "structured": response.structured,
            "started_at": response.started_at,
            "ended_at": response.ended_at,
            "reasoning": response.reasoning,
            "finish_reason": response.finish_reason,
        }
        target = self._path(key)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(self.cache_dir)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
            self._replace_with_retry(tmp_path, target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return True

    @staticmethod
    def _replace_with_retry(tmp_path: Path, target: Path) -> None:
        """``os.replace(tmp_path, target)`` with a short retry on Windows.

        Windows briefly denies ``ReplaceFile`` when another thread/process is
        mid-replace on the same target -- an ``os.replace`` under genuine
        concurrent writers to one cache key can raise a transient
        ``PermissionError`` even though each individual replace is atomic.
        POSIX ``rename`` has no such window (it never fails on a
        same-directory target already open elsewhere), so this only matters
        on Windows, but the retry is harmless everywhere. A handful of short
        sleeps is enough for the other writer's replace to finish; a
        persistent failure (e.g. a genuinely locked file) still raises after
        the budget is spent.
        """
        last_exc: Optional[PermissionError] = None
        for attempt in range(10):
            try:
                os.replace(tmp_path, target)
                return
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.01 * (attempt + 1))
        assert last_exc is not None
        raise last_exc


# ---------------------------------------------------------------------------
# call_llm -- the single entry point
# ---------------------------------------------------------------------------


def call_llm(
    backend: LLMBackend,
    system: str,
    user: str,
    *,
    model: str,
    options: Optional[BackendOptions] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    pricing: Optional[Mapping[str, Any]] = None,
    input_budgets: Optional[Mapping[str, int]] = None,
    cost_budget: Optional[CostBudget] = None,
    retries: int = 0,
    retry_sleep: float = 0.0,
    identifier: str = "",
) -> LLMResponse:
    """Run one completion through the shared pipeline concerns.

    Order of operations:

    1. **Budget guard** -- ``check_request_fits`` against ``input_budgets``
       (raises :class:`BudgetExceededError` before any provider call).
    2. **Cache lookup** -- when ``cache_dir`` is bound, a hit returns
       immediately with ``from_cache=True``.
    3. **Backend call + retry** -- ``backend.complete`` is attempted up to
       ``retries + 1`` times. A returned empty/whitespace response is a
       failed, priced attempt: it is logged, retried within this budget, and
       raises :class:`EmptyCompletionError` when exhausted. A failure the
       backend classifies as a halt is re-raised as
       :class:`PipelineHaltError` and NOT retried; any other failure is
       retried (sleeping ``retry_sleep`` between attempts) and, once the
       budget is exhausted, propagates as its original type.
    4. **Cost accounting** -- when ``pricing`` is bound each live response,
       including an empty response, is priced and charged to ``cost_budget``
       (which may raise :class:`BudgetExceededError`). Transport exceptions
       carrying token counts are charged before retry/classification too.
    5. **Cache write** -- a non-empty live response is stored under its key.

    Before step 4, a live response's ``likely_reasoning_exhausted`` is
    (re)computed via :func:`is_likely_reasoning_exhaustion` -- see that
    function and :func:`describe_likely_reasoning_exhaustion`. This does NOT
    change what counts as a valid response or any rejection/ordering
    behavior; it only makes an empty-but-token-spending completion
    programmatically distinguishable from a genuinely empty one.

    ``sleep`` is invoked through the module-level ``time`` object so tests can
    monkeypatch ``platform.time.sleep``.
    """
    opts = options or BackendOptions()

    if input_budgets is not None:
        check_request_fits(
            system=system,
            user=user,
            model=model,
            budgets=input_budgets,
            identifier=identifier,
        )

    cache: Optional[ResponseCache] = None
    cache_key: Optional[str] = None
    if cache_dir is not None:
        cache = ResponseCache(cache_dir)
        cache_key = build_cache_key(
            backend=backend.name,
            model=model,
            system=system,
            user=user,
            options=opts,
        )
        hit = cache.lookup(cache_key)
        if hit is not None:
            return hit

    response: Optional[LLMResponse] = None
    last_exc: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            candidate = backend.complete(system, user, model=model, options=opts)
        except PipelineHaltError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- classify then re-raise
            _charge_exception(
                exc,
                model=model,
                pricing=pricing,
                cost_budget=cost_budget,
                identifier=identifier,
            )
            halt = backend.classify_halt(exc)
            if halt is not None:
                raise PipelineHaltError(halt, str(exc)) from exc
            last_exc = exc
            if attempt < retries:
                if retry_sleep:
                    time.sleep(retry_sleep)
                continue
            raise
        candidate = replace(
            candidate,
            likely_reasoning_exhausted=is_likely_reasoning_exhaustion(
                candidate.text, candidate.output_tokens
            ),
        )
        if not candidate.text.strip():
            try:
                if pricing is not None:
                    cost = response_cost(candidate.model, candidate, pricing=pricing)
                    if cost_budget is not None:
                        cost_budget.charge(cost, identifier=identifier or model)
            finally:
                print(
                    _empty_completion_line(
                        candidate, attempt=attempt + 1, max_attempts=retries + 1
                    ),
                    file=sys.stderr,
                    flush=True,
                )
            if attempt < retries:
                if retry_sleep:
                    time.sleep(retry_sleep)
                continue
            raise EmptyCompletionError(
                "empty completion exhausted retry budget",
                model=candidate.model,
                finish_reason=candidate.finish_reason,
                input_tokens=candidate.input_tokens,
                output_tokens=candidate.output_tokens,
                attempt=attempt + 1,
                max_attempts=retries + 1,
                likely_reasoning_exhausted=candidate.likely_reasoning_exhausted,
                reasoning=candidate.reasoning,
                reasoning_tail=_reasoning_tail(candidate.reasoning),
            )
        response = candidate
        break
    assert response is not None, last_exc  # loop either breaks or raises

    if pricing is not None:
        cost = response_cost(response.model, response, pricing=pricing)
        if cost_budget is not None:
            cost_budget.charge(cost, identifier=identifier or model)

    if cache is not None and cache_key is not None and not response.from_cache:
        cache.store(cache_key, response)

    return response


# ---------------------------------------------------------------------------
# submit_validated -- the validate-until-valid loop
# ---------------------------------------------------------------------------


@dataclass
class SubmitResult:
    """Outcome of a validate-until-valid submission loop.

    - ``payload`` -- the last successfully PARSED payload (``None`` when no
      attempt ever parsed). A trailing parse failure never erases an earlier
      good parse.
    - ``rejections`` -- outstanding
      :class:`~content_pipeline.validate.contract.Rejection` after the final
      attempt; empty means accepted. A parse failure is surfaced as one
      ``parse_error`` rejection so the caller's exhaustion policy sees a
      uniform shape.
    - ``attempts`` -- number of completion calls made.
    - ``responses`` -- per-attempt :class:`LLMResponse` list (the audit
      trail).
    """

    payload: Any
    rejections: List[contract.Rejection]
    attempts: int
    responses: List[LLMResponse] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """True when the final attempt parsed and no rejection blocks."""
        return self.payload is not None and not contract.is_rejecting(self.rejections)


@dataclass(frozen=True)
class ValidationSpec:
    """The parse/validate contract one submission is judged against.

    Groups the consumer-supplied ``parse_fn``, ``validators``, ``context``,
    and ``block_soft`` policy so :func:`evaluate_submission` takes a single
    argument instead of four independently-threaded pieces of a caller's
    contract. This is the SAME contract :func:`submit_validated` drives its
    loop with (plan D1) -- it builds one ``ValidationSpec`` from its own
    parameters and calls :func:`evaluate_submission` with it, rather than
    reimplementing the parse/validate step inline. A ``RunAdapter``'s
    eventual typed ``ValidationSpec`` field (plan A-min.3,
    ``execution/adapter.py``, not built here) widens this shape for the
    out-of-process worker protocol; it does not replace it.
    """

    parse_fn: Callable[[str], Any]
    validators: Sequence[contract.Validator]
    context: Any = None
    block_soft: bool = True


@dataclass
class EvaluationResult:
    """Outcome of one pure :func:`evaluate_submission` call.

    - ``parsed`` -- True when ``parse_fn`` succeeded (even if the validators
      then rejected the parsed payload); False when ``parse_fn`` raised.
    - ``payload`` -- the parsed value when ``parsed`` is True, ``None``
      otherwise. Distinguishing ``parsed`` from "payload is falsy" matters
      because a legitimate parse result can itself be ``None`` or empty.
    - ``rejections`` -- validator rejections when ``parsed`` is True (empty
      means accepted); a single ``parse_error`` rejection when ``parsed`` is
      False.
    """

    parsed: bool
    payload: Any
    rejections: List[contract.Rejection]


def evaluate_submission(text: str, spec: ValidationSpec) -> EvaluationResult:
    """Judge one candidate response against ``spec`` -- parse, then validate.

    Extracted from :func:`submit_validated`'s per-attempt body (plan D1) so a
    worker in another process can evaluate a submission WITHOUT the
    surrounding retry/backend/cache machinery: this function is PURE. It
    makes no backend calls, no cache reads or writes, reads no clock, touches
    no filesystem, and makes no network call -- it only calls
    ``spec.parse_fn`` and runs ``spec.validators`` through
    :func:`~content_pipeline.validate.contract.run_rules`, exactly as
    ``submit_validated`` does today. Because it is the SAME call sequence
    (same ``parse_fn``, same ``run_rules`` -- which already sorts
    deterministically by ``(kind, detail)``), feedback strings and rejection
    ORDERING are byte-identical to ``submit_validated``'s prior inline logic;
    that equivalence is what the existing ``submit_validated`` tests pin.

    A raise from ``parse_fn`` is recorded as one ``parse_error`` HARD
    rejection -- a malformed response is a model defect exactly like a
    validation rejection, matching ``submit_validated``'s documented parse
    handling.
    """
    try:
        payload = spec.parse_fn(text)
    except Exception as exc:  # noqa: BLE001 -- parse_fn is caller code
        return EvaluationResult(
            parsed=False,
            payload=None,
            rejections=[
                contract.Rejection(
                    kind="parse_error",
                    severity=contract.Severity.HARD,
                    detail=str(exc),
                )
            ],
        )
    rejections = contract.run_rules(payload, spec.context, spec.validators)
    return EvaluationResult(parsed=True, payload=payload, rejections=rejections)


def _default_feedback(original_user: str, response_text: str, feedback: str) -> str:
    """Append the rejection feedback to the original prompt (cache-busting).

    The rebuilt prompt MUST differ in bytes from the previous attempt's --
    a content-addressed cache would otherwise replay the rejected response
    forever. Appending the rejection text satisfies that and is also what the
    model needs to see.
    """
    return f"{original_user}\n\n{feedback}"


def submit_validated(
    *,
    backend: LLMBackend,
    system: str,
    user: str,
    model: str,
    parse_fn: Callable[[str], Any],
    validators: Sequence[contract.Validator],
    context: Any = None,
    build_feedback: Optional[Callable[[str, str, str], str]] = None,
    max_attempts: int = 3,
    options: Optional[BackendOptions] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    block_soft: bool = True,
    **call_kwargs: Any,
) -> SubmitResult:
    """Run the call -> parse -> validate -> feed-back loop.

    Both the in-loop generation site and the post-hoc audit site validate
    through the SAME ``validate.contract`` validators, so the rule set cannot
    drift between them (the one-rule-set-many-call-sites boundary).

    - ``parse_fn`` -- ``text -> payload``. A raise is recorded as a
      ``parse_error`` rejection and the loop retries (a malformed response is
      a model defect exactly like a validation rejection).
    - ``validators`` -- run via ``contract.run_rules(payload, context,
      validators)``; a non-blocking result (per ``block_soft``) accepts.
    - ``build_feedback`` -- ``(original_user, response_text, feedback_text) ->
      str`` for the next attempt's prompt. Defaults to appending the rendered
      rejections. Receives the ORIGINAL user prompt so feedback does not stack
      across attempts, and MUST return different bytes than the prior attempt.

    Extra keyword arguments are forwarded to :func:`call_llm` (e.g. ``pricing``,
    ``input_budgets``, ``retries``). Per-attempt cache-busting is automatic:
    each retry carries a ``cache_salt`` equal to the attempt index.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    feedback_fn = build_feedback or _default_feedback
    base_options = options or BackendOptions()
    spec = ValidationSpec(
        parse_fn=parse_fn,
        validators=validators,
        context=context,
        block_soft=block_soft,
    )

    responses: List[LLMResponse] = []
    payload: Any = None  # last SUCCESSFULLY parsed payload; never clobbered
    rejections: List[contract.Rejection] = []
    current_user = user

    for attempt in range(max_attempts):
        attempt_options = (
            base_options
            if attempt == 0
            else replace(base_options, cache_salt=attempt)
        )
        try:
            resp = call_llm(
                backend,
                system,
                current_user,
                model=model,
                options=attempt_options,
                cache_dir=cache_dir,
                **call_kwargs,
            )
        except EmptyCompletionError as exc:
            # call_llm owns per-call retries; this loop owns validation
            # retries. Treat its exhausted empty attempt as the same parse
            # input the old successful-empty path supplied, without adding a
            # second retry inside one validation attempt.
            resp = exc.response()
        responses.append(resp)

        evaluation = evaluate_submission(resp.text, spec)
        rejections = evaluation.rejections
        if evaluation.parsed:
            payload = evaluation.payload
            if not contract.is_rejecting(rejections, block_soft=block_soft):
                break

        if attempt < max_attempts - 1:
            feedback_text = contract.format_rejections(
                rejections, block_soft=block_soft
            )
            current_user = feedback_fn(user, resp.text, feedback_text)

    return SubmitResult(
        payload=payload,
        rejections=list(rejections),
        attempts=len(responses),
        responses=responses,
    )


__all__ = [
    "LLMResponse",
    "EmptyCompletionError",
    "BackendOptions",
    "LLMBackend",
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "PipelineHaltError",
    "HaltError",
    "HALT_UNREACHABLE",
    "LLMUnavailableError",
    "classify_halt_text",
    "classify_openai_exception",
    "is_likely_reasoning_exhaustion",
    "describe_likely_reasoning_exhaustion",
    "load_pricing",
    "estimate_cost",
    "response_cost",
    "model_alias",
    "BudgetExceededError",
    "estimate_request_tokens",
    "check_request_fits",
    "CostBudget",
    "build_cache_key",
    "ResponseCache",
    "call_llm",
    "SubmitResult",
    "submit_validated",
    "ValidationSpec",
    "EvaluationResult",
    "evaluate_submission",
]
