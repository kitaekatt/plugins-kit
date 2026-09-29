"""Behavioral tests for ModelEndpointBackend and its selection-time probe.

Hermetic throughout. The transport is llm_scripting_kit's OpenRouter client and
is covered in tests/llm-scripting-kit; what is specific to THIS adapter is the
policy around it -- the reasoning-effort precedence, the constant cache-key
name, and the fact that route() refuses a dead endpoint before any unit runs
rather than letting a bulk run rediscover it once per call.

Every test either injects a client (which suppresses the probe by contract) or
patches the probe, so nothing here touches the network.
"""

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    ModelEndpointBackend,
    route,
    routed_model,
)
from content_pipeline.llm.platform import (
    HALT_UNREACHABLE,
    BackendOptions,
    LLMUnavailableError,
)


class _Probe:
    def __init__(self, ok, endpoint="qwen38", detail="ok"):
        self.ok, self.endpoint, self.detail, self.base_url = ok, endpoint, detail, None


@pytest.fixture(autouse=True)
def _clean_routing(monkeypatch):
    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    backends.reset_declared_entry_cache()
    yield
    backends.reset_declared_entry_cache()


def _install_fake_declaration(monkeypatch, entry_id):
    """A minimal ``llm_scripting_kit.declaration`` naming a transport entry.

    Mirrors ``test_llm_backends.py``'s injection pattern for the
    ``CONTENT_PIPELINE_LLM_MODELS`` (C1) selection path.
    """
    import sys
    import types

    def _describe(names, **_kwargs):
        return types.SimpleNamespace(
            default=types.SimpleNamespace(id=entry_id, harness=None, model=None, drive=entry_id)
        )

    declaration = types.ModuleType("llm_scripting_kit.declaration")
    declaration.describe = _describe
    declaration.run = lambda *a, **kw: None
    declaration.RunRequest = object
    declaration.NoUsableRoutingTarget = RuntimeError
    declaration.CALLER_PROCESS = "process"
    package = types.ModuleType("llm_scripting_kit")
    package.declaration = declaration
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.declaration", declaration)


# --- identity ---------------------------------------------------------------


def test_name_is_constant_so_cache_keys_stay_stable():
    """The cache key is (backend name, model id, ...) -- the name must not vary
    per entry, or every registry edit would silently invalidate the cache."""
    assert ModelEndpointBackend(endpoint="a").name == "model-endpoint"
    assert ModelEndpointBackend(endpoint="b").name == "model-endpoint"


def test_endpoint_defaults_to_empty_meaning_the_registry_default():
    assert ModelEndpointBackend().endpoint == ""


def test_empty_endpoint_resolves_through_the_registry_not_the_config(monkeypatch):
    """Regression: an unset endpoint must resolve via the model-endpoints
    REGISTRY's own `default:`, never be passed onward as None.

    None reaches resolve_endpoint, whose default is the llm-scripting-kit
    config's default endpoint -- `openrouter` -- not this registry's. Live, that
    made an empty endpoint probe OpenRouter and report
    "no API key resolved", a nonsense diagnosis for a keyless local entry. Every
    other test here injects or patches, so only an end-to-end run caught it.
    """
    import sys, types

    fake = types.ModuleType("llm_scripting_kit.model_endpoints")
    fake.resolve_registry_entry = lambda name=None, **k: types.SimpleNamespace(
        id="the-default-entry", reasoning_effort=None
    )
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.model_endpoints", fake)

    b = ModelEndpointBackend()
    assert b.endpoint == ""
    assert b._entry_id() == "the-default-entry"
    # resolved id is cached onto .endpoint, so probes and cache keys see it
    assert b.endpoint == "the-default-entry"


def test_unreadable_registry_leaves_the_default_unresolved(monkeypatch):
    """An unreadable registry is the probe's failure to report, not a crash."""
    import sys, types

    fake = types.ModuleType("llm_scripting_kit.model_endpoints")

    def boom(name=None, **k):
        raise RuntimeError("no registry")

    fake.resolve_registry_entry = boom
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.model_endpoints", fake)
    assert ModelEndpointBackend()._entry_id() is None


def _make_shared_lib_importable():
    """Make the checked-in shared library available to these seam tests."""
    import sys
    from pathlib import Path

    shared_lib = (
        Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"
    )
    if str(shared_lib) not in sys.path:
        sys.path.insert(0, str(shared_lib))


