"""Completion backends behind one protocol, selected by process-level routing.

Six backends implement :class:`~content_pipeline.llm.platform.LLMBackend`:

- :class:`OpenRouterBackend` -- an OpenAI-compatible HTTP completion.
- :class:`ModelEndpointBackend` -- an OpenAI-compatible completion against an
  entry in the model-endpoints registry, typically a locally hosted keyless
  server. Unlike the others its availability is NOT assumed: :func:`route`
  pings the selected entry before returning it.
- :class:`ClaudeCliBackend` -- the local ``claude -p`` CLI (subscription-billed,
  no per-call metering).
- :class:`CodexCliBackend` -- the local ``codex exec`` CLI (subscription-billed,
  no per-call metering).
- :class:`OpencodeCliBackend` -- the local ``opencode run`` CLI, with stdout as
  the answer and a mandatory wall-clock timeout.
- :class:`MockBackend` -- deterministic, scriptable responses. The always-wins
  test seam: no network, no subprocess, no shared lib.

The five live transports are THIN ADAPTERS over ``llm_scripting_kit.completion``
(from llm-scripting-kit): that shared lib owns the actual completion
transport -- the ``claude -p``, ``codex exec``, and ``opencode run`` subprocess
runners, retry, timeout, hard-stop detection, and the OpenAI-compatible client
and prompt-cache message shaping.
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
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from content_pipeline.llm import platform
from content_pipeline.llm.platform import BackendOptions, LLMResponse

# The message a live backend raises when the shared lib is missing. The
# openrouter / model-endpoint / claude-cli / codex-cli / opencode-cli transports
# genuinely require llm_scripting_kit; only the MockBackend path is hermetic.
_MISSING_LIB_MSG = (
    "needs the 'llm_scripting_kit' shared lib (from llm-scripting-kit). Declare it "
    "via the plugin's shared_lib_imports, or use MockBackend for tests."
)


_UNSET = object()
"""Distinguishes "not looked up yet" from a looked-up None (see
:meth:`ModelEndpointBackend._entry_reasoning_effort`)."""


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

    ``llm_scripting_kit`` owns entry classification and the canonical refusal
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
    ``llm_scripting_kit`` backend with its OWN client slot, so each one goes on
    to build its own OpenAI client -- an ``ssl.create_default_context
    (cafile=certifi.where())`` and a file descriptor apiece. Measured
    downstream: 900 threads through a SINGLE shared transport still
    materialized 509 distinct clients and raised 391 ``[Errno 24] Too many open
    files`` errors against Windows' 512-entry table. Synchronizing only the
    shared lib's ``_ensure_client`` does NOT fix this -- surplus delegates each
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
        client_id=opts.client_id,
        log_prefix=opts.log_prefix,
        extras=opts.extras,
    )


def _error_to_data(error: Any) -> Any:
    """Reduce a seam ``ResponseError`` to plain data, duck-typed.

    ``getattr``-based rather than an isinstance check for the same reason
    ``total_tokens`` is read with ``getattr`` below: this package must keep
    working against a shared lib that predates the type, and it must not import
    the seam to do it.
    """
    if error is None:
        return None
    to_json = getattr(error, "to_json", None)
    return to_json() if callable(to_json) else error


def _from_completion_response(resp: Any) -> LLMResponse:
    """Adapt an ``llm_scripting_kit.completion.LLMResponse`` into ours.

    Field-for-field, and deliberately explicit in BOTH directions: the option
    conversion above exists so a field drift surfaces rather than mis-binding,
    and this one earns its keep the same way. ``total_tokens`` is read with
    ``getattr`` because a consumer may be running against an older shared lib
    that predates the field -- the shared lib reaches every consumer at once
    with no version pin, so the two can legitimately be out of step here. The
    same compatibility rule applies to every truthfulness field below.
    """
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
    )


@dataclass
class _LazyDelegate:
    """Shared double-checked-locked lazy delegate build for the thin adapters.

    Every live transport (:class:`OpenRouterBackend`, :class:`ClaudeCliBackend`,
    :class:`CodexCliBackend`, :class:`OpencodeCliBackend`,
    :class:`ModelEndpointBackend`) wraps exactly one ``llm_scripting_kit.completion``
    backend, built lazily and cached on ``self._delegate`` (see
    :func:`_lazy_build_note` for why the build itself must be lock-guarded, and
    why nothing else in the request path needs to be). This base class owns
    that mechanism -- :meth:`_build` -- plus the ``complete`` / ``classify_halt``
    bodies that are byte-identical across the four adapters that need no
    per-call adjustment of their own (``ModelEndpointBackend`` overrides both,
    since it also defaults a registry-declared reasoning effort and treats a
    connection failure as a halt).

    A subclass declares only its OWN fields plus a ``_backend()`` method that
    calls :meth:`_build` with the shared-lib symbol name, this adapter's own
    class name (for the ImportError message), and its constructor kwargs --
    everything else is inherited.
    """

    _delegate: Any = field(default=None, init=False, repr=False, compare=False)
    _build_lock: "threading.Lock" = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def _build(self, import_name: str, label: str, **kwargs: Any) -> Any:
        """Return the cached delegate, building it from ``import_name`` at most once.

        ``import_name`` is the symbol's name inside ``llm_scripting_kit.completion``
        (e.g. ``"OpenRouterBackend"``); ``label`` names THIS adapter class in the
        raised ``ImportError`` so a missing shared lib is diagnosed against the
        adapter actually in use, not the (possibly differently named) delegate
        symbol. ``kwargs`` become the delegate's constructor arguments.
        """
        if self._delegate is not None:
            return self._delegate
        with self._build_lock:
            if self._delegate is None:
                try:
                    from llm_scripting_kit import completion as _completion  # noqa: PLC0415
                    delegate_cls = getattr(_completion, import_name)
                except ImportError as exc:  # pragma: no cover - env-dependent
                    raise ImportError(f"{label} {_MISSING_LIB_MSG}") from exc
                self._delegate = delegate_cls(**kwargs)
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
# OpenRouter (OpenAI-compatible HTTP) backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------


@dataclass
class OpenRouterBackend(_LazyDelegate):
    """OpenAI-compatible HTTP completion, delegated to ``llm_scripting_kit``.

    ``endpoint`` / ``project_root`` / ``client`` are forwarded to
    ``llm_scripting_kit.completion.OpenRouterBackend`` (a caller may inject a
    pre-built ``client`` as the test seam). The delegate is built lazily on
    first use, so constructing this adapter never requires the shared lib.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    endpoint: Optional[str] = None
    project_root: Optional[Path] = None
    client: Any = None
    name: str = field(default="openrouter", init=False)

    def _backend(self) -> Any:
        return self._build(
            "OpenRouterBackend", "OpenRouterBackend",
            endpoint=self.endpoint,
            project_root=self.project_root,
            client=self.client,
        )


