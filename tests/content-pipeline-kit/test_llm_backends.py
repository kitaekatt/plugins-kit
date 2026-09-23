"""Behavioral tests for content_pipeline.llm.backends.

Covers the hermetic surface: the MockBackend seam and process-level routing.
The live transports (OpenRouterBackend, ClaudeCliBackend, CodexCliBackend,
OpencodeCliBackend) are thin adapters
that delegate to ``llm_scripting_kit.completion`` -- the ported transport itself is
covered in tests/llm-scripting-kit (fake-runner subprocess seam, envelope parse,
retry, hard-stop, timeout, halt classification, OpenRouter fake client). Here we
only verify the adapters construct without the shared lib and raise a clear
ImportError when actually driven without it.
"""

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    ClaudeCliBackend,
    CodexCliBackend,
    MockBackend,
    OpenRouterBackend,
    OpencodeCliBackend,
    active_backend_name,
    route,
    routed_model,
    set_active_backend,
)
from content_pipeline.llm.platform import BackendOptions, build_cache_key


# --- MockBackend -------------------------------------------------------------


def test_mock_serves_in_order():
    backend = MockBackend(responses=["a", "b"])
    assert backend.complete("s", "u", model="m").text == "a"
    assert backend.complete("s", "u", model="m").text == "b"


def test_mock_records_calls():
    backend = MockBackend(responses=["a"])
    backend.complete("sys", "usr", model="m1", options=BackendOptions(temperature=0.7))
    assert backend.calls[-1]["system"] == "sys"
    assert backend.calls[-1]["user"] == "usr"
    assert backend.calls[-1]["model"] == "m1"


def test_mock_raises_on_exhaustion():
    backend = MockBackend(responses=[])
    with pytest.raises(RuntimeError, match="exhausted"):
        backend.complete("s", "u", model="m")


def test_mock_dict_entry_carries_usage():
    backend = MockBackend(responses=[{"text": "x", "input_tokens": 12, "output_tokens": 3}])
    resp = backend.complete("s", "u", model="m")
    assert resp.input_tokens == 12
    assert resp.output_tokens == 3


def test_mock_keyed_responses_content_addressed():
    backend = MockBackend(keyed_responses={"alpha": "A", "beta": "B"})
    assert backend.complete("s", "please do beta now", model="m").text == "B"
    assert backend.complete("s", "and alpha too", model="m").text == "A"


def test_mock_keyed_no_match_raises():
    backend = MockBackend(keyed_responses={"alpha": "A"})
    with pytest.raises(RuntimeError, match="no key matched"):
        backend.complete("s", "nothing here", model="m")