def _write_registry(tmp_path, text):
    path = tmp_path / "model-endpoints.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_harness_sibling_does_not_break_transport_default(monkeypatch, tmp_path):
    """The upstream tolerant loader keeps a valid harness sibling visible."""
    _make_shared_lib_importable()
    path = _write_registry(
        tmp_path,
        "version: 1\n"
        "default: local\n"
        "models:\n"
        "  local:\n"
        "    base_url: http://local.invalid/v1\n"
        "    model: local-model\n"
        "  opencode:\n"
        "    harness: opencode\n"
        "    model: openai/gpt-5\n",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))

    assert ModelEndpointBackend()._entry_id() == "local"


def test_default_harness_is_refused_by_model_endpoint_backend(monkeypatch, tmp_path):
    """A default harness must name its kind instead of becoming None."""
    _make_shared_lib_importable()
    path = _write_registry(
        tmp_path,
        "version: 1\n"
        "default: opencode\n"
        "models:\n"
        "  opencode:\n"
        "    harness: opencode\n"
        "    model: openai/gpt-5\n",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))

    with pytest.raises(Exception, match="harness entry"):
        ModelEndpointBackend()._entry_id()


def test_explicit_harness_is_refused_as_transport(monkeypatch, tmp_path):
    """The shared endpoint resolver names the kind for an explicit harness."""
    _make_shared_lib_importable()
    path = _write_registry(
        tmp_path,
        "version: 1\n"
        "default: local\n"
        "models:\n"
        "  local:\n"
        "    base_url: http://local.invalid/v1\n"
        "    model: local-model\n"
        "  opencode:\n"
        "    harness: opencode\n"
        "    model: openai/gpt-5\n",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))

    probe = ModelEndpointBackend(endpoint="opencode").probe()
    assert not probe.ok
    assert "harness entry" in probe.detail


def test_constructs_without_the_shared_lib():
    """Constructing must never require llm_scripting_kit -- only driving it."""
    assert ModelEndpointBackend(endpoint="x") is not None


# --- reasoning-effort precedence --------------------------------------------
#
# Two branches, chosen by `backends._effort_seam()`: against an
# llm-scripting-kit exporting `plan_effort` / `resolve_endpoint_profile` the
# delegate places the effort itself (the adapter hands it `effort` and the
# caller's extras untouched); against an older one the adapter injects a
# top-level `extras["reasoning_effort"]` (legacy). Neither branch is left to
# test ordering: every test below pins the one it exercises.


@pytest.fixture
def new_seam():
    """The real shared lib's effort seam (llm-scripting-kit >= 0.54.0)."""
    _make_shared_lib_importable()
    seam = backends._effort_seam()
    assert seam is not None
    return seam


@pytest.fixture
def legacy_seam(monkeypatch):
    """A shared lib predating plan_effort: the legacy injection applies."""
    monkeypatch.setattr(backends, "_effort_seam", lambda: None)


def _capturing(monkeypatch, backend, entry_effort, style=None):
    seen = {}

    class _Delegate:
        def complete(self, system, user, *, model, options):
            seen["effort"] = getattr(options, "effort", None)
            seen["extras"] = dict(getattr(options, "extras", {}) or {})

            class R:
                text, model_id, usage = "ok", model, None
                model_name = model
                input_tokens = output_tokens = 0
                cost = 0.0
                raw = {}

            return R()

    monkeypatch.setattr(backend, "_backend", lambda: _Delegate())
    monkeypatch.setattr(backend, "_entry_reasoning_effort", lambda: (entry_effort, style))
    monkeypatch.setattr(backends, "_from_completion_response", lambda r: r)
    monkeypatch.setattr(backends, "_to_completion_options", lambda o: o)
    return seen