# ---------------------------------------------------------------------------
# Claude CLI backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------


@dataclass
class ClaudeCliBackend(_LazyDelegate):
    """Local ``claude -p`` completion, delegated to ``llm_scripting_kit``.

    Spawns the CLI in pure-completion mode (JSON output, no tools by default),
    enforces a per-call timeout, retries transient 5xx envelopes, and maps
    rate-limit / auth markers to a hard stop -- all inside
    ``llm_scripting_kit.completion.ClaudeCliBackend``. Cost is flat zero (the CLI
    bills at its own subscription, not per call).

    Config fields forward to the delegate; ``runner`` is the subprocess seam
    (``None`` uses the shared lib's battle-tested ``run_claude_streaming``). The
    delegate is built lazily so constructing this adapter never requires the
    shared lib. ``retry_max_attempts`` defaults to 1 (run-once: one request,
    at most one CLI invocation). Retry is this adapter's explicit policy; the
    higher-level pipeline retry policy should leave it at 1 to avoid a second
    retry loop.

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
    name: str = field(default="claude-cli", init=False)

    def _backend(self) -> Any:
        kwargs: Dict[str, Any] = {
            "default_timeout_s": self.default_timeout_s,
            "retry_max_attempts": self.retry_max_attempts,
            "retry_cooldown_s": self.retry_cooldown_s,
            "diagnostics_dir": self.diagnostics_dir,
            "executable": self.executable,
        }
        if self.runner is not None:
            kwargs["runner"] = self.runner
        return self._build("ClaudeCliBackend", "ClaudeCliBackend", **kwargs)


# ---------------------------------------------------------------------------
# Codex CLI backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------


@dataclass
class CodexCliBackend(_LazyDelegate):
    """Local ``codex exec`` completion, delegated to ``llm_scripting_kit``.

    ``default_timeout_s`` / ``argv_prefix`` / ``runner`` are forwarded to
    ``llm_scripting_kit.completion.CodexCliBackend``. The delegate is built
    lazily so constructing this adapter never requires the shared lib.

    THREAD SAFETY: one instance may be shared across worker threads; the lazy
    delegate build is guarded by double-checked locking (see
    :func:`_lazy_build_note`, mechanism in :class:`_LazyDelegate`).
    """

    default_timeout_s: float = 900.0
    argv_prefix: Optional[tuple] = None
    runner: Optional[Callable[..., "tuple[str, str, int]"]] = None
    name: str = field(default="codex-cli", init=False)

    def _backend(self) -> Any:
        kwargs: Dict[str, Any] = {
            "default_timeout_s": self.default_timeout_s,
            "argv_prefix": self.argv_prefix,
        }
        if self.runner is not None:
            kwargs["runner"] = self.runner
        return self._build("CodexCliBackend", "CodexCliBackend", **kwargs)


# ---------------------------------------------------------------------------
# OpenCode CLI backend -- delegates to llm_scripting_kit
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
    """Local ``opencode run`` completion, delegated to llm-scripting-kit.

    ``default_timeout_s`` / ``argv_prefix`` / ``runner`` are forwarded to
    ``llm_scripting_kit.completion.OpencodeCliBackend``. The
    delegate is built lazily so constructing this adapter never requires the
    shared lib or the ``opencode`` executable.

    OpenCode's non-interactive command requires ``--auto``, which bypasses
    permissions; its ``--dir`` option is not a write boundary. The shared
    backend emits that warning at call time and returns the answer on stdout.
    ``filesystem_posture`` records the run-level decision made here: this
    adapter accepts the explicitly selected backend's unconfined posture and
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
    name: str = field(default="opencode-cli", init=False)
    filesystem_posture: str = field(
        default=OPENCODE_FILESYSTEM_POSTURE, init=False
    )

    def _backend(self) -> Any:
        kwargs: Dict[str, Any] = {
            "default_timeout_s": self.default_timeout_s,
            "argv_prefix": self.argv_prefix,
        }
        if self.runner is not None:
            kwargs["runner"] = self.runner
        return self._build("OpencodeCliBackend", "OpencodeCliBackend", **kwargs)


