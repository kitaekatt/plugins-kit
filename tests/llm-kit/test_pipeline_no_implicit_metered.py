"""L3: metered backends require an explicit declaration or configured default."""

from __future__ import annotations

import copy

import pytest


def test_pipeline_without_declaration_or_configured_default_fails_before_call(
    monkeypatch
):
    from llm_kit.pipeline.runtime import backends
    from llm_kit.registry import models

    assert "default_endpoint" not in models.DEFAULT_MODEL_CONFIG
    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    monkeypatch.setattr(models, "load_model_config", lambda project_root=None: {
        **copy.deepcopy(models.DEFAULT_MODEL_CONFIG),
        "default_endpoint": None,
    })
    calls = []

    def complete(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(backends.OpenRouterBackend, "complete", complete)
    with pytest.raises(backends.platform.ConfigurationError, match="default_endpoint"):
        backends.route()
    assert calls == []


def test_pipeline_uses_deliberately_configured_default(monkeypatch):
    from llm_kit.pipeline.runtime import backends
    from llm_kit.registry import models

    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    config = copy.deepcopy(models.DEFAULT_MODEL_CONFIG)
    config["default_endpoint"] = "haiku"
    monkeypatch.setattr(models, "load_model_config", lambda project_root=None: config)
    declaration = backends._declaration_module()
    from llm_kit.dispatch.reachability import Reachability, STATUS_REACHABLE

    monkeypatch.setattr(
        declaration,
        "check_many",
        lambda entries, **kwargs: {
            name: Reachability(STATUS_REACHABLE, "test", "ok")
            for name in entries
        },
    )

    assert isinstance(backends.route(), backends.ClaudeCliBackend)


def test_explicitly_supplied_backend_still_works_without_a_default(monkeypatch):
    from llm_kit.pipeline.runtime import backends
    from llm_kit.registry import models

    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    monkeypatch.setattr(models, "load_model_config", lambda project_root=None: {
        **copy.deepcopy(models.DEFAULT_MODEL_CONFIG),
        "default_endpoint": None,
    })
    supplied = backends.OpenRouterBackend()

    assert backends.route(openrouter=supplied) is supplied


def _no_default(monkeypatch):
    from llm_kit.pipeline.runtime import backends
    from llm_kit.registry import models

    monkeypatch.delenv(backends.MODELS_ENV, raising=False)
    monkeypatch.setattr(models, "load_model_config", lambda project_root=None: {
        **copy.deepcopy(models.DEFAULT_MODEL_CONFIG),
        "default_endpoint": None,
    })
    return backends


def test_routed_model_for_supplied_backend_works_without_a_declaration(monkeypatch):
    backends = _no_default(monkeypatch)
    supplied = backends.OpenRouterBackend()

    assert backends.route(openrouter=supplied) is supplied
    assert backends.routed_model("vendor/model-x", openrouter=supplied) == "vendor/model-x"


def test_routed_model_without_declaration_or_backend_still_fails(monkeypatch):
    backends = _no_default(monkeypatch)

    with pytest.raises(backends.platform.ConfigurationError, match="default_endpoint"):
        backends.routed_model("vendor/model-x")


def test_routed_model_for_injected_mock_works_without_a_declaration(monkeypatch):
    backends = _no_default(monkeypatch)
    mock = backends.MockBackend()

    assert backends.route(mock=mock) is mock
    assert backends.routed_model("slug/model", mock=mock) == "slug/model"
