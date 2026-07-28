"""Tests for named-endpoint resolution and back-compat of the default endpoint.

Covers: pre-endpoints (top-level-only) config resolving the default openrouter
endpoint unchanged; a custom endpoint resolving its own base_url / key_env /
models; account_check 'none' skipping validation.
"""

import io
import urllib.error
from unittest.mock import patch

import pytest

from llm_scripting_kit import (
    EndpointResolveError,
    KeyLookupResult,
    ModelResolveError,
    resolve_endpoint,
    resolve_model,
    validate_endpoint,
)
from llm_scripting_kit import account as account_mod
from llm_scripting_kit import api_key as api_key_mod
from llm_scripting_kit.env_file import write_env_file


# A pre-endpoints user config: top-level registry only, no `endpoints` map.
LEGACY_CFG = {
    "models": {"qwen": {"slug": "qwen/qwen3-32b"}, "mini": {"slug": "openai/gpt-4o-mini"}},
    "default": "mini",
    "defaultCheap": "qwen",
}

# A config that adds a custom OpenAI-compatible endpoint alongside openrouter.
CUSTOM_CFG = {
    "default_endpoint": "openrouter",
    "endpoints": {
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
            "account_check": "openrouter",
        },
        "local-vllm": {
            "base_url": "http://localhost:8000/v1",
            "key_env": "MY_VLLM_KEY",
            "account_check": "none",
            "default": "llama",
            "models": {"llama": {"slug": "meta-llama/Llama-3.1-8B-Instruct"}},
        },
    },
    "models": {"qwen": {"slug": "qwen/qwen3-32b"}, "gpt-mini": {"slug": "openai/gpt-4o-mini"}},
    "default": "gpt-mini",
    "defaultCheap": "qwen",
}


class TestDefaultEndpointBackCompat:
    def test_legacy_config_resolves_default_endpoint(self):
        ep = resolve_endpoint(config=LEGACY_CFG)
        assert ep["name"] == "openrouter"
        assert ep["base_url"] == "https://openrouter.ai/api/v1"
        assert ep["key_env"] == "OPENROUTER_API_KEY"
        assert ep["account_check"] == "openrouter"
        # inherits the top-level registry + selectors
        assert ep["default"] == "mini"
        assert ep["defaultCheap"] == "qwen"

    def test_resolve_model_default_endpoint_unchanged(self):
        assert resolve_model(config=LEGACY_CFG) == "openai/gpt-4o-mini"
        assert resolve_model(cheap=True, config=LEGACY_CFG) == "qwen/qwen3-32b"
        assert resolve_model("qwen", config=LEGACY_CFG) == "qwen/qwen3-32b"

    def test_explicit_openrouter_endpoint_equals_default(self):
        assert resolve_model(config=LEGACY_CFG, endpoint="openrouter") == "openai/gpt-4o-mini"


class TestCustomEndpoint:
    def test_resolves_own_base_url_and_key_env(self):
        ep = resolve_endpoint("local-vllm", config=CUSTOM_CFG)
        assert ep["base_url"] == "http://localhost:8000/v1"
        assert ep["key_env"] == "MY_VLLM_KEY"
        assert ep["account_check"] == "none"

    def test_resolves_own_models(self):
        assert (
            resolve_model(config=CUSTOM_CFG, endpoint="local-vllm")
            == "meta-llama/Llama-3.1-8B-Instruct"
        )

    def test_default_endpoint_untouched_by_custom(self):
        assert resolve_model(config=CUSTOM_CFG) == "openai/gpt-4o-mini"

    def test_unknown_endpoint_raises(self):
        with pytest.raises(EndpointResolveError, match="unknown endpoint 'nope'"):
            resolve_endpoint("nope", config=CUSTOM_CFG)

    def test_custom_endpoint_unknown_alias_raises(self):
        with pytest.raises(ModelResolveError):
            resolve_model("gpt-mini", config=CUSTOM_CFG, endpoint="local-vllm")


class TestGetApiKeyPerEndpoint:
    def test_named_endpoint_uses_its_key_env(self, monkeypatch, tmp_path):
        user_env = tmp_path / "user" / ".env"
        monkeypatch.setattr("llm_scripting_kit.constants.USER_ENV_FILE", user_env)
        monkeypatch.setattr("llm_scripting_kit.api_key.USER_ENV_FILE", user_env)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("MY_VLLM_KEY", raising=False)
        # Both endpoints' keys live in the same .env, keyed by their key_env.
        write_env_file(user_env, {"OPENROUTER_API_KEY": "or-key", "MY_VLLM_KEY": "vllm-key"})
        # Patch config resolution so resolve_endpoint uses CUSTOM_CFG.
        monkeypatch.setattr("llm_scripting_kit.models.load_model_config", lambda **kw: CUSTOM_CFG)

        default_key = api_key_mod.get_api_key(project_root=tmp_path / "proj")
        custom_key = api_key_mod.get_api_key(project_root=tmp_path / "proj", endpoint="local-vllm")
        assert default_key.key == "or-key"
        assert custom_key.key == "vllm-key"
        assert custom_key.source == "user"


class TestValidateEndpoint:
    def test_account_check_none_skips(self):
        ep = resolve_endpoint("local-vllm", config=CUSTOM_CFG)
        # No network call should happen; returns None (skipped).
        assert validate_endpoint(ep, "any-key") is None

    def test_openrouter_mode_calls_auth_key(self):
        ep = resolve_endpoint("openrouter", config=CUSTOM_CFG)
        with patch.object(account_mod, "check_account") as mock_check:
            mock_check.return_value = "SENTINEL"
            result = validate_endpoint(ep, "or-key")
        mock_check.assert_called_once()
        _, kwargs = mock_check.call_args
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert result == "SENTINEL"

    def test_models_probe_mode_hits_models_endpoint(self):
        ep = {"base_url": "http://localhost:8000/v1", "account_check": "models-probe"}
        with patch("urllib.request.urlopen") as mock_open:
            resp = mock_open.return_value.__enter__.return_value
            resp.read.return_value = b"{}"
            status = validate_endpoint(ep, "vllm-key")
        assert status.ok is True
        called_url = mock_open.call_args[0][0].full_url
        assert called_url == "http://localhost:8000/v1/models"

    def test_models_probe_401_is_auth_failure(self):
        ep = {"base_url": "http://localhost:8000/v1", "account_check": "models-probe"}
        err = urllib.error.HTTPError(
            url="http://localhost:8000/v1/models", code=401, msg="Unauthorized",
            hdrs=None, fp=io.BytesIO(b""),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status = validate_endpoint(ep, "bad-key")
        assert status.ok is False
        assert status.failure_reason == "auth"