# ---------------------------------------------------------------------------
# Mock backend (test seam)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Model-endpoint backend -- delegates to llm_scripting_kit
# ---------------------------------------------------------------------------

ENDPOINT_ENV = "CONTENT_PIPELINE_LLM_ENDPOINT"
"""Selects WHICH registry entry this backend talks to (an entry id).

Empty means the registry's own ``default`` entry. Separate from
:data:`BACKEND_ENV`, which selects the backend itself: one names the transport,
the other names the server.
"""


@dataclass
class ModelEndpointBackend(_LazyDelegate):
    """Completion against a registered model endpoint.

    The entry comes from llm-scripting-kit's model-endpoints registry -- a
    private, fleet-propagating list of OpenAI-compatible endpoints, typically
    locally hosted and keyless. "Local" is a property of an ENTRY, not of this
    backend, so nothing here assumes localhost.

    AVAILABILITY IS NOT ASSUMED, which is what separates this adapter from its
    four siblings. A cloud provider is up unless it is having an incident; a
    server on the registry is up only if somebody started it. :func:`route`
    therefore pings the selected entry at selection time and refuses with
    :class:`~content_pipeline.llm.platform.LLMUnavailableError` rather than
    letting a bulk run discover the same dead host once per unit.

    ``endpoint`` empty means the registry's default entry, resolved on first
    use so that ``.endpoint`` is a concrete entry id by the time it reaches a
    probe or a cache key.

    THREAD SAFETY: as :class:`OpenRouterBackend` -- one instance may be shared
    across worker threads; the lazy delegate build is double-checked-locked.
    """

    endpoint: str = field(
        default_factory=lambda: os.environ.get(ENDPOINT_ENV, "").strip()
    )
    project_root: Optional[Path] = None
    client: Any = None
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
        ``resolve_endpoint``, whose default is the llm-scripting-kit config's
        default endpoint -- ``openrouter`` -- and NOT this registry's own
        ``default:`` key. That mismatch is not cosmetic: it makes an unset
        CONTENT_PIPELINE_LLM_ENDPOINT probe OpenRouter and fail with
        "no API key resolved", a nonsense diagnosis for a backend whose entries
        are typically keyless and local.

        Resolution is cached onto ``endpoint`` so the id is concrete by the time
        it reaches a probe or a cache key. A registry that cannot be read leaves
        it unresolved and returns None -- the probe then reports that failure as
        its own ``detail``, which is the honest answer to "is it usable?". The
        shared library's harness refusal is intentionally propagated: a harness
        is a valid registry entry, but not a transport endpoint, and must not be
        turned into a misleading default or missing-base-url error here.
        """
        if self.endpoint:
            return self.endpoint
        try:
            from llm_scripting_kit.model_endpoints import (  # noqa: PLC0415
                resolve_registry_entry,
            )

            self.endpoint = resolve_registry_entry(None).id
        except Exception as exc:  # noqa: BLE001 -- unresolvable is the probe's to report
            if _is_harness_refusal(exc):
                raise
            return None
        return self.endpoint

    def _backend(self) -> Any:
        # Delegates to the same completion.OpenRouterBackend as
        # OpenRouterBackend itself -- an OpenAI-compatible client works
        # against any base URL, and the model-endpoints registry entry
        # supplies this one's.
        return self._build(
            "OpenRouterBackend", "ModelEndpointBackend",
            endpoint=self._entry_id(),
            project_root=self.project_root,
            client=self.client,
        )

    def probe(self, *, timeout: float = 2.0) -> Any:
        """Non-raising reachability ping of the selected entry.

        Returns an ``EndpointProbe`` (``ok`` / ``endpoint`` / ``base_url`` /
        ``detail``). Ordinary registry/readability failures are reported as a
        probe result rather than raised; a selected harness is the deliberate
        exception, because it is a valid registry entry but not a transport
        endpoint and must name its kind.
        """
        from llm_scripting_kit.account import probe_endpoint  # noqa: PLC0415

        return probe_endpoint(
            self._entry_id(),
            timeout=timeout,
            project_root=str(self.project_root) if self.project_root else None,
        )

    def _entry_reasoning_effort(self) -> Optional[str]:
        """The selected entry's declared ``reasoning_effort``, or None.

        Cached on the instance, including the None result -- a registry without
        the field must not re-read the file on every call. Ordinary lookup
        failures resolve to None; a harness refusal is preserved so callers do
        not receive a misleading transport error.
        """
        if self._effort is not _UNSET:
            return self._effort
        effort: Optional[str] = None
        try:
            from llm_scripting_kit.model_endpoints import (  # noqa: PLC0415
                resolve_registry_entry,
            )

            effort = resolve_registry_entry(self._entry_id()).reasoning_effort
        except Exception as exc:  # noqa: BLE001 -- a missing default is not an error
            if _is_harness_refusal(exc):
                raise
            effort = None
        self._effort = effort
        return effort

    def effective_options(self, options: Optional[BackendOptions] = None) -> BackendOptions:
        """Resolve ``options`` to what actually reaches the provider.

        Defaults ``extras["reasoning_effort"]`` from the selected registry
        entry when the caller left it unset -- see :meth:`complete`'s
        precedence rule. This is the seam :func:`platform.build_cache_key`
        needs: the registry default is resolved HERE, inside the backend,
        which is downstream of where a caller builds its cache key, so a
        caller that hashes its own pre-resolution options would key two
        entries with different registry defaults identically even though
        they serve different completions. Any caller building a cache key --
        or otherwise needing the options that will actually be sent -- should
        call this first when the backend exposes it (duck-typed via
        ``getattr``; a backend that resolves nothing beyond the caller's own
        options need not implement it).

        Precedence, highest first:

        1. ``options.extras["reasoning_effort"]`` -- an explicit level, or an
           explicit ``None`` to suppress the parameter entirely and let the
           server's own default win;
        2. the selected registry entry's ``reasoning_effort``;
        3. neither -- the parameter is not sent, so the server decides.

        The plugin ships no effort value of its own; the fleet default lives in
        the private registry, per entry.
        """
        opts = options or BackendOptions()
        extras = dict(opts.extras or {})
        if "reasoning_effort" not in extras:
            default = self._entry_reasoning_effort()
            if default:
                extras["reasoning_effort"] = default
        elif extras["reasoning_effort"] is None:
            extras.pop("reasoning_effort")
        return replace(opts, extras=extras)

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str,
        options: Optional[BackendOptions] = None,
    ) -> LLMResponse:
        """Complete, defaulting reasoning effort from the registry entry.

        See :meth:`effective_options` for the precedence rule this applies.
        """
        opts = self.effective_options(options)
        resp = self._backend().complete(
            system, user, model=model, options=_to_completion_options(opts)
        )
        return _from_completion_response(resp)

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
    exception, D6 / migration step 10) -- reported verbatim, exactly like the
    real ``CodexCliBackend.classify_halt``, with no import of
    ``llm_scripting_kit`` (this backend stays hermetic). Every
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
            # A scripted CodexRunError-shaped exception (D6, migration step
            # 10) carries its own verdict -- report it verbatim, exactly like
            # the real CodexCliBackend.classify_halt does, without importing
            # llm_scripting_kit (MockBackend stays hermetic: no network, no
            # subprocess, no shared lib).
            return halt_kind
        return platform.classify_halt_text(str(exc))


# ---------------------------------------------------------------------------
# Process-level routing
# ---------------------------------------------------------------------------

BACKEND_ENV = "CONTENT_PIPELINE_LLM_BACKEND"
"""Process-level backend selection env var (empty / unset => openrouter).