def test_entry_default_is_handed_to_the_delegate_as_effort(monkeypatch, new_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium", "ninfer")
    b.complete("s", "u", model="m")
    assert seen["effort"] == "medium"
    assert seen["extras"] == {}  # the delegate places it; nothing is injected


def test_caller_effort_beats_the_entry_default(monkeypatch, new_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium", "ninfer")
    b.complete("s", "u", model="m", options=BackendOptions(effort="low"))
    assert seen["effort"] == "low"


@pytest.mark.parametrize(
    "extras",
    [
        {"reasoning_effort": "xhigh"},
        {"reasoning_effort": None},
        {"chat_template_kwargs": {"reasoning_effort": None}},
    ],
)
def test_caller_extras_reach_the_delegate_unchanged(monkeypatch, new_seam, extras):
    """Explicit extras -- an explicit None included -- are the delegate's to
    apply, so the adapter forwards them as given rather than pre-applying."""
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium", "ninfer")
    b.complete("s", "u", model="m", options=BackendOptions(extras=extras))
    assert seen["extras"] == extras
    assert seen["effort"] == "medium"


def test_no_entry_default_hands_the_delegate_no_effort(monkeypatch, new_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, None, "ninfer")
    b.complete("s", "u", model="m")
    assert seen["effort"] is None
    assert seen["extras"] == {}


@pytest.mark.parametrize("branch", ["new_seam", "legacy_seam"])
def test_caller_extras_are_not_mutated(monkeypatch, request, branch):
    """The adapter copies extras -- a caller's dict must survive the call."""
    request.getfixturevalue(branch)
    b = ModelEndpointBackend(endpoint="qwen38")
    _capturing(monkeypatch, b, "medium", "chat_template_kwargs")
    mine = {"chat_template_kwargs": {"enable_thinking": True}}
    b.complete("s", "u", model="m", options=BackendOptions(extras=mine))
    b.effective_options(BackendOptions(extras=mine))
    assert mine == {"chat_template_kwargs": {"enable_thinking": True}}


def test_legacy_entry_default_is_injected_top_level(monkeypatch, legacy_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m")
    assert seen["extras"] == {"reasoning_effort": "medium"}


def test_legacy_caller_extras_beat_the_entry_default(monkeypatch, legacy_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m",
               options=BackendOptions(extras={"reasoning_effort": "xhigh"}))
    assert seen["extras"]["reasoning_effort"] == "xhigh"


def test_legacy_explicit_none_suppresses_the_parameter(monkeypatch, legacy_seam):
    """An explicit None is a caller OPT-OUT -- the server's own default wins.

    Distinct from omitting the key, which takes the entry default instead."""
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m",
               options=BackendOptions(extras={"reasoning_effort": None}))
    assert "reasoning_effort" not in seen["extras"]


def test_legacy_no_entry_default_sends_nothing(monkeypatch, legacy_seam):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, None)
    b.complete("s", "u", model="m")
    assert "reasoning_effort" not in seen["extras"]


# --- the import guard -------------------------------------------------------


def test_effort_seam_is_the_shared_libs_own_functions(new_seam):
    from llm_scripting_kit.completion import plan_effort, resolve_endpoint_profile

    assert new_seam == (plan_effort, resolve_endpoint_profile)


def test_effort_seam_is_absent_against_an_older_shared_lib(monkeypatch):
    """A linked llm-scripting-kit predating plan_effort -> legacy, not a crash."""
    import sys
    import types

    older = types.ModuleType("llm_scripting_kit.completion")
    older.BackendOptions = object  # present in every release; the probe ignores it
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", older)
    assert backends._effort_seam() is None


def test_effort_seam_is_absent_without_the_shared_lib(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", None)
    assert backends._effort_seam() is None


# --- wire parity: effective_options().extras IS the delegate's extra_body ---
#
# Drives the REAL llm_scripting_kit OpenRouterBackend (built by the adapter)
# against a fake OpenAI client, over a real registry file with one entry per
# effort-style shape. The cache key is built from effective_options(), so this
# is what makes the key a function of the wire.

_PARITY_REGISTRY = (
    "models:\n"
    "  u3-ninfer:\n    base_url: http://ninfer.invalid/v1\n    model: ninfer-m\n"
    "    reasoning_effort: medium\n"
    "    routing: {group: u3, order: 1, effort_style: ninfer}\n"
    "  u3-ctk:\n    base_url: http://ctk.invalid/v1\n    model: ctk-m\n"
    "    reasoning_effort: medium\n"
    "    routing: {group: u3, order: 2, effort_style: chat_template_kwargs}\n"
    "  u3-frontdoor:\n    base_url: http://fd.invalid/v1\n    model: fd-m\n"
    "    reasoning_effort: medium\n    frontdoor: true\n"
    "  u3-nostyle:\n    base_url: http://plain.invalid/v1\n    model: plain-m\n"
    "    reasoning_effort: medium\n"
    "  u3-nodefault:\n    base_url: http://nodef.invalid/v1\n    model: nodef-m\n"
    "    routing: {group: u3, order: 3, effort_style: ninfer}\n"
)

_PARITY_ENTRIES = ["u3-ninfer", "u3-ctk", "u3-frontdoor", "u3-nostyle", "u3-nodefault"]

_PARITY_OPTIONS = {
    "nothing": {},
    "effort": {"effort": "high"},
    "extras-value": {"extras": {"reasoning_effort": "low"}},
    "extras-null": {"extras": {"reasoning_effort": None}},
    "effort+extras-value": {"effort": "high", "extras": {"reasoning_effort": "low"}},
    "effort+extras-null": {"effort": "high", "extras": {"reasoning_effort": None}},
    "nested-value": {"extras": {"chat_template_kwargs": {"reasoning_effort": "low"}}},
    "nested-null": {"extras": {"chat_template_kwargs": {"reasoning_effort": None,
                                                        "enable_thinking": True}}},
    "both-channels": {"effort": "high", "extras": {
        "reasoning_effort": None, "chat_template_kwargs": {"reasoning_effort": "low"}}},
    "unrelated-extras": {"effort": "high",
                         "extras": {"chat_template_kwargs": {"enable_thinking": True}}},
}


class _FakeCompletions:
    def __init__(self, sink):
        self._sink = sink

    def create(self, **kwargs):
        import types

        self._sink.append(kwargs)
        message = types.SimpleNamespace(content="ok", reasoning_content=None)
        choice = types.SimpleNamespace(message=message, finish_reason="stop")
        usage = types.SimpleNamespace(
            prompt_tokens=1, completion_tokens=1, prompt_tokens_details=None
        )
        return types.SimpleNamespace(choices=[choice], usage=usage)


class _FakeClient:
    def __init__(self):
        import types

        self.sink = []
        self.chat = types.SimpleNamespace(completions=_FakeCompletions(self.sink))


def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


@pytest.fixture
def parity_registry(tmp_path, monkeypatch):
    """A hermetic registry and HOME for the real delegate; returns the project root."""
    _isolate_home(tmp_path, monkeypatch)
    path = _write_registry(tmp_path, _PARITY_REGISTRY)
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return tmp_path


def _wire(entry, project_root, options):
    client = _FakeClient()
    b = ModelEndpointBackend(endpoint=entry, project_root=project_root, client=client)
    b.complete("s", "u", model="test/slug", options=options)
    (sent,) = client.sink
    return b, sent


@pytest.mark.parametrize("case", sorted(_PARITY_OPTIONS))
@pytest.mark.parametrize("entry", _PARITY_ENTRIES)
def test_effective_extras_equal_the_extra_body_on_the_wire(
    parity_registry, new_seam, entry, case
):
    options = BackendOptions(**_PARITY_OPTIONS[case])
    b, sent = _wire(entry, parity_registry, options)
    assert b.effective_options(options).extras == sent.get("extra_body", {})


@pytest.mark.parametrize(
    "entry, expected",
    [
        ("u3-ninfer", {"reasoning_effort": "medium"}),
        ("u3-ctk", {"chat_template_kwargs": {"reasoning_effort": "medium"}}),
        ("u3-frontdoor", {"reasoning_effort": "medium"}),
        ("u3-nostyle", {}),
        ("u3-nodefault", {}),
    ],
)
def test_entry_default_is_sent_in_the_entrys_style(parity_registry, new_seam, entry, expected):
    _b, sent = _wire(entry, parity_registry, BackendOptions())
    assert sent.get("extra_body", {}) == expected


def test_ninfer_translates_high_to_xhigh_on_the_wire(parity_registry, new_seam):
    _b, sent = _wire("u3-ninfer", parity_registry, BackendOptions(effort="high"))
    assert sent["extra_body"] == {"reasoning_effort": "xhigh"}


def test_effective_options_keeps_the_callers_effort(parity_registry, new_seam):
    """Cleared, it would move the key for a byte-identical wire request (a
    caller effort overridden by explicit extras); kept, the key moves only
    where the wire does -- see test_review_fix_d_cache_key.py."""
    b = ModelEndpointBackend(endpoint="u3-ninfer", project_root=parity_registry)
    options = BackendOptions(effort="high", extras={"reasoning_effort": "low"})
    assert b.effective_options(options).effort == "high"
    assert b.effective_options(BackendOptions()).effort is None


def test_legacy_effective_options_inject_top_level_whatever_the_style(
    parity_registry, legacy_seam
):
    _make_shared_lib_importable()  # the legacy registry read is real
    for entry in ("u3-ninfer", "u3-ctk", "u3-frontdoor", "u3-nostyle"):
        b = ModelEndpointBackend(endpoint=entry, project_root=parity_registry)
        assert b.effective_options(BackendOptions()).extras == {"reasoning_effort": "medium"}


# --- harness refusal survives the new seam ----------------------------------


def _harness_registry(tmp_path, monkeypatch, default):
    _isolate_home(tmp_path, monkeypatch)
    path = _write_registry(
        tmp_path,
        "version: 1\n"
        f"default: {default}\n"
        "models:\n"
        "  local:\n"
        "    base_url: http://local.invalid/v1\n"
        "    model: local-model\n"
        "    reasoning_effort: medium\n"
        "  opencode:\n"
        "    harness: opencode\n"
        "    model: openai/gpt-5\n",
    )
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))


