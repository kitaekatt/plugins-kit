"""Tests for llm_scripting_kit.client.make_openai_client (I1).

endpoint=None must follow the config's ``default_endpoint`` -- the same
contract resolve_endpoint(None) / resolve_model() already honor -- instead of
hardcoding OpenRouter's BASE_URL. A half-applied default (model resolves to a
local slug while the client still points at OpenRouter) would ship the
OpenRouter key to the wrong host.
"""

import sys
import types

import pytest

from llm_scripting_kit import client as client_mod


class _FakeOpenAI:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def fake_openai(monkeypatch):
    captured = {}

    class _Recording(_FakeOpenAI):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = _Recording
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    return captured


class TestDefaultEndpointFollowsConfiguredDefault:
    CONFIG = {
        "default_endpoint": "local",
        "endpoints": {
            "local": {
                "base_url": "http://localhost:8000/v1",
                "key_env": None,  # keyless, so no key resolution is needed
            },
        },
    }

    def test_endpoint_none_uses_the_configured_default_endpoints_base_url(
        self, monkeypatch, fake_openai
    ):
        monkeypatch.setattr(
            "llm_scripting_kit.models.load_model_config", lambda **kw: self.CONFIG
        )
        client_mod.make_openai_client()
        assert fake_openai["base_url"] == "http://localhost:8000/v1"

    def test_default_openrouter_endpoint_is_unchanged(self, monkeypatch, fake_openai):
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        client_mod.make_openai_client()
        assert fake_openai["base_url"] == client_mod.BASE_URL
        assert fake_openai["api_key"] == "from-env"