DEPRECATED, together with :data:`MODEL_ENV` and :data:`ENDPOINT_ENV`: replaced
by the single :data:`MODELS_ENV` declaration. All three are still honoured
until migration step 12 (mapped to a synthesized one-entry declaration; see
:func:`declared_model_names`), with a deprecation warning when
:data:`MODELS_ENV` is unset and any of the three is set explicitly.
"""

MODEL_ENV = "CONTENT_PIPELINE_LLM_MODEL"
"""Optional override for the model a routed non-openrouter backend runs.

DEPRECATED -- see :data:`BACKEND_ENV`.
"""

MODELS_ENV = "CONTENT_PIPELINE_LLM_MODELS"
"""One model declaration -- a comma-separated list of llm-scripting-kit
registry ids (``bootstrap_lib.model_declaration``'s format; see bootstrap's
``plugin-dev`` skill, ``references/model-declaration.md``) naming which
model(s) may serve a completion in this process. Replaces the
:data:`BACKEND_ENV` / :data:`MODEL_ENV` / :data:`ENDPOINT_ENV` triple
(R37). Resolved through ``llm_scripting_kit.declaration.describe`` (the D3
routing layer) via :func:`resolve_declaration`, so quota pacing, reachability,
and the itemised :class:`~llm_scripting_kit.declaration.NoUsableRoutingTarget`
floor apply exactly as they do for every other declaration-driven caller.
Unset means the legacy triple still governs (R37: honoured until step 12).
"""

_DECLARATION_FLOOR = "0.46.0"
"""The llm-scripting-kit release that shipped ``declaration.describe``/``run``."""

_MISSING_DECLARATION_LIB_MSG = (
    "needs the 'llm_scripting_kit' shared lib (from llm-scripting-kit) for "
    f"{MODELS_ENV} routing, or use the legacy "
    "CONTENT_PIPELINE_LLM_BACKEND/_MODEL/_ENDPOINT triple. Run "
    "`claude plugin install llm-scripting-kit@plugins-kit`."
)
_STALE_DECLARATION_LIB_MSG = (
    "the linked llm_scripting_kit predates llm_scripting_kit.declaration "
    f"(describe/run); {MODELS_ENV} routing needs llm-scripting-kit >= "
    f"{_DECLARATION_FLOOR}. Run `claude plugin update llm-scripting-kit@plugins-kit`."
)

_DECLARATION_SYMBOLS = ("describe", "run", "RunRequest", "NoUsableRoutingTarget", "CALLER_PROCESS")


def _declaration_module() -> Any:
    """Return ``llm_scripting_kit.declaration``, probed for the symbols used.

    Two distinct messages -- absent vs. too old -- per ``plugins/CLAUDE.md``
    ("Optional use of another plugin"): an absent shared lib and a stale one
    have different remedies, so they must not collapse into one diagnosis.
    """
    try:
        from llm_scripting_kit import declaration as _declaration  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(_MISSING_DECLARATION_LIB_MSG) from exc
    if not all(hasattr(_declaration, name) for name in _DECLARATION_SYMBOLS):
        raise ImportError(_STALE_DECLARATION_LIB_MSG)
    return _declaration


def declared_model_names() -> Optional[List[str]]:
    """The parsed :data:`MODELS_ENV` declaration, or ``None`` when unset.

    Splits on commas (the "repeated CLI flag or comma list" env carrier of
    the declaration format); each part is stripped and empty parts are
    dropped. Structural validation (empty-list, duplicates) happens inside
    ``llm_scripting_kit.declaration.describe`` via
    ``bootstrap_lib.model_declaration``, not here.
    """
    raw = os.environ.get(MODELS_ENV, "").strip()
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


_LEGACY_HARNESS_SHIPPED_DEFAULT = {"claude-cli": "opus", "codex-cli": "sol"}
"""The shipped registry entry each legacy CLI backend name maps to.

