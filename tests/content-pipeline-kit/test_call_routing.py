"""Per-call model declaration: explicit `models` wins over the env default."""
import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import (
    MODELS_ENV, MockBackend, OpenRouterBackend, declared_model_names,
    reset_declared_entry_cache, route, routed_model,
)
from content_pipeline.llm.platform import ConfigurationError


class _Entry:
    def __init__(self, id, model):
        self.id, self.model, self.harness, self.drive = id, model, "", id


def _decl(seen):
    class _Ranking:
        def __init__(self, names):
            self.default = _Entry("openrouter", "model-for-" + ",".join(names))

    class _Decl:
        CALLER_PROCESS = "process"

        @staticmethod
        def describe(names, project_root=None, caller=None):
            seen.append(list(names))
            return _Ranking(names)

    return _Decl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for n in (MODELS_ENV, *backends.REMOVED_ROUTING_ENVS):
        monkeypatch.delenv(n, raising=False)
    reset_declared_entry_cache()
    yield
    reset_declared_entry_cache()


def test_explicit_models_win_over_env(monkeypatch):
    seen = []
    monkeypatch.setattr(backends, "_declaration_module", lambda: _decl(seen))
    monkeypatch.setenv(MODELS_ENV, "from-env")
    assert declared_model_names() == ["from-env"]
    assert declared_model_names(["a", " b "]) == ["a", "b"]
    assert routed_model("", models=["a", "b"]) == "model-for-a,b"
    assert routed_model("") == "model-for-from-env"
    assert seen == [["a", "b"], ["from-env"]]


def test_two_pipelines_route_differently_in_one_process(monkeypatch):
    seen = []
    monkeypatch.setattr(backends, "_declaration_module", lambda: _decl(seen))
    assert routed_model("", models=["x"]) == "model-for-x"
    assert routed_model("", models=["y"]) == "model-for-y"
    assert routed_model("", models=["x"]) == "model-for-x"
    assert seen == [["x"], ["y"]]  # memoized per declaration


def test_explicit_models_work_without_the_env(monkeypatch):
    monkeypatch.setattr(backends, "_declaration_module", lambda: _decl([]))
    assert type(route(models=["q"])) is OpenRouterBackend


def test_env_remains_the_default(monkeypatch):
    seen = []
    monkeypatch.setattr(backends, "_declaration_module", lambda: _decl(seen))
    monkeypatch.setenv(MODELS_ENV, "e1,e2")
    route()
    assert seen == [["e1", "e2"]]


def test_empty_explicit_list_is_refused():
    with pytest.raises(ConfigurationError):
        declared_model_names([])
    with pytest.raises(ConfigurationError):
        route(models=[" "])


def test_explicit_models_silence_the_removed_env_refusal(monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_BACKEND", "claude-cli")
    monkeypatch.setattr(backends, "_declaration_module", lambda: _decl([]))
    route(models=["q"])  # no ConfigurationError
    with pytest.raises(ConfigurationError):
        route()


def test_mock_still_wins_unconditionally():
    m = MockBackend(responses=["x"])
    assert route(mock=m, models=["q"]) is m