@pytest.mark.parametrize("branch", ["new_seam", "legacy_seam"])
def test_default_harness_refusal_reaches_the_cache_key_path(
    monkeypatch, tmp_path, request, branch
):
    """call_llm calls effective_options before the cache lookup; a default
    harness must still be refused by kind there, not keyed as a transport."""
    _make_shared_lib_importable()
    request.getfixturevalue(branch)
    _harness_registry(tmp_path, monkeypatch, "opencode")
    with pytest.raises(Exception, match="harness entry"):
        ModelEndpointBackend(project_root=tmp_path).effective_options(BackendOptions())


def test_explicit_harness_is_refused_when_driven(monkeypatch, tmp_path, new_seam):
    """An explicitly selected harness keys without error (it resolves no
    effort) and is refused by kind by the delegate as soon as it is driven --
    the same two outcomes the legacy path produces."""
    _harness_registry(tmp_path, monkeypatch, "local")
    b = ModelEndpointBackend(endpoint="opencode", project_root=tmp_path)
    assert b.effective_options(BackendOptions()).extras == {}
    with pytest.raises(Exception, match="harness entry"):
        b.complete("s", "u", model="m")


# --- effective_options / cache-key collision --------------------------------


def test_effective_options_exposes_the_entry_default_reasoning_effort(monkeypatch, new_seam):
    """The seam platform.build_cache_key needs: the registry default is
    resolved by the backend, downstream of where a caller would otherwise
    build its cache key -- so the key must be built from THIS, not from the
    caller's raw options."""
    b = ModelEndpointBackend(endpoint="qwen38")
    monkeypatch.setattr(b, "_entry_reasoning_effort", lambda: ("medium", "top-level"))
    resolved = b.effective_options(BackendOptions())
    assert resolved.extras["reasoning_effort"] == "medium"


