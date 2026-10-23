"""Completion backends behind one protocol, selected by process-level routing.

Six backends implement :class:`~llm_kit.pipeline.runtime.platform.LLMBackend`:

- :class:`OpenRouterBackend` -- an OpenAI-compatible HTTP completion.
- :class:`ModelEndpointBackend` -- an OpenAI-compatible completion against an
  entry in the model-endpoints registry, typically a locally hosted keyless
  server. Unlike the others its availability is NOT assumed: :func:`route`
  pings the selected entry before returning it.
- :class:`ClaudeCliBackend` -- the local ``claude -p`` CLI (billed under the
  operator's subscription rather than per call; the response carries no
  metered charge, so the run's cost comes from ``platform.response_cost``).
- :class:`CodexCliBackend` -- the local ``codex exec`` CLI (billed under the
  operator's subscription rather than per call; cost is derived as for
  ``ClaudeCliBackend``).
- :class:`OpencodeCliBackend` -- the local ``opencode run`` CLI, with stdout as
  the answer and a mandatory wall-clock timeout.
- :class:`MockBackend` -- deterministic, scriptable responses. The always-wins
  test seam: no network, no subprocess, no adapter.

The five live transports are THIN WRAPPERS over a completion backend built
by an injected :class:`~llm_kit.completion.factory_protocols.BackendFactory`
(default :class:`~llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory`):
each wrapper keeps its public constructor options, describes the backend it
needs as an immutable :class:`~llm_kit.completion.factory_protocols.BackendSpec`
and hands that to the factory the first time it is used. The transport itself
-- the ``claude -p``, ``codex exec``, and ``opencode run`` subprocess runners,
retry, timeout, hard-stop detection, and the OpenAI-compatible client and
prompt-cache message shaping -- lives in ``llm_kit.adapters`` and is reached
only through that factory, so this module (and the MockBackend path) loads with
no SDK and no adapter. The per-call options and the normalized response are
adapted across the seam so this module's
:class:`~llm_kit.pipeline.runtime.platform.BackendOptions` /
:class:`~llm_kit.pipeline.runtime.platform.LLMResponse` stay the pipeline-facing
types regardless of provider. A declared output contract crosses the same
seam: it rides the options, its report comes back as JSON data on the
response, and the completion layer's ``OutputContractViolation`` becomes
:class:`~llm_kit.pipeline.runtime.platform.StructuralOutputError`, all through
the one contract probe,
:func:`~llm_kit.pipeline.runtime.platform._contract_seam`.

:func:`route` reads one process-level model declaration
(``CONTENT_PIPELINE_LLM_MODELS``) and returns the backend for its first usable
entry; unset, it returns the default :class:`OpenRouterBackend`. Backend
selection is one process-wide switch rather than a parameter threaded through
every call site. The returned response's ``model`` reflects the model that
ACTUALLY ran, so audit stamping stays truthful.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from llm_kit.completion.factory_protocols import BackendFactory, BackendSpec
from llm_kit.dispatch.pipeline_backends import (
    BACKEND_CLAUDE_CLI,
    BACKEND_CODEX_CLI,
    BACKEND_MODEL_ENDPOINT,
    BACKEND_OPENCODE_CLI,
    BACKEND_OPENROUTER,
    ConfiguredBackendFactory,
)
from llm_kit.pipeline.runtime import platform
from llm_kit.pipeline.runtime.platform import BackendOptions, LLMResponse, valid_reported_cost

_UNSET = object()
"""Distinguishes "not looked up yet" from a looked-up None (see
:meth:`ModelEndpointBackend._entry_reasoning_effort`)."""


def _effort_seam() -> Optional[Any]:
    """The effort translation, as ``(plan_effort, resolve_endpoint_profile)``.

    ``plan_effort`` is :func:`llm_kit.registry.effort.plan_effort` and
    ``resolve_endpoint_profile`` is the dispatch re-export of the adapters'
    profile resolution -- the same functions the OpenAI-compatible adapter uses
    to put an effort on the wire, so :class:`ModelEndpointBackend` keys its
    cache off exactly what the delegate sends. A caller (or a test) may replace
    this seam with one returning ``None``; the caller then keeps the legacy
    top-level injection (:meth:`ModelEndpointBackend._legacy_effective_options`).
    """
    from llm_kit.dispatch.endpoint_profile import resolve_endpoint_profile  # noqa: PLC0415
    from llm_kit.registry.effort import plan_effort  # noqa: PLC0415

    return plan_effort, resolve_endpoint_profile


def _is_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` means the endpoint could not be reached at all.

    Checked by TYPE against ``openai.APIConnectionError`` when the SDK is
    importable, and by the stdlib connection errors otherwise, so a machine
    without the SDK still classifies correctly. Import is lazy and failure is
    non-fatal: an unclassifiable exception is simply not a connection error,
    which falls through to the delegate rather than mislabelling anything.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    try:
        import openai  # noqa: PLC0415
    except Exception:  # noqa: BLE001 -- no SDK: stdlib check above is the answer
        return False
    return isinstance(exc, openai.APIConnectionError)


def _is_harness_refusal(exc: BaseException) -> bool:
    """True when the shared registry refused a harness as an HTTP endpoint.

    ``llm_kit.registry`` owns entry classification and the canonical refusal
    wording. This small check preserves that refusal across this adapter's
    best-effort fallback boundaries; it does not classify or load entries here.
    Other registry failures retain the existing probe-friendly fallback.
    """
    return "is a harness entry" in str(exc)


def _lazy_build_note() -> None:
    """Why live adapters guard their lazy delegate build with a lock.

    Not called -- a documentation anchor the adapters reference, kept in
    one place so the reasoning cannot drift between them.

    ``if self._delegate is None: ... self._delegate = ...`` is an unsynchronized
    check-then-assign: under a concurrent first wave every thread finds the slot
    unset and builds its own delegate. Each surplus delegate is a separate
    completion backend with its OWN client slot, so each one goes on
    to build its own OpenAI client -- an ``ssl.create_default_context
    (cafile=certifi.where())`` and a file descriptor apiece. Measured
    downstream: 900 threads through a SINGLE shared transport still
    materialized 509 distinct clients and raised 391 ``[Errno 24] Too many open
    files`` errors against Windows' 512-entry table. Synchronizing only the
    adapter's ``_ensure_client`` does NOT fix this -- surplus delegates each
    hold a distinct, individually-synchronized client.

    Four properties the fix preserves, each one learned the hard way:

    1. **Nothing builds at routing time.** :func:`route` returns adapter
       instances and never touches ``_backend()``. Routing precedes
       ``call_llm``'s response-cache lookup, so building there would pay the
       cost -- and take the descriptor -- on every call, including the ones the
       cache is about to serve.
    2. **``classify_halt`` builds nothing additional.** The platform calls it unguarded
       from inside an ``except``; the lock-free fast path means a warm backend
       never contends there.
    3. **The gate opens on DELEGATE EXISTS, not REQUEST SUCCEEDED.** The slot is
       assigned the moment the object is constructed. Gating on a successful
       request would leave it empty through the whole first round-trip and let
       the next wave build again.
    4. **Only the BUILD is serialized, not the queue behind it.** The lock is
       held for construction alone; ``complete`` issues its request outside it,
       so concurrency is not collapsed to one in-flight call.
    """


def _to_completion_options(opts: BackendOptions) -> Any:
    """Build a ``llm_kit.completion.types.BackendOptions`` from ours.

    Field-for-field: the two option bundles are intentionally identical, but the
    conversion is explicit so a future field drift surfaces here rather than
    silently mis-binding across the seam. Lazy import -- only reached once a live
    backend is actually driven.

    A declared ``output_contract`` is passed through, and it goes through
    :func:`~llm_kit.pipeline.runtime.platform._contract_seam` (the one contract
    probe), never a second import. Without a contract the options are built
    exactly as before and nothing new is imported.
    """
    fields = dict(
        max_tokens=opts.max_tokens,
        temperature=opts.temperature,
        timeout_s=opts.timeout_s,
        cache_salt=opts.cache_salt,
        user_cache_prefix=opts.user_cache_prefix,
        effort=opts.effort,
        allowed_tools=opts.allowed_tools,
        cwd=opts.cwd,
        client_id=opts.client_id,
        log_prefix=opts.log_prefix,
        extras=opts.extras,
    )
    if opts.output_contract is None:
        from llm_kit.completion.types import BackendOptions as _CompletionOptions  # noqa: PLC0415

        return _CompletionOptions(**fields)
    seam = platform._contract_seam()
    platform._require_contract(opts.output_contract, seam)
    return seam.BackendOptions(**fields, output_contract=opts.output_contract)


def _error_to_data(error: Any) -> Any:
    """Reduce a seam ``ResponseError`` to plain data, duck-typed.

    ``getattr``-based rather than an isinstance check for the same reason
    ``total_tokens`` is read with ``getattr`` below: a delegate may predate the
    type, and this package must not import the seam to do it.
    """
    if error is None:
        return None
    to_json = getattr(error, "to_json", None)
    return to_json() if callable(to_json) else error


def _from_completion_response(resp: Any) -> LLMResponse:
    """Adapt a ``llm_kit.completion.types.LLMResponse`` into ours.

    Field-for-field, and deliberately explicit in BOTH directions: the option
    conversion above exists so a field drift surfaces rather than mis-binding,
    and this one earns its keep the same way. ``total_tokens`` is read with
    ``getattr`` because an injected delegate may predate the field. The same
    compatibility rule applies to every truthfulness field below.
    """
    reported_cost, reported_source = valid_reported_cost(
        getattr(resp, "reported_cost_usd", None),
        getattr(resp, "reported_cost_source", None),
    )
    return LLMResponse(
        text=resp.text,
        model=resp.model,
        input_tokens=resp.input_tokens,
        output_tokens=resp.output_tokens,
        cache_hit_tokens=resp.cache_hit_tokens,
        wall_ms=resp.wall_ms,
        attempts=resp.attempts,
        from_cache=resp.from_cache,
        total_tokens=getattr(resp, "total_tokens", 0),
        reasoning=getattr(resp, "reasoning", ""),
        finish_reason=getattr(resp, "finish_reason", None),
        status=getattr(resp, "status", "completed"),
        # normalized to its JSON form AT THE BOUNDARY, so this field is one type
        # everywhere. The seam hands back a ResponseError object; the response
        # cache can only store JSON. Converting on the way out means a cache hit
        # and a live call yield the same shape, instead of a consumer's
        # `error.code` working live and breaking on a cached response.
        error=_error_to_data(getattr(resp, "error", None)),
        dropped_params=getattr(resp, "dropped_params", ()),
        forwarded_params=getattr(resp, "forwarded_params", ()),
        execution_controls_applied=getattr(resp, "execution_controls_applied", ()),
        structured=getattr(resp, "structured", None),
        started_at=getattr(resp, "started_at", None),
        ended_at=getattr(resp, "ended_at", None),
        # A delegate without either field: unknown, never zero. The pair
        # is re-validated here so a malformed or one-sided value cannot reach
        # the budget as a coerced zero.
        reported_cost_usd=reported_cost,
        reported_cost_source=reported_source,
        # The seam's ContractReport, normalized to JSON data at the boundary
        # for the same reason `error` is: the response cache stores JSON, and a
        # live call and a cache hit must yield one shape. A delegate without a
        # report: None.
        output_contract=platform._report_mapping(getattr(resp, "output_contract", None)),
    )


def _complete_through(
    delegate_fn: Callable[[], Any],
    system: str,
    user: str,
    *,
    model: str,
    opts: BackendOptions,
) -> LLMResponse:
    """Drive a completion delegate and adapt its answer, contract included.

    ``delegate_fn`` is the adapter's ``_backend`` (called here, so building
    the delegate stays lazy). Under a declared ``opts.output_contract`` the
    contract probe runs FIRST, before any delegate is built, and an
    completion layer's ``OutputContractViolation`` -- recognized by the
    probe-imported type, never a second import -- becomes the pipeline's
    :class:`~llm_kit.pipeline.runtime.platform.StructuralOutputError` carrying the
    adapted failed response (raw text, usage, report), so ``call_llm`` charges
    it and ``submit_validated`` turns it into a structural rejection. Without
    a contract nothing new is imported and nothing is mapped.
    """
    violation: Any = ()  # an empty tuple of exception types catches nothing
    if opts.output_contract is not None:
        violation = platform._contract_seam().OutputContractViolation
    delegate = delegate_fn()
    options = _to_completion_options(opts)
    try:
        resp = delegate.complete(system, user, model=model, options=options)
    except violation as exc:
        raise platform.StructuralOutputError(
            _from_completion_response(exc.response)
        ) from exc
    return _from_completion_response(resp)


@dataclass
class _LazyDelegate:
    """Shared double-checked-locked lazy delegate build for the thin wrappers.

    Every live transport (:class:`OpenRouterBackend`, :class:`ClaudeCliBackend`,
    :class:`CodexCliBackend`, :class:`OpencodeCliBackend`,
    :class:`ModelEndpointBackend`) wraps exactly one completion backend, built
    lazily and cached on ``self._delegate`` (see :func:`_lazy_build_note` for
    why the build itself must be lock-guarded, and why nothing else in the
    request path needs to be). This base class owns that mechanism --
    :meth:`_build` -- plus the ``complete`` / ``classify_halt`` bodies that are
    byte-identical across the four wrappers that need no per-call adjustment of
    their own (``ModelEndpointBackend`` overrides both, since it also defaults a
    registry-declared reasoning effort and treats a connection failure as a
    halt).

    The wrapper does not construct the delegate itself: it describes it as an
    immutable :class:`~llm_kit.completion.factory_protocols.BackendSpec` and
    asks a :class:`~llm_kit.completion.factory_protocols.BackendFactory` to
    create it. Each subclass carries an optional ``factory`` field; ``None``
    means the default
    :class:`~llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory`.

    A subclass declares only its OWN fields plus a ``_backend()`` method that
    calls :meth:`_build` with this wrapper's spec and factory -- everything else
    is inherited.
    """

    _delegate: Any = field(default=None, init=False, repr=False, compare=False)
    _build_lock: "threading.Lock" = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def _build(self, spec: BackendSpec, factory: Optional[BackendFactory]) -> Any:
        """Return the cached delegate, building it from ``spec`` at most once.

        ``factory`` is the injected :class:`BackendFactory`; ``None`` selects
        the default configured factory. The factory is called under the build
        lock, never while holding it for a request.
        """
        if self._delegate is not None:
            return self._delegate
        with self._build_lock:
            if self._delegate is None:
                chosen = ConfiguredBackendFactory() if factory is None else factory
                self._delegate = chosen.create(spec)
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
        return _complete_through(self._backend, system, user, model=model, opts=opts)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self._backend().classify_halt(exc)


# ---------------------------------------------------------------------------
# OpenRouter (OpenAI-compatible HTTP) backend -- built through the factory
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterBackend(_LazyDelegate):
    """OpenAI-compatible HTTP completion, built through the backend factory.

    ``endpoint`` / ``project_root`` / ``client`` are forwarded in the
    construction spec to the ``openrouter`` backend (a caller may inject a
    pre-built ``client`` as the test seam). ``factory`` injects the
    :class:`~llm_kit.completion.factory_protocols.BackendFactory`; ``None`` is
    the default configured factory. The delegate is built lazily on first use,
    so constructing this wrapper builds nothing.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    factory: Optional[BackendFactory] = field(default=None, repr=False, compare=False)
    name: str = field(default="openrouter", init=False)

    def _backend(self) -> Any:
        return self._build(
            BackendSpec(
                backend=BACKEND_OPENROUTER,
                endpoint=self.endpoint,
                project_root=self.project_root,
                client=self.client,
            ),
            self.factory,
        )