def test_mock_exception_entry_is_raised():
    backend = MockBackend(responses=[ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        backend.complete("s", "u", model="m")


def test_mock_default_model_when_blank():
    backend = MockBackend(responses=["x"], default_model="mm")
    assert backend.complete("s", "u", model="").model == "mm"


# --- live-backend delegation (adapter boundary) ------------------------------


def _has_llm_scripting_kit() -> bool:
    try:
        import llm_scripting_kit  # noqa: F401
        return True
    except ImportError:
        return False


def test_openrouter_backend_constructs_without_lib():
    """Constructing the adapter never needs the shared lib -- only driving it."""
    OpenRouterBackend()
    ClaudeCliBackend()


def test_openrouter_backend_requires_lib_when_driven():
    backend = OpenRouterBackend()
    if _has_llm_scripting_kit():  # pragma: no cover - env-dependent
        pytest.skip("llm_scripting_kit importable; delegation path exercised in llm-scripting-kit")
    with pytest.raises(ImportError, match="llm_scripting_kit"):
        backend.complete("s", "u", model="x")


def test_claude_cli_backend_requires_lib_when_driven():
    backend = ClaudeCliBackend()
    if _has_llm_scripting_kit():  # pragma: no cover - env-dependent
        pytest.skip("llm_scripting_kit importable; delegation path exercised in llm-scripting-kit")
    with pytest.raises(ImportError, match="llm_scripting_kit"):
        backend.complete("s", "u", model="x")


def test_claude_cli_backend_defaults_to_run_once() -> None:
    assert ClaudeCliBackend().retry_max_attempts == 1


def test_openrouter_adapter_passes_unset_temperature_to_shared_seam(monkeypatch):
    """The pipeline adapter preserves None for the transport to omit."""
    import sys
    import types

    captured = {}

    class _Delegate:
        def __init__(self, **_kwargs):
            pass

        def complete(self, _system, _user, *, model, options):
            captured["temperature"] = options.temperature
            return types.SimpleNamespace(
                text="ok",
                model=model,
                input_tokens=0,
                output_tokens=0,
                cache_hit_tokens=0,
                wall_ms=0,
                attempts=1,
                from_cache=False,
            )

    class _CompletionOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    package = types.ModuleType("llm_scripting_kit")
    completion = types.ModuleType("llm_scripting_kit.completion")
    completion.BackendOptions = _CompletionOptions
    completion.OpenRouterBackend = _Delegate
    package.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)

    response = OpenRouterBackend().complete("s", "u", model="m")

    assert response.text == "ok"
    assert captured["temperature"] is None


# --- lazy-delegate build under concurrency -----------------------------------


def _install_counting_completion(monkeypatch, built, built_lock):
    """Put a fake ``llm_scripting_kit.completion`` in sys.modules.

    The delegate classes count their own construction and sleep first, so the
    lazy-build race is exercised with production-shaped timing rather than an
    instantaneous stub (an instantaneous probe closes the window and passes
    against broken code). Installed via sys.modules so the test does not depend
    on the real shared lib being importable from this suite.
    """
    import sys
    import time
    import types

    class _Counting:
        def __init__(self, **_kwargs):
            time.sleep(0.05)
            with built_lock:
                built.append(self)

        def classify_halt(self, _exc):
            return None

    pkg = types.ModuleType("llm_scripting_kit")
    completion = types.ModuleType("llm_scripting_kit.completion")
    completion.OpenRouterBackend = _Counting
    completion.ClaudeCliBackend = _Counting
    pkg.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", pkg)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)


@pytest.mark.parametrize("backend_cls", [OpenRouterBackend, ClaudeCliBackend])
def test_concurrent_first_wave_builds_exactly_one_delegate(monkeypatch, backend_cls):
    """N threads racing ``_backend()`` must produce exactly ONE delegate.

    This is the UPPER half of the same defect the shared lib carries in
    ``_ensure_client``: ``if self._delegate is None: ... self._delegate = ...``
    is an unsynchronized check-then-assign. Fixing only the lower layer is not
    sufficient -- each surplus delegate built here is a SEPARATE
    ``llm_scripting_kit`` backend instance with its own ``client`` slot, so each
    one goes on to build its own OpenAI client (an SSL context and a file
    descriptor apiece) no matter how well synchronized that lower build is.
    """
    import threading

    built = []
    built_lock = threading.Lock()
    _install_counting_completion(monkeypatch, built, built_lock)

    n_threads = 24
    backend = backend_cls()
    gate = threading.Barrier(n_threads)
    seen = []
    seen_lock = threading.Lock()

    def worker() -> None:
        gate.wait()
        delegate = backend._backend()
        with seen_lock:
            seen.append(delegate)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1, (
        f"unsynchronized lazy build produced {len(built)} delegates for "
        f"{n_threads} threads -- each carries its own client slot"
    )
    assert len(seen) == n_threads
    assert all(d is built[0] for d in seen)


def test_route_builds_no_delegate(monkeypatch):
    """Routing must build nothing.

    ``route`` runs BEFORE ``call_llm``'s response-cache lookup, so a build at
    routing time would pay the cost (and take the file descriptor) on every
    call including the ones the cache is about to serve.
    """
    import threading

    built = []
    _install_counting_completion(monkeypatch, built, threading.Lock())
    route()
    set_active_backend("claude-cli")
    route()
    set_active_backend(None)
    assert built == []


def test_classify_halt_does_not_rebuild(monkeypatch):
    """A warm ``classify_halt`` must not trigger another build.

    The platform calls ``classify_halt`` unguarded from inside an ``except``
    block. Once the delegate exists the fast path must return it without
    entering the lock, so error handling never queues behind an in-flight
    build.
    """
    import threading

    built = []
    _install_counting_completion(monkeypatch, built, threading.Lock())
    backend = OpenRouterBackend()
    backend._backend()  # warm it once
    assert len(built) == 1
    assert backend.classify_halt(ValueError("boom")) is None
    assert len(built) == 1


# --- routing -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_backend_env(monkeypatch):
    monkeypatch.delenv(backends.BACKEND_ENV, raising=False)
    monkeypatch.delenv(backends.MODEL_ENV, raising=False)
    monkeypatch.delenv(backends.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    backends.reset_declared_entry_cache()
    yield
    backends.reset_declared_entry_cache()


def test_routing_default_is_openrouter():
    assert active_backend_name() == "openrouter"
    assert isinstance(route(), OpenRouterBackend)


def test_routing_set_and_clear():
    set_active_backend("claude-cli")
    assert active_backend_name() == "claude-cli"
    set_active_backend(None)
    assert active_backend_name() == "openrouter"


def test_routing_returns_active_backend():
    set_active_backend("claude-cli")
    assert isinstance(route(), ClaudeCliBackend)
    set_active_backend("codex-cli")
    assert isinstance(route(), CodexCliBackend)
    set_active_backend("opencode-cli")
    assert isinstance(route(), OpencodeCliBackend)
    set_active_backend("mock")
    assert isinstance(route(), MockBackend)


def test_routing_injected_instance_wins():
    set_active_backend("mock")
    mine = MockBackend(responses=["x"])
    assert route(mock=mine) is mine


def test_routing_injected_mock_wins_without_active_backend_set():
    """Regression: a supplied mock must win even with no backend selected.

    Previously ``route`` read ``active_backend_name()`` first and only
    consulted a supplied instance for the name ALREADY active -- so
    ``route(mock=...)`` with ``CONTENT_PIPELINE_LLM_BACKEND`` unset (the
    default state, not merely cleared) silently returned a live
    ``OpenRouterBackend`` and ignored the mock. This test never calls
    ``set_active_backend``, so it fails against that defect and passes only
    when a supplied mock wins unconditionally.
    """
    assert active_backend_name() == "openrouter"
    mine = MockBackend(responses=["x"])
    result = route(mock=mine)
    assert result is mine
    assert not isinstance(result, OpenRouterBackend)


def test_routed_model_substitutes_for_claude(monkeypatch):
    set_active_backend("claude-cli")
    monkeypatch.setenv(backends.MODEL_ENV, "claude-sonnet-4-6")
    # An OpenRouter-style id is substituted.
    assert routed_model("deepseek/deepseek-v4") == "claude-sonnet-4-6"
    # A caller-passed claude id wins (no substitution).
    assert routed_model("claude-opus") == "claude-opus"


def test_routed_model_no_substitution_for_openrouter():
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_routed_model_preserves_codex_model_id(monkeypatch):
    set_active_backend("codex-cli")
    monkeypatch.setenv(backends.MODEL_ENV, "gpt-5.6-sol")
    assert routed_model("gpt-5.6-luna") == "gpt-5.6-luna"
    assert routed_model("luna") == "luna"


def test_opencode_backend_has_constant_name_and_model_specific_cache_keys():
    """The provider/model id, not a registry entry id, separates cache entries."""
    first = OpencodeCliBackend()
    second = OpencodeCliBackend()
    assert first.name == second.name == "opencode-cli"
    assert first.filesystem_posture == "unconfined"

    one = build_cache_key(
        backend=first.name, model="openai/gpt-5", system="s", user="u"
    )
    other = build_cache_key(
        backend=first.name, model="anthropic/claude-sonnet-4-6", system="s", user="u"
    )
    assert one != other


def test_routed_model_preserves_opencode_provider_model_and_honors_override(monkeypatch):
    set_active_backend("opencode-cli")
    assert routed_model("openai/gpt-5") == "openai/gpt-5"
    monkeypatch.setenv(backends.MODEL_ENV, "anthropic/claude-sonnet-4-6")
    assert routed_model("openai/gpt-5") == "anthropic/claude-sonnet-4-6"


def test_opencode_backend_delegates_lazily_without_running_opencode(monkeypatch):
    """The adapter test seam needs neither the CLI nor a live model."""
    import sys
    import types

    built = []

    class _Delegate:
        def __init__(self, **kwargs):
            built.append(kwargs)

    package = types.ModuleType("llm_scripting_kit")
    completion = types.ModuleType("llm_scripting_kit.completion")
    completion.OpencodeCliBackend = _Delegate
    package.completion = completion
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", completion)

    runner = object()
    backend = OpencodeCliBackend(
        default_timeout_s=17.0, argv_prefix=("opencode",), runner=runner
    )
    assert backend._backend() is not None
    assert built == [
        {
            "default_timeout_s": 17.0,
            "argv_prefix": ("opencode",),
            "runner": runner,
        }
    ]


def test_mock_backend_exhaustion_race_raises_documented_error():
    """Threads racing the LAST scripted entry get RuntimeError, not IndexError.

    Regression: ``complete`` used to do an unguarded ``if not self._queue`` /
    ``self._queue.pop(0)``. Both threads could pass the emptiness check and
    the loser popped an empty list -- an IndexError leaking out of the mock
    instead of the documented "MockBackend exhausted" RuntimeError. Consumers
    share one backend across a ThreadPoolExecutor stage, so this is the shape
    that runs in real pipelines.

    It hammers the boundary directly (queue of one, many threads released
    together) across many rounds at a minimal switch interval.

    HONESTY NOTE: this is an INVARIANT test, not a proven regression test.
    It was run against the pre-fix code and PASSED there too -- the window is
    one bytecode wide and CPython's GIL makes the individual list ops
    effectively atomic, so the race is real by inspection but not practically
    reachable on this interpreter. It is kept because it pins the CONTRACT
    (the entry is served exactly once; exhaustion raises the documented
    RuntimeError, never IndexError), which WOULD catch a future change that
    genuinely breaks atomicity: a swap to a non-atomic container, any I/O
    added between the check and the pop, or a free-threaded (no-GIL) build.
    Do not read a pass here as evidence the lock is unnecessary.
    """
    import sys
    import threading

    n_threads = 16
    rounds = 300
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for _ in range(rounds):
            backend = MockBackend(responses=["only"])
            served: list[str] = []
            exhausted = 0
            unexpected: list[BaseException] = []
            lock = threading.Lock()
            gate = threading.Barrier(n_threads)

            def worker() -> None:
                nonlocal exhausted
                gate.wait()
                try:
                    resp = backend.complete("sys", "user", model="m")
                except RuntimeError:
                    with lock:
                        exhausted += 1
                    return
                except BaseException as exc:  # noqa: BLE001 -- the defect
                    with lock:
                        unexpected.append(exc)
                    return
                with lock:
                    served.append(resp.text)

            threads = [threading.Thread(target=worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not unexpected, (
                f"unguarded check-then-pop leaked {unexpected[0]!r}; "
                "expected the documented RuntimeError"
            )
            assert served == ["only"], f"entry served {len(served)} times"
            assert exhausted == n_threads - 1
    finally:
        sys.setswitchinterval(old_interval)


def test_response_adapter_carries_total_tokens():
    """The response seam must not silently drop a field.

    Its sibling `_to_completion_options` exists so a field drift SURFACES
    rather than mis-binding. The response direction had no such guard, and a
    codex-only `total_tokens` was in fact dropped here -- a codex call routed
    through this layer reported no usage at all.
    """

    class _Resp:
        text = "x"
        model = "gpt-5.6-luna"
        input_tokens = 0
        output_tokens = 0
        cache_hit_tokens = 0
        wall_ms = 5
        attempts = 1
        from_cache = False
        total_tokens = 14214

    adapted = backends._from_completion_response(_Resp())
    assert adapted.total_tokens == 14214
    # An undifferentiated total must NOT masquerade as metered output, which
    # the cost estimator prices per output token.
    assert adapted.output_tokens == 0


def test_response_adapter_carries_truthfulness_fields() -> None:
    class _Resp:
        text = "x"
        model = "m"
        input_tokens = 1
        output_tokens = 2
        cache_hit_tokens = 3
        wall_ms = 5
        attempts = 1
        from_cache = False
        total_tokens = 0
        status = "completed"
        error = None
        dropped_params = ("temperature",)
        forwarded_params = ("extras.top_k",)
        execution_controls_applied = ("allowed-tools",)
        structured = {"answer": "x"}
        started_at = "2026-09-01T12:00:00Z"
        ended_at = "2026-09-01T12:00:05Z"

    adapted = backends._from_completion_response(_Resp())
    assert adapted.status == "completed"
    assert adapted.error is None
    assert adapted.dropped_params == ("temperature",)
    assert adapted.forwarded_params == ("extras.top_k",)
    assert adapted.execution_controls_applied == ("allowed-tools",)
    assert adapted.structured == {"answer": "x"}
    assert adapted.started_at == "2026-09-01T12:00:00Z"
    assert adapted.ended_at == "2026-09-01T12:00:05Z"


def test_response_adapter_tolerates_older_shared_lib():
    """A shared lib reaches every consumer at once with no version pin."""

    class _OldResp:
        text = "x"
        model = "m"
        input_tokens = 1
        output_tokens = 2
        cache_hit_tokens = 0
        wall_ms = 5
        attempts = 1
        from_cache = False

    adapted = backends._from_completion_response(_OldResp())
    assert adapted.total_tokens == 0
    assert adapted.status == "completed"
    assert adapted.error is None
    assert adapted.dropped_params == ()
    assert adapted.forwarded_params == ()
    assert adapted.execution_controls_applied == ()
    assert adapted.structured is None
    assert adapted.started_at is None
    assert adapted.ended_at is None


def test_seam_error_is_normalized_to_data_at_the_boundary():
    """A live call and a cache hit must yield the same shape for `error`.

    The response cache can only hold JSON, so an error object crossing the
    boundary unchanged would read as an object live and as a dict from cache.
    """
    from content_pipeline.llm.backends import _error_to_data

    class _SeamError:
        code = "halt_rate_limit"

        def to_json(self):
            return {"code": "halt_rate_limit", "message": "hit your limit"}

    assert _error_to_data(None) is None
    assert _error_to_data(_SeamError()) == {
        "code": "halt_rate_limit", "message": "hit your limit"
    }
    # an older shared lib without the type is passed through untouched
    assert _error_to_data("legacy") == "legacy"


# --- CONTENT_PIPELINE_LLM_MODELS declaration (step 10, C1/C2) ---------------


def _install_fake_declaration(monkeypatch, *, default=None, raises=None):
    """Install a fake ``llm_scripting_kit.declaration`` module.

    ``default`` is the ``EntryState``-shaped object ``describe()`` returns as
    ``Ranking.default``; ``raises`` is an exception instance ``describe()``
    raises instead. Mirrors the existing ``_install_counting_completion``
    sys.modules-injection pattern in this file.
    """
    import sys
    import types

    class _NoUsableRoutingTarget(Exception):
        pass

    def _describe(names, **_kwargs):
        if raises is not None:
            raise raises
        return types.SimpleNamespace(default=default)

    declaration = types.ModuleType("llm_scripting_kit.declaration")
    declaration.describe = _describe
    declaration.run = lambda *a, **kw: None
    declaration.RunRequest = object
    declaration.NoUsableRoutingTarget = _NoUsableRoutingTarget
    declaration.CALLER_PROCESS = "process"

    package = types.ModuleType("llm_scripting_kit")
    package.declaration = declaration
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.declaration", declaration)
    return declaration


def _entry(id, harness=None, model=None, drive=None):
    import types

    return types.SimpleNamespace(
        id=id, harness=harness, model=model, drive=drive or (f"{harness}-cli" if harness else id)
    )


def test_declared_model_names_unset_is_none():
    assert backends.declared_model_names() is None


def test_declared_model_names_parses_comma_list(monkeypatch):
    monkeypatch.setenv(backends.MODELS_ENV, "sol, astra , opus")
    assert backends.declared_model_names() == ["sol", "astra", "opus"]


def test_resolve_declaration_absent_lib_names_itself(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "llm_scripting_kit", raising=False)
    monkeypatch.delitem(sys.modules, "llm_scripting_kit.declaration", raising=False)
    if _has_llm_scripting_kit():  # pragma: no cover - env-dependent
        pytest.skip("llm_scripting_kit importable in this environment")
    with pytest.raises(ImportError, match="llm_scripting_kit"):
        backends.resolve_declaration(["opus"])


def test_resolve_declaration_stale_lib_names_the_floor(monkeypatch):
    import sys
    import types

    stale = types.ModuleType("llm_scripting_kit.declaration")  # no describe/run/etc.
    package = types.ModuleType("llm_scripting_kit")
    package.declaration = stale
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.declaration", stale)
    with pytest.raises(ImportError, match="0.46.0"):
        backends.resolve_declaration(["opus"])


def test_resolve_declaration_returns_the_default_entry(monkeypatch):
    entry = _entry("opus", harness="claude", model="claude-opus-5")
    _install_fake_declaration(monkeypatch, default=entry)
    assert backends.resolve_declaration(["opus"]) is entry


def test_resolve_declaration_propagates_the_floor(monkeypatch):
    floor = RuntimeError("no usable routing target in [sol, astra]")
    _install_fake_declaration(monkeypatch, raises=floor)
    with pytest.raises(RuntimeError, match="no usable routing target"):
        backends.resolve_declaration(["sol", "astra"])


def test_declared_backend_and_model_from_claude_entry(monkeypatch):
    entry = _entry("opus", harness="claude", model="claude-opus-5", drive="claude-cli")
    _install_fake_declaration(monkeypatch, default=entry)
    assert backends.declared_backend_and_model(["opus"]) == ("claude-cli", "claude-opus-5")


def test_route_uses_declared_entry_for_codex_harness(monkeypatch):
    monkeypatch.setenv(backends.MODELS_ENV, "sol")
    entry = _entry("sol", harness="codex", model="gpt-5.6-sol", drive="codex-cli")
    _install_fake_declaration(monkeypatch, default=entry)
    assert isinstance(route(), CodexCliBackend)


def test_route_uses_declared_entry_for_openrouter_transport(monkeypatch):
    monkeypatch.setenv(backends.MODELS_ENV, "openrouter")
    entry = _entry("openrouter", harness=None, model=None, drive="openrouter")
    _install_fake_declaration(monkeypatch, default=entry)
    assert isinstance(route(), OpenRouterBackend)


def test_route_supplied_mock_wins_even_with_models_env_set(monkeypatch):
    """R16-adjacent: the hermetic test seam must never be shadowed by a
    declaration, exactly like it must never be shadowed by BACKEND_ENV."""
    monkeypatch.setenv(backends.MODELS_ENV, "sol")
    mine = MockBackend(responses=["x"])
    assert route(mock=mine) is mine


def test_routed_model_uses_declared_entry_model(monkeypatch):
    monkeypatch.setenv(backends.MODELS_ENV, "sol")
    entry = _entry("sol", harness="codex", model="gpt-5.6-sol", drive="codex-cli")
    _install_fake_declaration(monkeypatch, default=entry)
    assert routed_model("anything", backend_name="codex-cli") == "gpt-5.6-sol"


def test_declared_entry_is_memoized_per_process(monkeypatch):
    """A declaration governs a run; re-probing on every call site is wasted
    work (and, live, a second network/CLI probe). One resolve per (names,
    project_root) key."""
    monkeypatch.setenv(backends.MODELS_ENV, "sol")
    calls = []

    def _describe(names, **_kwargs):
        import types

        calls.append(tuple(names))
        return types.SimpleNamespace(
            default=_entry("sol", harness="codex", model="gpt-5.6-sol", drive="codex-cli")
        )

    import sys
    import types as _types

    declaration = _types.ModuleType("llm_scripting_kit.declaration")
    declaration.describe = _describe
    declaration.run = lambda *a, **kw: None
    declaration.RunRequest = object
    declaration.NoUsableRoutingTarget = RuntimeError
    declaration.CALLER_PROCESS = "process"
    package = _types.ModuleType("llm_scripting_kit")
    package.declaration = declaration
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.declaration", declaration)

    routed_model("x", backend_name="codex-cli")
    route()
    assert len(calls) == 1


def test_legacy_triple_deprecation_warning_only_when_explicit(monkeypatch):
    """Bare defaults (nothing set) must stay silent; an explicit legacy env
    must warn once it is actually consulted (R37: honoured with a
    deprecation line)."""
    entry = _entry("opus", harness="claude", model="claude-opus-5", drive="claude-cli")
    _install_fake_declaration(monkeypatch, default=entry)

    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        backends._resolve_declared_entry()
    assert not any(issubclass(w.category, DeprecationWarning) for w in caught)

    backends.reset_declared_entry_cache()
    monkeypatch.setenv(backends.BACKEND_ENV, "claude-cli")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        backends._resolve_declared_entry()
    assert any(
        issubclass(w.category, DeprecationWarning) and backends.MODELS_ENV in str(w.message)
        for w in caught
    )


def test_legacy_claude_cli_maps_to_shipped_default_entry(monkeypatch):
    monkeypatch.setenv(backends.BACKEND_ENV, "claude-cli")
    entry_id, explicit = backends._legacy_declaration_name()
    assert entry_id == "opus"
    assert explicit is True


def test_legacy_codex_cli_maps_to_shipped_default_entry(monkeypatch):
    monkeypatch.setenv(backends.BACKEND_ENV, "codex-cli")
    entry_id, explicit = backends._legacy_declaration_name()
    assert entry_id == "sol"
    assert explicit is True


def test_legacy_model_endpoint_maps_to_named_endpoint_exactly(monkeypatch):
    monkeypatch.setenv(backends.BACKEND_ENV, "model-endpoint")
    monkeypatch.setenv(backends.ENDPOINT_ENV, "my-vllm")
    entry_id, explicit = backends._legacy_declaration_name()
    assert entry_id == "my-vllm"
    assert explicit is True


def test_legacy_defaults_map_to_openrouter_and_are_not_explicit():
    entry_id, explicit = backends._legacy_declaration_name()
    assert entry_id == "openrouter"
    assert explicit is False