@pytest.mark.parametrize("branch", ["new_seam", "legacy_seam"])
def test_call_llm_cache_key_distinguishes_entries_by_effective_effort(
    monkeypatch, request, branch
):
    """REFUTES the hypothesis that ``name`` (constant across every entry --
    see test_name_is_constant_so_cache_keys_stay_stable) already keeps two
    entries serving the same model id, with different registry-declared
    reasoning_effort, from colliding on one cache entry. Without the
    effective_options seam in call_llm, a call through entry A's cached
    response would be served back for entry B."""
    from content_pipeline.llm import platform

    request.getfixturevalue(branch)
    a = ModelEndpointBackend(endpoint="entry-a")
    b = ModelEndpointBackend(endpoint="entry-b")
    monkeypatch.setattr(a, "_entry_reasoning_effort", lambda: ("low", "top-level"))
    monkeypatch.setattr(b, "_entry_reasoning_effort", lambda: ("high", "top-level"))
    assert a.name == b.name == "model-endpoint"  # the collision precondition

    key_a = platform.build_cache_key(
        backend=a.name, model="m", system="s", user="u",
        options=a.effective_options(BackendOptions()),
    )
    key_b = platform.build_cache_key(
        backend=b.name, model="m", system="s", user="u",
        options=b.effective_options(BackendOptions()),
    )
    assert key_a != key_b