# ---------------------------------------------------------------------------
# Claude CLI backend -- built through the factory
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCliBackend(_LazyDelegate):
    """Local ``claude -p`` completion, built through the backend factory.

    Spawns the CLI in pure-completion mode (JSON output, no tools by default),
    enforces a per-call timeout, retries transient 5xx envelopes, and maps
    rate-limit / auth markers to a hard stop -- all inside the ``claude-cli``
    adapter. The CLI is billed under the operator's subscription rather than
    per call (the billing model, not a measured cost);
    ``platform.response_cost`` returns the response's reported cost when it
    carries a valid one, otherwise an estimate from the pricing table,
    otherwise ``None`` (unknown). It never returns a synthetic zero for a live
    response.

    Config fields forward in the construction spec; ``runner`` is the
    subprocess seam (an omitted ``runner`` stays omitted, so the adapter keeps
    its own default runner). A ``None`` timeout or retry option means the
    adapter's default. The delegate is built lazily so constructing this
    wrapper builds nothing. ``retry_max_attempts`` defaults to 1 (run-once: one
    request, at most one CLI invocation). Retry is this wrapper's explicit
    policy; the higher-level pipeline retry policy should leave it at 1 to
    avoid a second retry loop.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    default_timeout_s: float = 900.0
    retry_max_attempts: int = 1
    retry_cooldown_s: float = 60.0
    diagnostics_dir: Optional[Path] = None
    executable: Optional[str] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None
    factory: Optional[BackendFactory] = field(default=None, repr=False, compare=False)
    name: str = field(default="claude-cli", init=False)

    def _backend(self) -> Any:
        return self._build(
            BackendSpec(
                backend=BACKEND_CLAUDE_CLI,
                default_timeout_s=self.default_timeout_s,
                retry_max_attempts=self.retry_max_attempts,
                retry_cooldown_s=self.retry_cooldown_s,
                diagnostics_dir=self.diagnostics_dir,
                executable=self.executable,
                runner=self.runner,
            ),
            self.factory,
        )


# ---------------------------------------------------------------------------
# Codex CLI backend -- built through the factory
# ---------------------------------------------------------------------------


@dataclass
class CodexCliBackend(_LazyDelegate):
    """Local ``codex exec`` completion, built through the backend factory.

    ``default_timeout_s`` / ``argv_prefix`` / ``runner`` are forwarded in the
    construction spec to the ``codex-cli`` backend. The delegate is built
    lazily so constructing this wrapper builds nothing.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    default_timeout_s: float = 900.0
    argv_prefix: Optional[tuple] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None
    factory: Optional[BackendFactory] = field(default=None, repr=False, compare=False)
    name: str = field(default="codex-cli", init=False)

    def _backend(self) -> Any:
        return self._build(
            BackendSpec(
                backend=BACKEND_CODEX_CLI,
                default_timeout_s=self.default_timeout_s,
                argv_prefix=self.argv_prefix,
                runner=self.runner,
            ),
            self.factory,
        )


