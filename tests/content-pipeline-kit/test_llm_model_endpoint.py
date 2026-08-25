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
    ENDPOINT_ENV,
    ModelEndpointBackend,
    route,
    routed_model,
    set_active_backend,
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
    monkeypatch.delenv(ENDPOINT_ENV, raising=False)
    set_active_backend(None)
    yield
    set_active_backend(None)


# --- identity ---------------------------------------------------------------


def test_name_is_constant_so_cache_keys_stay_stable():
    """The cache key is (backend name, model id, ...) -- the name must not vary
    per entry, or every registry edit would silently invalidate the cache."""
    assert ModelEndpointBackend(endpoint="a").name == "model-endpoint"
    assert ModelEndpointBackend(endpoint="b").name == "model-endpoint"


def test_endpoint_defaults_from_env(monkeypatch):
    monkeypatch.setenv(ENDPOINT_ENV, "  qwen38  ")
    assert ModelEndpointBackend().endpoint == "qwen38"


def test_empty_endpoint_resolves_through_the_registry_not_the_config(monkeypatch):
    """Regression: an unset endpoint must resolve via the model-endpoints
    REGISTRY's own `default:`, never be passed onward as None.

    None reaches resolve_endpoint, whose default is the llm-scripting-kit
    config's default endpoint -- `openrouter` -- not this registry's. Live, that
    made an unset CONTENT_PIPELINE_LLM_ENDPOINT probe OpenRouter and report
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


def test_constructs_without_the_shared_lib():
    """Constructing must never require llm_scripting_kit -- only driving it."""
    assert ModelEndpointBackend(endpoint="x") is not None


# --- reasoning-effort precedence --------------------------------------------


def _capturing(monkeypatch, backend, entry_effort):
    seen = {}

    class _Delegate:
        def complete(self, system, user, *, model, options):
            seen["extras"] = dict(getattr(options, "extras", {}) or {})

            class R:
                text, model_id, usage = "ok", model, None
                model_name = model
                input_tokens = output_tokens = 0
                cost = 0.0
                raw = {}

            return R()

    monkeypatch.setattr(backend, "_backend", lambda: _Delegate())
    monkeypatch.setattr(backend, "_entry_reasoning_effort", lambda: entry_effort)
    monkeypatch.setattr(backends, "_from_completion_response", lambda r: r)
    monkeypatch.setattr(backends, "_to_completion_options", lambda o: o)
    return seen


def test_entry_default_supplies_effort_when_caller_says_nothing(monkeypatch):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m")
    assert seen["extras"]["reasoning_effort"] == "medium"


def test_caller_effort_beats_the_entry_default(monkeypatch):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m",
               options=BackendOptions(extras={"reasoning_effort": "xhigh"}))
    assert seen["extras"]["reasoning_effort"] == "xhigh"


def test_explicit_none_suppresses_the_parameter_entirely(monkeypatch):
    """An explicit None is a caller OPT-OUT -- the server's own default wins.

    Distinct from omitting the key, which takes the entry default instead."""
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, "medium")
    b.complete("s", "u", model="m",
               options=BackendOptions(extras={"reasoning_effort": None}))
    assert "reasoning_effort" not in seen["extras"]


def test_no_entry_default_sends_nothing(monkeypatch):
    b = ModelEndpointBackend(endpoint="qwen38")
    seen = _capturing(monkeypatch, b, None)
    b.complete("s", "u", model="m")
    assert "reasoning_effort" not in seen["extras"]


def test_caller_extras_are_not_mutated(monkeypatch):
    """The adapter copies extras -- a caller's dict must survive the call."""
    b = ModelEndpointBackend(endpoint="qwen38")
    _capturing(monkeypatch, b, "medium")
    mine = {}
    b.complete("s", "u", model="m", options=BackendOptions(extras=mine))
    assert mine == {}


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


def test_route_returns_the_backend_when_the_endpoint_is_up(monkeypatch):
    set_active_backend("model-endpoint")
    b = ModelEndpointBackend(endpoint="qwen38")
    monkeypatch.setattr(b, "probe", lambda **k: _Probe(True))
    assert route(model_endpoint=b) is b


def test_route_refuses_when_the_endpoint_is_down(monkeypatch):
    set_active_backend("model-endpoint")
    b = ModelEndpointBackend(endpoint="qwen38")
    monkeypatch.setattr(
        b, "probe", lambda **k: _Probe(False, detail="connection refused")
    )
    with pytest.raises(LLMUnavailableError) as e:
        route(model_endpoint=b)
    msg = str(e.value)
    assert "qwen38" in msg and "connection refused" in msg
    # The remedy names env vars the consumer controls, never a file in our tree.
    assert ENDPOINT_ENV in msg and backends.BACKEND_ENV in msg


def test_injected_client_skips_the_probe(monkeypatch):
    """The hermetic test seam: an injected client is the caller's affair, and
    probing it would put every such test back on the network."""
    set_active_backend("model-endpoint")

    def explode(**k):  # pragma: no cover - must never run
        raise AssertionError("probe ran despite an injected client")

    b = ModelEndpointBackend(endpoint="qwen38", client=object())
    monkeypatch.setattr(b, "probe", explode)
    assert route(model_endpoint=b) is b


def test_a_supplied_mock_still_wins_over_this_backend():
    """route()'s unconditional mock seam must not regress -- the probe must not
    run when a mock is supplied, whatever backend is selected."""
    set_active_backend("model-endpoint")
    mine = backends.MockBackend(responses=["x"])
    assert route(mock=mine) is mine


# --- routed_model() ---------------------------------------------------------


def test_routed_model_prefers_the_pipeline_override(monkeypatch):
    set_active_backend("model-endpoint")
    monkeypatch.setenv(backends.MODEL_ENV, "explicit/override")
    assert routed_model("deepseek/deepseek-v4") == "explicit/override"


def test_routed_model_falls_back_truthfully(monkeypatch):
    """An unresolvable registry must not invent a model id -- the requested one
    is returned so the audit record stays honest."""
    set_active_backend("model-endpoint")
    monkeypatch.delenv(backends.MODEL_ENV, raising=False)
    monkeypatch.setenv(ENDPOINT_ENV, "nonexistent-entry-xyz")
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"


def test_other_backends_are_unaffected():
    """The openrouter cache-key path must be byte-identical to before."""
    set_active_backend(None)
    assert routed_model("deepseek/deepseek-v4") == "deepseek/deepseek-v4"
