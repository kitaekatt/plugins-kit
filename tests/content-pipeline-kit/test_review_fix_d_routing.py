"""A removed routing env var is a loud configuration error, not a silent fallback.

CONTENT_PIPELINE_LLM_BACKEND / _MODEL / _ENDPOINT no longer route anything.
Ignoring them silently sent a consumer that had chosen a subscription backend
to metered OpenRouter (and changed every cache key). Routing now refuses when
one of them is set and CONTENT_PIPELINE_LLM_MODELS is not.
"""

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    MODELS_ENV,
    MockBackend,
    ModelEndpointBackend,
    OpenRouterBackend,
    route,
    routed_model,
)
from content_pipeline.llm.platform import ConfigurationError

REMOVED = (
    "CONTENT_PIPELINE_LLM_BACKEND",
    "CONTENT_PIPELINE_LLM_MODEL",
    "CONTENT_PIPELINE_LLM_ENDPOINT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in REMOVED + (MODELS_ENV,):
        monkeypatch.delenv(name, raising=False)
    backends.reset_declared_entry_cache()
    yield
    backends.reset_declared_entry_cache()


@pytest.fixture
def no_network(monkeypatch):
    """Fail the test if routing reaches the declaration layer or a backend."""

    def _boom(*a, **k):
        raise AssertionError("routing reached the declaration layer")

    monkeypatch.setattr(backends, "_declaration_module", _boom)


# --- refusal: each removed name, both entry points ---------------------------


@pytest.mark.parametrize("name", REMOVED)
def test_route_refuses_a_removed_env_and_names_the_replacement(monkeypatch, no_network, name):
    monkeypatch.setenv(name, "claude-cli")
    with pytest.raises(ConfigurationError) as info:
        route()
    text = str(info.value)
    assert name in text
    assert MODELS_ENV in text


@pytest.mark.parametrize("name", REMOVED)
def test_routed_model_refuses_a_removed_env(monkeypatch, no_network, name):
    monkeypatch.setenv(name, "x")
    with pytest.raises(ConfigurationError, match=MODELS_ENV):
        routed_model("deepseek/deepseek-v4", backend_name="claude-cli")


def test_route_names_every_removed_env_that_is_set(monkeypatch, no_network):
    monkeypatch.setenv(REMOVED[0], "claude-cli")
    monkeypatch.setenv(REMOVED[1], "opus")
    with pytest.raises(ConfigurationError) as info:
        route()
    assert REMOVED[0] in str(info.value) and REMOVED[1] in str(info.value)


def test_route_refuses_even_with_a_supplied_openrouter_instance(monkeypatch):
    """The supplied instance is the default entry; a stale env still means the
    operator asked for something else, so silently serving it is the bug."""
    monkeypatch.setenv(REMOVED[0], "claude-cli")
    with pytest.raises(ConfigurationError):
        route(openrouter=OpenRouterBackend())


def test_configuration_error_is_distinct_from_unavailable():
    from content_pipeline.llm.platform import LLMUnavailableError

    assert not issubclass(ConfigurationError, LLMUnavailableError)


# --- accept cases -------------------------------------------------------------


def test_nothing_set_is_unchanged():
    assert type(route()) is OpenRouterBackend
    assert routed_model("openai/gpt-5", backend_name="openrouter") == "openai/gpt-5"


@pytest.mark.parametrize("name", REMOVED)
@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_removed_env_counts_as_unset(monkeypatch, name, blank):
    monkeypatch.setenv(name, blank)
    assert type(route()) is OpenRouterBackend


def test_only_the_models_env_set_routes_through_the_declaration(monkeypatch):
    monkeypatch.setenv(MODELS_ENV, "openrouter")
    sentinel = object()

    class _Entry:
        id = "openrouter"
        harness = ""
        drive = "openrouter"
        model = "m-declared"

    class _Ranking:
        default = _Entry()

    class _Decl:
        CALLER_PROCESS = "process"
        RunRequest = NoUsableRoutingTarget = object
        describe = staticmethod(lambda names, project_root=None, caller=None: _Ranking())
        run = staticmethod(lambda *a, **k: sentinel)

    monkeypatch.setattr(backends, "_declaration_module", lambda: _Decl)
    assert type(route()) is OpenRouterBackend
    assert routed_model("") == "m-declared"


@pytest.mark.parametrize("name", REMOVED)
def test_models_env_wins_and_the_stale_names_are_ignored(monkeypatch, name):
    """Both set: the operator has migrated and the leftover is inert, so no
    error (and no per-call warning to repeat on every route)."""
    monkeypatch.setenv(name, "claude-cli")
    monkeypatch.setenv(MODELS_ENV, "openrouter")

    class _Entry:
        id = "openrouter"
        harness = ""
        drive = "openrouter"
        model = "m-declared"

    class _Ranking:
        default = _Entry()

    class _Decl:
        CALLER_PROCESS = "process"
        describe = staticmethod(lambda names, project_root=None, caller=None: _Ranking())

    monkeypatch.setattr(backends, "_declaration_module", lambda: _Decl)
    assert type(route()) is OpenRouterBackend
    assert routed_model("") == "m-declared"


@pytest.mark.parametrize("name", REMOVED)
def test_a_supplied_mock_is_not_blocked_by_a_stale_env(monkeypatch, name):
    monkeypatch.setenv(name, "claude-cli")
    mine = MockBackend(responses=["x"])
    assert route(mock=mine) is mine


@pytest.mark.parametrize("name", REMOVED)
def test_constructing_a_backend_in_code_does_not_consult_routing(monkeypatch, name):
    """An explicit backend argument passed in code never goes through route()."""
    monkeypatch.setenv(name, "claude-cli")
    assert ModelEndpointBackend(endpoint="qwen38").endpoint == "qwen38"
    assert OpenRouterBackend().name == "openrouter"
    assert MockBackend(responses=["a"]).complete("s", "u", model="m").text == "a"


# --- injected backend and requested model survive a declaration --------------


def _declare_openrouter(monkeypatch, entry_id="openrouter", model="m-declared", harness=""):
    monkeypatch.setenv(MODELS_ENV, entry_id)

    class _Entry:
        id = entry_id
        drive = entry_id
        model = ""

    _Entry.model = model
    _Entry.harness = harness

    class _Ranking:
        default = _Entry()

    class _Decl:
        CALLER_PROCESS = "process"
        describe = staticmethod(lambda names, project_root=None, caller=None: _Ranking())

    monkeypatch.setattr(backends, "_declaration_module", lambda: _Decl)


def test_injected_openrouter_backend_is_used_and_reused_under_a_declaration(monkeypatch):
    _declare_openrouter(monkeypatch)
    stub = MockBackend(responses=["a", "b"])
    first = route(openrouter=stub)
    second = route(openrouter=stub)
    assert first is stub and second is stub
    assert not isinstance(first, OpenRouterBackend)
    # the stub receives the calls and sees the caller's model, not the declared one
    model = routed_model("caller/model-x")
    assert model == "caller/model-x"
    first.complete("s", "u", model=model)
    second.complete("s", "u", model=routed_model("caller/model-x"))
    assert [c["model"] for c in stub.calls] == ["caller/model-x", "caller/model-x"]


def test_empty_request_defers_to_the_declared_model(monkeypatch):
    _declare_openrouter(monkeypatch)
    assert routed_model("") == "m-declared"


def test_declared_non_openrouter_entry_still_beats_an_injected_instance(monkeypatch):
    _declare_openrouter(monkeypatch, entry_id="sol", model="gpt-5.6-sol", harness="codex")
    stub = MockBackend(responses=["a"])
    assert route(openrouter=stub) is not stub
    assert routed_model("openai/gpt-5") == "gpt-5.6-sol"
