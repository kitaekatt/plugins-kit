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
- :class:`BackendOptions` -- per-call knobs (some understood by only one
  transport, documented on the field).
- :class:`LLMBackend` -- the completion protocol.
- :class:`HaltError` and :func:`classify_openai_exception` /
  :func:`classify_halt_text` -- the shared halt taxonomy. A *halt* is a
  failure that persists across subsequent calls (rate-limit / auth /
  insufficient-credit), so a bulk runner catching :class:`HaltError` stops
  cleanly instead of burning the rest of the corpus.
- :func:`call_llm` -- the single entry point: budget guard, cache, retry,
  halt-mapping, cost accounting.
- :func:`build_cache_key` / :class:`ResponseCache` -- the content-addressed
  cache (pluggable directory; empty responses never cached).
- :func:`estimate_cost` / :func:`response_cost` / :func:`load_pricing` -- cost
  against a pricing table; an unknown model is a hard :class:`KeyError` by
  design (a typo must never silently price at 0.0).
- :func:`estimate_request_tokens` / :func:`check_request_fits` /
  :class:`CostBudget` / :class:`BudgetExceededError` -- the budget guards.
- :func:`submit_validated` / :class:`SubmitResult` -- the validate-until-valid
  loop, taking validators from ``validate.contract``.
"""

from __future__ import annotations

import json
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
    """

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
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


class HaltError(Exception):
    """A failure that persists across subsequent calls -- stop the bulk run.

    Carries a machine-readable ``kind`` (one of :data:`HALT_AUTH`,
    :data:`HALT_RATE_LIMIT`, :data:`HALT_INSUFFICIENT_CREDIT`) so a bulk
    runner can halt-and-resume without parsing the message text. ``call_llm``
    raises this when a backend classifies the underlying exception as a halt;
    a non-halt failure propagates as its original exception type.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)


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


def classify_openai_exception(exc: BaseException) -> Optional[str]:
    """Map an OpenAI-SDK exception (or one wrapping it) to a halt kind.

    Returns :data:`HALT_AUTH`, :data:`HALT_RATE_LIMIT`, or
    :data:`HALT_INSUFFICIENT_CREDIT` for the known persistent failures;
    ``None`` otherwise. The ``openai`` import is optional -- when absent, the
    text-marker fallback still catches the common shapes. Recurses on
    ``__cause__`` so a wrapped SDK exception is still classified.
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
        return classify_openai_exception(cause)
    return None


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
    via sorted-key ASCII JSON). ``cache_salt`` and ``user_cache_prefix``
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
        )

    def store(self, key: str, response: LLMResponse) -> bool:
        """Persist ``response`` under ``key``; skip empty bodies.

        Returns True when a row was written, False when the response was
        empty (and therefore skipped).
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
        }
        self._path(key).write_text(
            json.dumps(payload, ensure_ascii=True, sort_keys=True),
            encoding="utf-8",
        )
        return True


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
       ``retries + 1`` times. A failure the backend classifies as a halt is
       re-raised as :class:`HaltError` and NOT retried; any other failure is
       retried (sleeping ``retry_sleep`` between attempts) and, once the
       budget is exhausted, propagates as its original type.
    4. **Cost accounting** -- when ``pricing`` is bound the response is
       priced and charged to ``cost_budget`` (which may raise
       :class:`BudgetExceededError`).
    5. **Cache write** -- a non-empty live response is stored under its key.

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
            response = backend.complete(system, user, model=model, options=opts)
            break
        except HaltError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- classify then re-raise
            halt = backend.classify_halt(exc)
            if halt is not None:
                raise HaltError(halt, str(exc)) from exc
            last_exc = exc
            if attempt < retries:
                if retry_sleep:
                    time.sleep(retry_sleep)
                continue
            raise
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
        resp = call_llm(
            backend,
            system,
            current_user,
            model=model,
            options=attempt_options,
            cache_dir=cache_dir,
            **call_kwargs,
        )
        responses.append(resp)

        try:
            attempt_payload = parse_fn(resp.text)
        except Exception as exc:  # noqa: BLE001 -- parse_fn is caller code
            rejections = [
                contract.Rejection(
                    kind="parse_error",
                    severity=contract.Severity.HARD,
                    detail=str(exc),
                )
            ]
        else:
            payload = attempt_payload
            rejections = contract.run_rules(attempt_payload, context, validators)
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
    "BackendOptions",
    "LLMBackend",
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_INSUFFICIENT_CREDIT",
    "HaltError",
    "classify_halt_text",
    "classify_openai_exception",
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
]