# ---------------------------------------------------------------------------
# OpenCode CLI backend -- built through the factory
# ---------------------------------------------------------------------------

OPENCODE_FILESYSTEM_POSTURE = "unconfined"
"""The upstream OpenCode adapter's actual filesystem posture.

OpenCode's required ``--auto`` flag bypasses permissions, and ``--dir`` does
not confine absolute writes. Selecting ``opencode-cli`` is therefore an
explicit process-wide run decision; this adapter exposes the posture instead
of presenting the working directory as a sandbox.
"""


@dataclass
class OpencodeCliBackend(_LazyDelegate):
    """Local ``opencode run`` completion, built through the backend factory.

    ``default_timeout_s`` / ``argv_prefix`` / ``runner`` are forwarded in the
    construction spec to the ``opencode-cli`` backend. The delegate is built
    lazily so constructing this wrapper builds nothing and needs no
    ``opencode`` executable.

    OpenCode's non-interactive command requires ``--auto``, which bypasses
    permissions; its ``--dir`` option is not a write boundary. The adapter
    emits that warning at call time and returns the answer on stdout.
    ``filesystem_posture`` records the run-level decision made here: this
    wrapper accepts the explicitly selected backend's unconfined posture and
    does not claim to sandbox a pipeline run.

    ``name`` is intentionally constant across OpenCode configurations. The
    cache key also includes the exact provider/model string (for example,
    ``openai/gpt-5``), so distinct model ids remain distinct without embedding
    a user-specific config or entry id in the backend identity.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    default_timeout_s: float = 120.0
    argv_prefix: Optional[tuple] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None
    factory: Optional[BackendFactory] = field(default=None, repr=False, compare=False)
    name: str = field(default="opencode-cli", init=False)
    filesystem_posture: str = field(
        default=OPENCODE_FILESYSTEM_POSTURE, init=False
    )

    def _backend(self) -> Any:
        return self._build(
            BackendSpec(
                backend=BACKEND_OPENCODE_CLI,
                default_timeout_s=self.default_timeout_s,
                argv_prefix=self.argv_prefix,
                runner=self.runner,
            ),
            self.factory,
        )


# ---------------------------------------------------------------------------
# Mock backend (test seam)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Model-endpoint backend -- built through the factory
# ---------------------------------------------------------------------------

@dataclass
class ModelEndpointBackend(_LazyDelegate):
    """Completion against a registered model endpoint.

    The entry comes from the ``llm_kit.registry`` model-endpoints registry -- a
    private, fleet-propagating list of OpenAI-compatible endpoints, typically
    locally hosted and keyless. "Local" is a property of an ENTRY, not of this
    backend, so nothing here assumes localhost.

    AVAILABILITY IS NOT ASSUMED, which is what separates this adapter from its
    four siblings. A cloud provider is up unless it is having an incident; a
    server on the registry is up only if somebody started it. :func:`route`
    therefore pings the selected entry at selection time and refuses with
    :class:`~llm_kit.pipeline.runtime.platform.LLMUnavailableError` rather than
    letting a bulk run discover the same dead host once per unit.

    ``endpoint`` empty means the registry's default entry, resolved on first
    use so that ``.endpoint`` is a concrete entry id by the time it reaches a
    probe or a cache key.

    THREAD SAFETY: as :class:`OpenRouterBackend` -- one instance may be shared
    across worker threads; the lazy delegate build is double-checked-locked.
    """

    endpoint: str = ""
    project_root: Optional[Path] = None
    client: Any = None
    factory: Optional[BackendFactory] = field(default=None, repr=False, compare=False)
    name: str = field(default="model-endpoint", init=False)
    _effort: Any = field(default=_UNSET, init=False, repr=False, compare=False)

    # `name` is CONSTANT across entries, deliberately. The on-disk cache key is
    # (backend name, model id, ...) and two entries serve different model ids,
    # so their caches stay distinct without the entry id in the key. The one
    # edge -- two entries serving the SAME model id on different servers --
    # shares cache identity, which is acceptable: same weights, and the cache
    # was always approximate across server restarts.

    def _entry_id(self) -> Optional[str]:
        """The concrete entry id for the selected entry, resolving the default.

        An empty ``endpoint`` must be resolved through the model-endpoints
        REGISTRY, not left as None. Passing None onward reaches
        ``resolve_endpoint``, whose default is the registry config's
        default endpoint -- ``openrouter`` -- and NOT this registry's own
        ``default:`` key. That mismatch is not cosmetic: it makes an empty
        ``endpoint`` probe OpenRouter and fail with
        "no API key resolved", a nonsense diagnosis for a backend whose entries
        are typically keyless and local.

        Resolution is cached onto ``endpoint`` so the id is concrete by the time
        it reaches a probe or a cache key. A registry that cannot be read leaves
        it unresolved and returns None -- the probe then reports that failure as
        its own ``detail``, which is the honest answer to "is it usable?". The
        registry's harness refusal is intentionally propagated: a harness
        is a valid registry entry, but not a transport endpoint, and must not be
        turned into a misleading default or missing-base-url error here.
        """
        if self.endpoint:
            return self.endpoint
        try:
            from llm_kit.registry.model_endpoints import (  # noqa: PLC0415
                resolve_registry_entry,
            )

            self.endpoint = resolve_registry_entry(None).id
        except Exception as exc:  # noqa: BLE001 -- unresolvable is the probe's to report
            if _is_harness_refusal(exc):
                raise
            return None
        return self.endpoint

    def _backend(self) -> Any:
        # The ``model-endpoint`` spec is built by the factory with the same
        # OpenAI-compatible adapter as ``openrouter`` -- a client works
        # against any base URL, and the model-endpoints registry entry
        # supplies this one's.
        return self._build(
            BackendSpec(
                backend=BACKEND_MODEL_ENDPOINT,
                endpoint=self._entry_id(),
                project_root=self.project_root,
                client=self.client,
            ),
            self.factory,
        )

    def probe(self, *, timeout: float = 2.0) -> Any:
        """Non-raising reachability ping of the selected entry.

        Returns an ``EndpointProbe`` (``ok`` / ``endpoint`` / ``base_url`` /
        ``detail``). Ordinary registry/readability failures are reported as a
        probe result rather than raised; a selected harness is the deliberate
        exception, because it is a valid registry entry but not a transport
        endpoint and must name its kind.
        """
        from llm_kit.dispatch.reachability import probe_model_endpoint  # noqa: PLC0415

        return probe_model_endpoint(
            self._entry_id(),
            timeout=timeout,
            project_root=self.project_root,
        )

    def _entry_reasoning_effort(self) -> "tuple[Optional[str], Optional[str]]":
        """``(declared reasoning_effort, effort style)`` of the selected entry.

        The declared value is the entry's ``reasoning_effort`` default, or None.
        The style is how a direct call to the entry delivers an effort
        (``top-level``, ``ninfer``, ``chat_template_kwargs``), or None when it
        delivers none; it comes from ``resolve_endpoint_profile``, the
        resolution the OpenAI-compatible adapter makes for the same entry, so
        the cache key cannot drift from the wire. Without the effort seam (see
        :func:`_effort_seam`) the style is always None and the default is read
        from the registry entry.

        Cached on the instance, including a None result -- a registry without
        the field must not re-read the file on every call. Ordinary lookup
        failures resolve to ``(None, None)``; a harness refusal is preserved so
        callers do not receive a misleading transport error.
        """
        if self._effort is not _UNSET:
            return self._effort
        seam = _effort_seam()
        result: "tuple[Optional[str], Optional[str]]" = (None, None)
        if seam is not None:
            # _entry_id raises a default-harness refusal and swallows the rest;
            # resolve_endpoint_profile never raises (an explicitly selected
            # harness resolves no effort, and the delegate refuses it on use).
            # An unresolved id (None) is passed on as-is: the delegate is built
            # with that same id, so both resolve the same endpoint.
            profile = seam[1](self._entry_id(), project_root=self.project_root)
            result = (profile.declared_effort, profile.effort.style)
        else:
            try:
                from llm_kit.registry.model_endpoints import (  # noqa: PLC0415
                    resolve_registry_entry,
                )

                result = (resolve_registry_entry(self._entry_id()).reasoning_effort, None)
            except Exception as exc:  # noqa: BLE001 -- a missing default is not an error
                if _is_harness_refusal(exc):
                    raise
        self._effort = result
        return result

    def _legacy_effective_options(self, opts: BackendOptions) -> BackendOptions:
        """Top-level ``extras["reasoning_effort"]`` injection, style-blind.

        What a delegate without ``plan_effort`` sends: it ignores
        ``options.effort`` and forwards ``extras`` verbatim, so the entry
        default has to ride in ``extras`` to reach the wire at all.
        """
        extras = dict(opts.extras or {})
        if "reasoning_effort" not in extras:
            default, _style = self._entry_reasoning_effort()
            if default:
                extras["reasoning_effort"] = default
        elif extras["reasoning_effort"] is None:
            extras.pop("reasoning_effort")
        return replace(opts, extras=extras)

    def _delegate_options(self, opts: BackendOptions) -> BackendOptions:
        """``opts`` with ``effort`` defaulted from the entry; ``extras`` untouched.

        The delegate's ``plan_effort`` applies the precedence (see
        :meth:`effective_options`), so the caller's extras -- an explicit
        ``None`` included -- are forwarded as given.
        """
        if opts.effort is not None:
            return opts
        default, _style = self._entry_reasoning_effort()
        return replace(opts, effort=default) if default is not None else opts

    def effective_options(self, options: Optional[BackendOptions] = None) -> BackendOptions:
        """Resolve ``options`` to what actually reaches the provider.

        Returns ``options`` with ``extras`` replaced by exactly the extra body
        the delegate sends: the entry's effort placed in the entry's effort
        style, or the caller's own effort, or nothing. This is the seam
        :func:`platform.build_cache_key` needs: the registry default and the
        endpoint's wire style are resolved HERE, inside the backend, which is
        downstream of where a caller builds its cache key, so a caller that
        hashes its own pre-resolution options would key two entries with
        different registry defaults identically even though they serve
        different completions. Any caller building a cache key -- or otherwise
        needing the options that will actually be sent -- should call this
        first when the backend exposes it (duck-typed via ``getattr``; a
        backend that resolves nothing beyond the caller's own options need not
        implement it).

        ``effort`` is returned as the caller passed it, not cleared, so a key
        stays equal to the one earlier releases computed wherever the extra
        body is unchanged. The key therefore changes exactly where the wire
        does.

        Precedence, highest first (``llm_kit.registry.effort.plan_effort``):

        1. the caller's ``extras`` name an effort, top-level
           (``extras["reasoning_effort"]``) or nested
           (``extras["chat_template_kwargs"]["reasoning_effort"]``); the
           top-level one wins when both are present. A value is sent verbatim;
           an explicit ``None`` suppresses the parameter entirely and lets the
           server's own default win;
        2. ``options.effort``, else the selected registry entry's
           ``reasoning_effort``, placed in the entry's effort style (NInfer
           maps ``high`` to ``xhigh``); an entry that resolves no style sends
           none;
        3. neither -- the parameter is not sent, so the server decides.

        The plugin ships no effort value of its own; the fleet default lives in
        the private registry, per entry. Without the effort seam this falls
        back to the legacy behavior: the entry default injected as a top-level
        ``extras["reasoning_effort"]`` whatever the entry's style.
        """
        opts = options or BackendOptions()
        seam = _effort_seam()
        if seam is None:
            return self._legacy_effective_options(opts)
        _default, style = self._entry_reasoning_effort()
        sent = self._delegate_options(opts)
        plan = seam[0](sent.extras, sent.effort, style)
        return replace(opts, extras=dict(plan.extra_body))

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        """Complete, defaulting reasoning effort from the registry entry.

        The delegate receives ``effort`` (the caller's, else the entry's) and
        the caller's ``extras`` unchanged, and places the effort itself; see
        :meth:`effective_options` for the precedence it applies.
        """
        opts = options or BackendOptions()
        if _effort_seam() is None:
            sent = self._legacy_effective_options(opts)
        else:
            sent = self._delegate_options(opts)
        return _complete_through(self._backend, system, user, model=model, opts=sent)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        """Connection failures are halts here; everything else defers."""
        if _is_connection_error(exc):
            return platform.HALT_UNREACHABLE
        return self._backend().classify_halt(exc)


@dataclass
class MockBackend:
    """Deterministic, scriptable completion backend for tests.

    Scriptable three ways:

    - ``responses`` -- a FIFO queue drained one per call. Each entry is a
      ``str`` (the response text), a ``dict`` (``{text, model?, input_tokens?,
      ...}`` including optional ``finish_reason`` and ``reasoning``), an
      :class:`LLMResponse` (served verbatim), or an ``Exception`` instance
      (raised, to exercise retry / halt paths).
    - ``keyed_responses`` -- a ``{substring: entry}`` map: the first key found
      in the user prompt wins (order-independent concurrency tests).
    - both empty -- every call raises ``RuntimeError("MockBackend exhausted")``.

    ``classify_halt`` maps a raised entry to a halt kind when its message
    carries a marker, so a scripted ``PipelineHaltError``-shaped exception halts,
    or when it carries a truthy ``halt_kind`` attribute (a CodexRunError-shaped
    exception; migration step 10) -- reported verbatim, exactly like the
    real ``CodexCliBackend.classify_halt``, with no import of
    an adapter (this backend stays hermetic). Every
    call's kwargs are recorded on ``self.calls``.

    THREAD SAFETY: one instance may be shared across worker threads. The
    ``calls`` recording and the ``responses`` FIFO drain are both guarded by
    an internal lock, so a concurrent stage cannot interleave call records or
    race the check-then-pop of the queue. Note this makes each ENTRY atomic,
    not the ORDER: under concurrency, ``responses`` is drained in completion
    order, so a test asserting a specific per-thread response pairing wants
    ``keyed_responses`` (matched on the prompt) rather than the FIFO.
    """

    responses: Optional[List[Any]] = None
    keyed_responses: Optional[Dict[str, Any]] = None
    default_model: str = "mock-model"
    name: str = field(default="mock", init=False)
    calls: List[Dict[str, Any]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._queue: List[Any] = list(self.responses) if self.responses else []
        # Concurrent pipelines share ONE backend across worker threads (the
        # convergence-loop stages dispatch through a ThreadPoolExecutor), so
        # the recording and the FIFO drain must be atomic: without this,
        # ``if not self._queue`` / ``pop(0)`` is a check-then-act race that
        # surfaces as an IndexError instead of the intended "exhausted"
        # error, and interleaved appends scramble call ordering.
        self._lock = threading.Lock()

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
                reasoning=str(entry.get("reasoning", "")),
                finish_reason=entry.get("finish_reason"),
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
        with self._lock:
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
        with self._lock:
            if not self._queue:
                raise RuntimeError("MockBackend exhausted")
            entry = self._queue.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return self._coerce(entry, effective_model)

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        if isinstance(exc, platform.PipelineHaltError):
            return exc.kind
        halt_kind = getattr(exc, "halt_kind", None)
        if halt_kind:
            # A scripted CodexRunError-shaped exception (migration step
            # 10) carries its own verdict -- report it verbatim, exactly like
            # the real CodexCliBackend.classify_halt does, without importing
            # an adapter (MockBackend stays hermetic: no network, no
            # subprocess, no adapter).
            return halt_kind
        return platform.classify_halt_text(str(exc))


# ---------------------------------------------------------------------------
# Process-level routing
# ---------------------------------------------------------------------------

MODELS_ENV = "CONTENT_PIPELINE_LLM_MODELS"
"""One model declaration -- a comma-separated list of ``llm_kit.registry``
ids (``bootstrap_lib.model_declaration``'s format; see bootstrap's
``plugin-dev`` skill, ``references/model-declaration.md``) naming which
model(s) may serve a completion in this process. It is the only routing env.
Resolved through ``llm_kit.dispatch.declaration.describe`` (the routing
layer) via :func:`resolve_declaration`, so quota pacing, reachability, and the
itemised :class:`~llm_kit.dispatch.declaration.NoUsableRoutingTarget` floor
apply exactly as they do for every other declaration-driven caller. Unset
uses a deliberately configured default_endpoint; with no declaration or
configured default, routing fails before execution.
"""


def _declaration_module() -> Any:
    """Return ``llm_kit.dispatch.declaration``, the routing layer.

    A named seam: :func:`resolve_declaration` reaches the declaration module
    only through this function.
    """
    from llm_kit.dispatch import declaration as _declaration  # noqa: PLC0415

    return _declaration


def declared_model_names(
    models: "Optional[Sequence[str]]" = None,
) -> Optional[List[str]]:
    """The model declaration in force, or ``None`` when there is none.

    An explicit ``models`` list (ordered registry ids, passed per
    pipeline or per call) wins; an empty one is a ``ConfigurationError``.
    With ``models=None`` the process-wide :data:`MODELS_ENV` is the
    backward-compatible default: the parsed value, or ``None`` when unset.

    Splits on commas (the "repeated CLI flag or comma list" env carrier of
    the declaration format); each part is stripped and empty parts are
    dropped. Structural validation (empty-list, duplicates) happens inside
    ``llm_kit.dispatch.declaration.describe`` via
    ``bootstrap_lib.model_declaration``, not here.
    """
    if models is not None:
        names = [str(m).strip() for m in models if str(m).strip()]
        if not names:
            raise platform.ConfigurationError(
                "an explicit models declaration must name at least one "
                "llm-scripting-kit model id"
            )
        return names
    raw = os.environ.get(MODELS_ENV, "").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _configured_default_names(project_root: Optional[str]) -> List[str]:
    """Resolve a deliberate config default or refuse before any backend runs."""
    from llm_kit.registry.models import default_declaration  # noqa: PLC0415

    root = project_root or os.getcwd()
    names = default_declaration(project_root=root)
    if not names:
        raise platform.ConfigurationError(
            f"No model is declared. Set {MODELS_ENV}, configure "
            "default_endpoint in your llm-kit config, or supply an "
            "explicit backend (workflow-kit users can pass --model)."
        )
    return names


def resolve_declaration(
    names: "Sequence[str] | List[str]",
    *,
    project_root: Optional[str] = None,
    requirements: Optional[Dict[str, Any]] = None,
) -> Any:
    """Resolve an explicit declaration to its first usable registry entry.

    Thin caller of ``llm_kit.dispatch.declaration.describe(caller="process")``
    -- the pipeline runtime keeps its own retry/cache/budget loop
    (``platform.call_llm``), so it plays job-kit's role (a caller WITH a loop
    of its own) rather than an unattended ``run`` caller. Propagates
    :class:`~llm_kit.dispatch.declaration.NoUsableRoutingTarget` (R23) and
    :class:`DeclarationSupportError` / ``ImportError`` for an absent or stale
    ``bootstrap_lib``. Skipping is silent (R18, R19).

    ``requirements`` is handed to ``describe`` so an entry whose adapter
    cannot satisfy them is skipped rather than selected and refused at
    dispatch. :func:`route` passes an output contract's
    ``contract_requirements``. Empty or ``None`` passes nothing, so the call
    is exactly the no-requirement call.
    """
    declaration = _declaration_module()
    extra: Dict[str, Any] = {"requirements": requirements} if requirements else {}
    ranking = declaration.describe(
        list(names), project_root=project_root, caller=declaration.CALLER_PROCESS,
        **extra,
    )
    return ranking.default


def declared_backend_and_model(
    names: "Sequence[str] | List[str]", *, project_root: Optional[str] = None
) -> "tuple[str, str]":
    """Resolve a declaration to ``(backend_name, model_id)`` for a run record.

    ``backend_name`` is the resolved entry's process drive name (the
    adapter family: ``claude-cli`` / ``codex-cli`` /
    ``opencode-cli`` / ``openrouter``, or a model-endpoint transport entry's
    own id); ``model_id`` is the entry's concrete model. Intended for a
    caller building a ``RunRecord``
    (or the ``create-run`` CLI's ``--models`` flag) so the record stores the
    CHOSEN entry rather than a caller-guessed label (C2).
    """
    entry = resolve_declaration(names, project_root=project_root)
    return entry.drive, (entry.model or entry.id)


_declared_entry_cache: Dict[Any, Any] = {}
"""Per-process memo of :func:`resolve_declaration` keyed by (names, root,
output-contract identity).

A declaration governs a whole run; re-probing reachability and quota on every
call site would multiply live probes for no benefit. The contract identity is
part of the key because a contract narrows the usable entries: a no-contract
selection must never stand in for a contract one, nor one contract's for
another's. Cleared by :func:`reset_declared_entry_cache` (a test seam, and a
legitimate call after a mid-run re-selection that should re-probe)."""


def _resolve_declared_entry(
    *,
    project_root: Optional[str] = None,
    output_contract: Optional[Any] = None,
    models: "Optional[Sequence[str]]" = None,
) -> Any:
    """The first usable entry for the ACTIVE :data:`MODELS_ENV` declaration, memoized.

    Callers gate on ``declared_model_names() is not None`` before calling
    this (:func:`route`, :func:`routed_model`), so ``names`` is never
    ``None`` here.

    With an ``output_contract`` the selection requirement comes from
    the completion layer's ``contract_requirements``, reached through
    :func:`~llm_kit.pipeline.runtime.platform._contract_seam`, and the memo is keyed
    on the contract's ``identity()``.
    """
    names = declared_model_names(models)
    identity: Optional[tuple] = None
    requirements: Optional[Dict[str, Any]] = None
    if output_contract is not None:
        seam = platform._contract_seam()
        platform._require_contract(output_contract, seam)
        identity = tuple(output_contract.identity())
        requirements = seam.contract_requirements(output_contract)
    key = (tuple(names), project_root, identity)
    if key not in _declared_entry_cache:
        _declared_entry_cache[key] = resolve_declaration(
            names, project_root=project_root, requirements=requirements
        )
    return _declared_entry_cache[key]


def reset_declared_entry_cache() -> None:
    """Test seam: clear the per-process declaration memo."""
    _declared_entry_cache.clear()


def _backend_for_entry(entry: Any) -> Any:
    """Instantiate the pipeline wrapper for a resolved declaration entry."""
    harness = (entry.harness or "").lower()
    if harness == "claude":
        return ClaudeCliBackend()
    if harness == "codex":
        return CodexCliBackend()
    if harness == "opencode":
        return OpencodeCliBackend()
    if entry.id == "openrouter":
        return OpenRouterBackend()
    return ModelEndpointBackend(endpoint=entry.id)


REMOVED_ROUTING_ENVS = (
    "CONTENT_PIPELINE_LLM_BACKEND",
    "CONTENT_PIPELINE_LLM_MODEL",
    "CONTENT_PIPELINE_LLM_ENDPOINT",
)
"""Routing settings that no longer route anything. Setting one without
:data:`MODELS_ENV` is refused (:func:`_refuse_removed_routing_env`) rather
than ignored, because ignoring it could change the selected model and every
response-cache key."""


def _refuse_removed_routing_env(models: "Optional[Sequence[str]]" = None) -> None:
    """Raise :class:`~llm_kit.pipeline.runtime.platform.ConfigurationError` when a
    removed routing env is set and :data:`MODELS_ENV` is not.

    Runs before the declaration is read, any network probe, or any cache
    lookup. A blank value counts as unset. When :data:`MODELS_ENV` is set the
    leftover names are inert and pass silently: the operator has already
    chosen the replacement, and a per-call warning would repeat on every
    routed call.
    """
    if declared_model_names(models) is not None:
        return
    stale = [n for n in REMOVED_ROUTING_ENVS if os.environ.get(n, "").strip()]
    if stale:
        raise platform.ConfigurationError(
            f"{', '.join(stale)} no longer select a backend or model. "
            f"Unset {'it' if len(stale) == 1 else 'them'} and set {MODELS_ENV} "
            "to a comma-separated list of llm-scripting-kit model ids instead "
            f"(for example {MODELS_ENV}=<entry id>); without it no model is "
            "selected unless default_endpoint is configured."
        )


def route(
    *,
    openrouter: Optional[Any] = None,
    mock: Optional[Any] = None,
    output_contract: Optional[Any] = None,
    models: "Optional[Sequence[str]]" = None,
    project_root: Optional[str] = None,
) -> Any:
    """Return the backend for this call's declaration.

    ``models`` is the explicit, ordered list of registry ids for THIS
    pipeline or call; it wins over :data:`MODELS_ENV`, which stays only as the
    process-wide default when ``models`` is ``None``. Two pipelines in one
    process may therefore route differently. The memo key includes the names.

    A supplied ``mock`` wins UNCONDITIONALLY -- checked before the
    declaration is even read. This is the seam that keeps tests off a live
    transport; it must never be contingent on environment state.

    When :data:`MODELS_ENV` is set, the backend is resolved through the
    declaration (:func:`_resolve_declared_entry`) --
    ``NoUsableRoutingTarget`` (R23) propagates uncaught. A ``model-endpoint``-kind entry is probed before
    being returned: a registry server is up only if somebody started it, so
    a dead one refuses here rather than once per unit. Unset, a deliberately
    configured ``default_endpoint`` is used; with no declaration or configured
    default, routing raises before execution. A supplied ``openrouter`` instance
    remains an explicit backend selection. When the declaration resolves to the
    ``openrouter`` entry and an ``openrouter`` instance was supplied, that
    instance is returned (never a fresh one), so the caller's client, endpoint
    pin and budget wrapper survive a declaration; an entry naming another
    transport still wins over the supplied instance. A removed routing env set without
    :data:`MODELS_ENV` raises ``ConfigurationError`` instead (a supplied
    ``mock`` is still returned first).

    ``output_contract`` (a ``llm_kit.completion.contract_types.OutputContract``, the same
    object the call will carry on ``BackendOptions.output_contract``) makes
    the declaration select only entries whose adapter can satisfy it: its
    ``contract_requirements`` reach ``describe``, so a ``native-required``
    contract skips an entry that cannot deliver natively instead of selecting
    it and refusing at dispatch. The default entry (no :data:`MODELS_ENV`) is
    not re-selected: the adapter's pre-dispatch refusal guards it, so an
    unsatisfiable contract fails before any request rather than downgrading.
    """
    if mock is not None:
        return mock
    _refuse_removed_routing_env(models)
    names = declared_model_names(models)
    if names is None and openrouter is not None:
        return openrouter
    if names is None:
        project_root = project_root or os.getcwd()
        names = _configured_default_names(project_root)
    if names is not None:
        entry = _resolve_declared_entry(
            output_contract=output_contract, models=names, project_root=project_root
        )
        if entry.id == "openrouter" and openrouter is not None:
            # The caller's instance IS the openrouter entry: it carries the
            # client, endpoint pin and budget wrapper a bare per-call
            # OpenRouterBackend() would drop, and is reused across calls.
            return openrouter
        backend = _backend_for_entry(entry)
        # PROBE ONLY THE SELECTED ENTRY, and only here. One ping per route()
        # call. A server that dies MID-run surfaces instead as
        # HALT_UNREACHABLE on the failing call.
        if isinstance(backend, ModelEndpointBackend):
            probe = backend.probe()
            if not probe.ok:
                raise platform.LLMUnavailableError(
                    f"model endpoint {probe.endpoint!r} is unavailable: "
                    f"{probe.detail}. Start that server (if it is one of "
                    f"yours) or change {MODELS_ENV}."
                )
        return backend
    raise platform.ConfigurationError(
        f"No model is declared. Set {MODELS_ENV} or configure default_endpoint."
    )


def routed_model(
    requested_model: str,
    *,
    backend_name: Optional[str] = None,
    openrouter: Optional[Any] = None,
    mock: Optional[Any] = None,
    output_contract: Optional[Any] = None,
    models: "Optional[Sequence[str]]" = None,
    project_root: Optional[str] = None,
) -> str:
    """Resolve the model a routed call should run, truthfully.

    When :data:`MODELS_ENV` (or ``models``) is set, ``backend_name`` is
    ignored in favour of the declaration's resolved entry. If that entry is
    ``openrouter`` and the caller requested a model (non-empty
    ``requested_model``), the request wins: an OpenRouter slug is meaningful
    to that transport and the caller chose it explicitly. Otherwise the
    entry's own concrete model IS the truthful answer (Y1: a CLI or endpoint
    entry cannot run an OpenRouter slug, and an empty request defers to the
    declaration).

    A supplied ``mock`` wins first, as in :func:`route`: it runs
    ``requested_model``. With no explicit declaration, an explicitly supplied
    ``openrouter`` instance (as for :func:`route`) is preserved and never
    raises: that backend runs ``requested_model``. With neither a declaration nor a
    supplied backend, the configured default declaration selects the entry,
    and when there is none this raises ``ConfigurationError`` rather than
    returning an implicit OpenRouter model. ``backend_name`` does not
    select anything: it is accepted for call-site compatibility and ignored.
    The returned id is what lands on ``LLMResponse.model`` and therefore on
    audit records.

    Pass the same ``models``, ``openrouter``, ``mock`` and ``output_contract`` given to
    :func:`route`: a contract can select a different entry, and the model
    must be that entry's.
    """
    if mock is not None:
        return requested_model
    _refuse_removed_routing_env(models)
    names = declared_model_names(models)
    if names is None and openrouter is not None:
        return requested_model
    if names is None:
        project_root = project_root or os.getcwd()
        names = _configured_default_names(project_root)
    entry = _resolve_declared_entry(
        output_contract=output_contract, models=names, project_root=project_root
    )
    if entry.id == "openrouter" and requested_model:
        return requested_model
    return entry.model or requested_model


__all__ = [
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "CodexCliBackend",
    "OpencodeCliBackend",
    "ModelEndpointBackend",
    "MockBackend",
    "MODELS_ENV",
    "REMOVED_ROUTING_ENVS",
    "OPENCODE_FILESYSTEM_POSTURE",
    "route",
    "routed_model",
    "declared_model_names",
    "resolve_declaration",
    "declared_backend_and_model",
    "reset_declared_entry_cache",
]