:data:`MODEL_ENV` under the legacy triple is a raw vendor model id (e.g.
``claude-sonnet-4-6``), not a registry id, so it cannot generally be resolved
to a declaration entry -- the legacy mapping falls back to the harness's
shipped default entry instead (documented lossiness, accepted for a
deprecated path). ``model-endpoint`` is the one legacy backend whose
:data:`ENDPOINT_ENV` already names a registry id exactly, so it maps without
loss.
"""


def _legacy_declaration_name() -> "tuple[str, bool]":
    """Synthesize a one-entry declaration id from the legacy env triple.

    Returns ``(entry_id, is_explicit)``; ``is_explicit`` is True when the
    caller set at least one of :data:`BACKEND_ENV` / :data:`MODEL_ENV` /
    :data:`ENDPOINT_ENV`, which is when the deprecation warning fires -- a
    process running on bare defaults (nothing set at all) gets no noise.
    """
    explicit = any(
        os.environ.get(var, "").strip() for var in (BACKEND_ENV, MODEL_ENV, ENDPOINT_ENV)
    )
    name = active_backend_name()
    if name == "model-endpoint":
        endpoint = os.environ.get(ENDPOINT_ENV, "").strip()
        return endpoint or "openrouter", explicit
    return _LEGACY_HARNESS_SHIPPED_DEFAULT.get(name, "openrouter"), explicit


def resolve_declaration(names: "Sequence[str] | List[str]", *, project_root: Optional[str] = None) -> Any:
    """Resolve an explicit declaration to its first usable llm-scripting-kit entry.

    Thin caller of ``llm_scripting_kit.declaration.describe(caller="process")``
    -- content-pipeline-kit keeps its own retry/cache/budget loop
    (``platform.call_llm``), so it plays job-kit's role (a caller WITH a loop
    of its own) rather than an unattended ``run`` caller. Propagates
    :class:`~llm_scripting_kit.declaration.NoUsableRoutingTarget` (R23) and
    :class:`DeclarationSupportError` / ``ImportError`` for an absent or stale
    ``bootstrap_lib`` / ``llm_scripting_kit``. Skipping is silent (R18, R19).
    """
    declaration = _declaration_module()
    ranking = declaration.describe(
        list(names), project_root=project_root, caller=declaration.CALLER_PROCESS,
    )
    return ranking.default


def declared_backend_and_model(
    names: "Sequence[str] | List[str]", *, project_root: Optional[str] = None
) -> "tuple[str, str]":
    """Resolve a declaration to ``(backend_name, model_id)`` for a run record.

    ``backend_name`` is the resolved entry's process drive name (the
    llm-scripting-kit adapter family: ``claude-cli`` / ``codex-cli`` /
    ``opencode-cli`` / ``openrouter``, or a model-endpoint transport entry's
    own id); ``model_id`` is the entry's concrete model. Intended for a
    caller building a :class:`~content_pipeline.execution.model.RunRecord`
    (or the ``create-run`` CLI's ``--models`` flag) so the record stores the
    CHOSEN entry rather than a caller-guessed label (C2).
    """
    entry = resolve_declaration(names, project_root=project_root)
    return entry.drive, (entry.model or entry.id)


_declared_entry_cache: Dict[Any, Any] = {}
"""Per-process memo of :func:`resolve_declaration` keyed by (names, root).

A declaration governs a whole run; re-probing reachability and quota on every
call site would multiply live probes for no benefit. Cleared by
:func:`reset_declared_entry_cache` (a test seam, and a legitimate call after a
mid-run re-selection that should re-probe)."""


def _resolve_declared_entry(*, project_root: Optional[str] = None) -> Any:
    """The first usable entry for the ACTIVE :data:`MODELS_ENV` declaration, memoized.

    Callers gate on ``declared_model_names() is not None`` before calling
    this (:func:`route`, :func:`routed_model`), so ``names`` is never
    ``None`` here -- there is no legacy fallback to resolve at this layer.
    The legacy-triple deprecation warning is a SEPARATE concern, handled by
    :func:`_warn_legacy_env_if_explicit` on the branch where a caller did
    NOT set :data:`MODELS_ENV` at all.
    """
    names = declared_model_names()
    key = (tuple(names), project_root)
    if key not in _declared_entry_cache:
        _declared_entry_cache[key] = resolve_declaration(names, project_root=project_root)
    return _declared_entry_cache[key]


def reset_declared_entry_cache() -> None:
    """Test seam: clear the per-process declaration memo."""
    _declared_entry_cache.clear()


def _warn_legacy_env_if_explicit() -> None:
    """One-shot ``DeprecationWarning`` for the legacy env triple.

    Called from :func:`route` / :func:`routed_model` on the branch where
    :data:`MODELS_ENV` is UNSET -- legacy dispatch itself is unchanged
    (byte-identical), this only surfaces the deprecation notice when at
    least one of :data:`BACKEND_ENV` / :data:`MODEL_ENV` / :data:`ENDPOINT_ENV`
    was set explicitly (never on bare defaults). Needs no
    ``llm_scripting_kit`` / registry resolution -- :func:`_legacy_declaration_name`
    is pure string logic over the env, so this is cheap to call on every
    legacy dispatch.
    """
    entry_id, explicit = _legacy_declaration_name()
    if not explicit:
        return
    import warnings  # noqa: PLC0415

    warnings.warn(
        f"{BACKEND_ENV}/{MODEL_ENV}/{ENDPOINT_ENV} are deprecated; set "
        f"{MODELS_ENV}=[{entry_id}] instead.",
        DeprecationWarning,
        stacklevel=3,
    )


def _backend_for_entry(entry: Any) -> Any:
    """Instantiate the content-pipeline adapter for a resolved declaration entry."""
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
    codex_cli: Optional[Any] = None,
    opencode_cli: Optional[Any] = None,
    model_endpoint: Optional[Any] = None,
    mock: Optional[Any] = None,
) -> Any:
    """Return the process-active backend instance.

    When :data:`MODELS_ENV` is set, the backend is resolved through the
    declaration (:func:`_resolve_declared_entry`) instead of the legacy
    triple below -- ``NoUsableRoutingTarget`` (R23) and an absent/stale
    ``llm_scripting_kit`` propagate uncaught. A ``model-endpoint``-kind
    entry is still probed before being returned, exactly like the legacy
    ``model-endpoint`` branch. A supplied ``mock`` wins UNCONDITIONALLY,
    regardless of
    :data:`BACKEND_ENV` -- checked before the active name is even read, so a
    test can inject a mock without also calling :func:`set_active_backend`.
    This is the seam that keeps tests off a live transport; it must never be
    contingent on environment state. Absent a supplied ``mock``, reads
    :data:`BACKEND_ENV` and returns the active backend, using any other
    caller-supplied instance for that name, otherwise constructing a default.
    """
    if mock is not None:
        return mock
    name = active_backend_name()
    if name == "mock":
        return MockBackend()
    if declared_model_names() is not None:
        backend = _backend_for_entry(_resolve_declared_entry())
        if isinstance(backend, ModelEndpointBackend) and backend.client is None:
            probe = backend.probe()
            if not probe.ok:
                raise platform.LLMUnavailableError(
                    f"model endpoint {probe.endpoint!r} is unavailable: "
                    f"{probe.detail}. Start that server (if it is one of "
                    f"yours) or change {MODELS_ENV}."
                )
        return backend
    _warn_legacy_env_if_explicit()
    if name == "claude-cli":
        return claude_cli if claude_cli is not None else ClaudeCliBackend()
    if name == "codex-cli":
        return codex_cli if codex_cli is not None else CodexCliBackend()
    if name == "opencode-cli":
        return opencode_cli if opencode_cli is not None else OpencodeCliBackend()
    if name == "model-endpoint":
        backend = (
            model_endpoint if model_endpoint is not None else ModelEndpointBackend()
        )
        # PROBE ONLY THE SELECTED ENTRY, and only here. One ping per route()
        # call -- ~4ms when up, at most one 2s timeout when down -- regardless
        # of how many entries the registry holds; the others' state is
        # irrelevant to a run that will not use them. No caching: route() runs
        # about once per run, so a cache buys nothing and can go stale. A
        # server that dies MID-run surfaces instead as HALT_UNREACHABLE on the
        # failing call.
        #
        # An injected client is the caller's affair -- that is the hermetic
        # test seam, and probing it would put tests back on the network.
        if backend.client is None:
            probe = backend.probe()
            if not probe.ok:
                raise platform.LLMUnavailableError(
                    f"model endpoint {probe.endpoint!r} is unavailable: "
                    f"{probe.detail}. Start that server (if it is one of "
                    f"yours), select another registry entry "
                    f"({ENDPOINT_ENV}), or another backend ({BACKEND_ENV})."
                )
        return backend
    return openrouter if openrouter is not None else OpenRouterBackend()


def routed_model(requested_model: str, *, backend_name: Optional[str] = None) -> str:
    """Resolve the model a routed call should run, with truthful substitution.

    An OpenRouter-style slug means nothing to the claude-cli transport, so a
    claude-cli call substitutes :data:`MODEL_ENV` when the requested id does
    not already name that backend's family. Codex model ids are passed through
    unchanged: they are fully qualified ids such as ``gpt-5.6-luna`` or
    ``gpt-5.6-sol`` and bare codenames are not dispatchable. The substituted or
    preserved id is what lands on ``LLMResponse.model`` and therefore on audit
    records.

    When :data:`MODELS_ENV` is set, ``requested_model`` and ``backend_name``
    are both ignored in favour of the declaration's resolved entry -- its own
    concrete model IS the truthful answer, with none of the per-backend
    substitution rules below needed (Y1: a caller like
    ``yaml-data-editor-kit``'s ``PlannerPolicy.model`` is routed through this
    env exactly like every other model choice).
    """
    if declared_model_names() is not None:
        entry = _resolve_declared_entry()
        return entry.model or requested_model
    _warn_legacy_env_if_explicit()
    name = backend_name or active_backend_name()
    if name == "model-endpoint":
        override = os.environ.get(MODEL_ENV, "").strip()
        if override:
            return override
        try:
            from llm_scripting_kit.models import resolve_model  # noqa: PLC0415

            return resolve_model(
                None, endpoint=ModelEndpointBackend()._entry_id()
            )
        except Exception as exc:  # noqa: BLE001 -- truthful fallback beats a guess
            if _is_harness_refusal(exc):
                raise
            return requested_model
    if name == "opencode-cli":
        # OpenCode model ids are provider/model strings from the user's own
        # OpenCode configuration. Do not translate an OpenRouter slug or invent
        # a provider; an explicit process-level override is authoritative.
        return os.environ.get(MODEL_ENV, "").strip() or requested_model
    if name == "codex-cli":
        return requested_model
    if name == "claude-cli" and not requested_model.startswith("claude"):
        return os.environ.get(MODEL_ENV, "").strip() or requested_model
    return requested_model


__all__ = [
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "CodexCliBackend",
    "OpencodeCliBackend",
    "ModelEndpointBackend",
    "MockBackend",
    "BACKEND_ENV",
    "ENDPOINT_ENV",
    "MODEL_ENV",
    "MODELS_ENV",
    "OPENCODE_FILESYSTEM_POSTURE",
    "active_backend_name",
    "set_active_backend",
    "route",
    "routed_model",
    "declared_model_names",
    "resolve_declaration",
    "declared_backend_and_model",
    "reset_declared_entry_cache",
]