# --- halt classification ----------------------------------------------------


def test_connection_error_is_a_halt(monkeypatch):
    """A registry server that is not running does not start itself, so every
    later unit would burn its own timeout rediscovering that."""
    b = ModelEndpointBackend(endpoint="qwen38")
    assert b.classify_halt(ConnectionError("refused")) == HALT_UNREACHABLE
    assert b.classify_halt(TimeoutError("timed out")) == HALT_UNREACHABLE


def test_non_connection_error_defers_to_the_delegate(monkeypatch):
    b = ModelEndpointBackend(endpoint="qwen38")

    class _Delegate:
        def classify_halt(self, exc):
            return "delegated"

    monkeypatch.setattr(b, "_backend", lambda: _Delegate())
    assert b.classify_halt(ValueError("nope")) == "delegated"


# --- route(): the selection-time probe --------------------------------------


def test_route_via_declaration_builds_model_endpoint_backend_and_probes_it(monkeypatch):
    """CONTENT_PIPELINE_LLM_MODELS naming a transport registry entry routes
    to a ModelEndpointBackend for that entry id, probed at selection (C1)."""
    monkeypatch.setenv(backends.MODELS_ENV, "qwen38")
    _install_fake_declaration(monkeypatch, "qwen38")
    monkeypatch.setattr(ModelEndpointBackend, "probe", lambda self, **k: _Probe(True))
    result = route()
    assert isinstance(result, ModelEndpointBackend)
    assert result.endpoint == "qwen38"


def test_route_via_declaration_refuses_when_the_endpoint_is_down(monkeypatch):
    monkeypatch.setenv(backends.MODELS_ENV, "qwen38")
    _install_fake_declaration(monkeypatch, "qwen38")
    monkeypatch.setattr(
        ModelEndpointBackend, "probe", lambda self, **k: _Probe(False, detail="connection refused")
    )
    with pytest.raises(LLMUnavailableError) as e:
        route()
    msg = str(e.value)
    assert "qwen38" in msg and "connection refused" in msg
    # The remedy names the env the consumer controls, never a file in our tree.
    assert backends.MODELS_ENV in msg


def test_a_supplied_mock_still_wins_over_this_backend(monkeypatch):
    """route()'s unconditional mock seam must not regress -- the probe must not
    run when a mock is supplied, whatever the declaration names."""
    monkeypatch.setenv(backends.MODELS_ENV, "qwen38")
    mine = backends.MockBackend(responses=["x"])
    assert route(mock=mine) is mine


# --- routed_model() ---------------------------------------------------------


def test_routed_model_falls_back_truthfully(monkeypatch):
    """An unresolvable registry entry must not invent a model id -- the
    requested one is returned so the audit record stays honest."""
    monkeypatch.setattr(ModelEndpointBackend, "_entry_id", lambda self: "nonexistent-entry-xyz")
    assert routed_model("deepseek/deepseek-v4", backend_name="model-endpoint") == "deepseek/deepseek-v4"


def test_other_backends_are_unaffected():
    """The openrouter cache-key path must be byte-identical to before."""
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_routed_model_via_declaration_ignores_an_explicit_request(monkeypatch):
    """When CONTENT_PIPELINE_LLM_MODELS governs, the resolved entry's own
    model is truthful even when a caller (e.g. PlannerPolicy.model, Y1)
    passed a different explicit id -- the declaration is a process-wide fact,
    not a per-call preference."""
    import sys
    import types

    monkeypatch.setenv(backends.MODELS_ENV, "qwen38")

    def _describe(names, **_kwargs):
        return types.SimpleNamespace(
            default=types.SimpleNamespace(
                id="qwen38", harness=None, model="qwen/qwen3-32b", drive="qwen38"
            )
        )

    declaration = types.ModuleType("llm_scripting_kit.declaration")
    declaration.describe = _describe
    declaration.run = lambda *a, **kw: None
    declaration.RunRequest = object
    declaration.NoUsableRoutingTarget = RuntimeError
    declaration.CALLER_PROCESS = "process"
    package = types.ModuleType("llm_scripting_kit")
    package.declaration = declaration
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.declaration", declaration)

    assert routed_model("explicit/override", backend_name="model-endpoint") == "qwen/qwen3-32b"
