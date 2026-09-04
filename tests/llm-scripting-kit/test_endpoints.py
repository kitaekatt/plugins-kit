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
    discover_model_entries,
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

    def test_key_file_defaults_to_none_when_undeclared(self):
        ep = resolve_endpoint("local-vllm", config=CUSTOM_CFG)
        assert ep["key_file"] is None

    def test_key_file_is_passed_through_when_declared(self):
        cfg = {
            "default_endpoint": "openrouter",
            "endpoints": {
                "openrouter": CUSTOM_CFG["endpoints"]["openrouter"],
                "filed": {
                    "base_url": "http://localhost:8001/v1",
                    "key_env": "FILED_KEY",
                    "key_file": "~/creds/filed-key",
                },
            },
        }
        ep = resolve_endpoint("filed", config=cfg)
        assert ep["key_file"] == "~/creds/filed-key"

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


# A config declaring an explicitly KEYLESS endpoint alongside one that merely
# omits key_env -- the pair that pins the deliberate asymmetry.
KEYLESS_CFG = {
    "default_endpoint": "openrouter",
    "endpoints": {
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_API_KEY",
            "account_check": "openrouter",
        },
        "keyless-local": {
            "base_url": "http://localhost:8080/v1",
            "key_env": None,
            "account_check": "models-probe",
            "default": "local",
            "models": {"local": {"slug": "local-27b"}},
        },
        "forgot-key-env": {
            "base_url": "http://localhost:8081/v1",
            "account_check": "models-probe",
        },
    },
    "models": {"gpt-mini": {"slug": "openai/gpt-4o-mini"}},
    "default": "gpt-mini",
}

REGISTRY_YAML = """\
default: alpha
models:
  alpha:
    name: Alpha
    base_url: http://alpha.invalid:8080/v1
    model: alpha-27b
    context_window: 262144
    reasoning_effort: medium
  keyed:
    base_url: https://vendor.invalid/v1
    model: vendor-1
    key_env: VENDOR_API_KEY
"""


@pytest.fixture
def registry_file(tmp_path, monkeypatch):
    """Point the registry at a tmp file and isolate the convention path.

    HOME and USERPROFILE are both redirected so the convention path can never
    reach the developer's real ~/.claude, whichever one expanduser follows.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = tmp_path / "model-endpoints.yaml"
    path.write_text(REGISTRY_YAML, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return path


@pytest.fixture
def no_registry(tmp_path, monkeypatch):
    """No override and an empty convention path -- the not-opted-in state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MODEL_ENDPOINTS_REGISTRY", raising=False)
    return home


class TestKeylessEndpoint:
    def test_explicit_null_key_env_resolves_keyless(self):
        ep = resolve_endpoint("keyless-local", config=KEYLESS_CFG)
        assert ep["key_env"] is None
        assert ep["base_url"] == "http://localhost:8080/v1"

    def test_omitted_key_env_still_raises(self):
        with pytest.raises(EndpointResolveError) as exc:
            resolve_endpoint("forgot-key-env", config=KEYLESS_CFG)
        assert "key_env" in str(exc.value)
        # The message must point at the deliberate opt-in, not just complain.
        assert "key_env: null" in str(exc.value)

    def test_default_endpoint_key_env_unchanged(self):
        ep = resolve_endpoint(config=KEYLESS_CFG)
        assert ep["key_env"] == "OPENROUTER_API_KEY"

    def test_keyless_endpoint_still_needs_a_base_url(self):
        cfg = {"endpoints": {"broken": {"key_env": None}}}
        with pytest.raises(EndpointResolveError, match="base_url"):
            resolve_endpoint("broken", config=cfg)

    def test_validate_endpoint_sends_no_authorization_when_keyless(self):
        ep = resolve_endpoint("keyless-local", config=KEYLESS_CFG)
        with patch("urllib.request.urlopen") as mock_open:
            resp = mock_open.return_value.__enter__.return_value
            resp.read.return_value = b"{}"
            status = validate_endpoint(ep, "")
        assert status.ok is True
        req = mock_open.call_args[0][0]
        assert "Authorization" not in req.headers
        assert req.full_url == "http://localhost:8080/v1/models"


class TestRegistryEndpoints:
    def test_entry_resolves_as_an_endpoint_by_id(self, registry_file):
        ep = resolve_endpoint("alpha", config=CUSTOM_CFG)
        assert ep["name"] == "alpha"
        assert ep["base_url"] == "http://alpha.invalid:8080/v1"
        assert ep["key_env"] is None  # keyless by default
        assert ep["account_check"] == "models-probe"
        assert ep["request_defaults"] == {"reasoning_effort": "medium"}
        assert ep["context_window"] == 262144

    def test_keyed_entry_carries_its_key_env(self, registry_file):
        ep = resolve_endpoint("keyed", config=CUSTOM_CFG)
        assert ep["key_env"] == "VENDOR_API_KEY"
        assert ep["request_defaults"] == {}
        assert ep["context_window"] is None

    def test_resolve_model_yields_the_entry_model(self, registry_file):
        assert resolve_model(None, config=CUSTOM_CFG, endpoint="alpha") == "alpha-27b"

    def test_config_endpoint_shadows_a_registry_entry(self, registry_file):
        shadowing = dict(CUSTOM_CFG)
        shadowing["endpoints"] = dict(CUSTOM_CFG["endpoints"])
        shadowing["endpoints"]["alpha"] = {
            "base_url": "http://shadow.invalid/v1",
            "key_env": "SHADOW_KEY",
        }
        ep = resolve_endpoint("alpha", config=shadowing)
        assert ep["base_url"] == "http://shadow.invalid/v1"
        assert ep["key_env"] == "SHADOW_KEY"

    def test_registry_harness_entry_is_refused_by_openai_resolution(self, registry_file):
        registry_file.write_text(
            "models:\n  sol:\n    harness: codex\n    model: gpt-5.6-sol\n",
            encoding="utf-8",
        )
        from llm_scripting_kit import models

        with pytest.raises(EndpointResolveError) as exc:
            models._registry_endpoint("sol")
        msg = str(exc.value)
        assert "sol" in msg
        assert "harness" in msg
        assert "codex" in msg

        with pytest.raises(EndpointResolveError) as resolve_exc:
            resolve_endpoint("sol", config={"endpoints": {}})
        assert "harness" in str(resolve_exc.value)

    def test_config_harness_entry_is_refused_by_openai_resolution(self):
        config = {"endpoints": {"sol": {"harness": "codex", "model": "gpt-5.6-sol"}}}
        with pytest.raises(EndpointResolveError) as exc:
            resolve_endpoint("sol", config=config)
        msg = str(exc.value)
        assert "sol" in msg
        assert "harness" in msg
        assert "codex" in msg

    def test_discovery_merges_config_and_registry_with_cross_kind_shadowing(self):
        from llm_scripting_kit.model_endpoints import EndpointEntry, EndpointRegistry

        registry = EndpointRegistry(
            entries={
                "transport-shadow": EndpointEntry(
                    "transport-shadow", "http://registry.invalid/v1", "registry-model"
                ),
                "harness-shadow": EndpointEntry(
                    "harness-shadow",
                    None,
                    "registry-model",
                    kind="harness",
                    harness="codex",
                ),
                "registry-only": EndpointEntry(
                    "registry-only", "http://registry.invalid/v1", "registry-only-model"
                ),
            },
            notes=["registry entry 'mystery' skipped; unknown kind"],
        )
        config = {
            "endpoints": {
                "transport-shadow": {
                    "base_url": "http://config.invalid/v1",
                    "model": "config-transport-model",
                },
                "harness-shadow": {
                    "harness": "opencode",
                    "model": "config-harness-model",
                    "effort": "medium",
                },
                "config-only": {
                    "harness": "codex",
                    "model": "config-only-model",
                },
            }
        }

        discovered = discover_model_entries(config=config, registry=registry)

        assert discovered.entries is discovered
        assert discovered.notes == registry.notes
        assert discovered["transport-shadow"].kind == "transport"
        assert discovered["transport-shadow"].model == "config-transport-model"
        assert discovered["harness-shadow"].kind == "harness"
        assert discovered["harness-shadow"].harness == "opencode"
        assert discovered["harness-shadow"].model == "config-harness-model"
        assert discovered["harness-shadow"].effort == "medium"
        assert discovered["config-only"].kind == "harness"
        assert discovered["registry-only"].kind == "transport"

    def test_unknown_name_with_no_registry_raises_as_before(self, no_registry):
        with pytest.raises(EndpointResolveError, match="unknown endpoint 'nope'"):
            resolve_endpoint("nope", config=CUSTOM_CFG)

    def test_unknown_name_with_a_registry_raises_as_before(self, registry_file):
        with pytest.raises(EndpointResolveError, match="unknown endpoint 'nope'"):
            resolve_endpoint("nope", config=CUSTOM_CFG)

    def test_unreadable_registry_is_loud_not_silent(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        broken = tmp_path / "broken.yaml"
        broken.write_text("models: [unclosed\n", encoding="utf-8")
        monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(broken))
        with pytest.raises(EndpointResolveError) as exc:
            resolve_endpoint("alpha", config=CUSTOM_CFG)
        assert "broken.yaml" in str(exc.value)

    def test_default_endpoint_never_consults_the_registry(self, registry_file):
        ep = resolve_endpoint(config=LEGACY_CFG)
        assert ep["name"] == "openrouter"
        assert ep["key_env"] == "OPENROUTER_API_KEY"


class TestUnknownEndpointNamesBothNamespaces:
    """The unknown-endpoint error must list registry entry ids too.

    An endpoint name may come from the config's `endpoints` map OR from the
    model-endpoints registry, and resolve_endpoint tries both. Listing only the
    config's is misleading in exactly the case the reader is in: a typo'd
    registry entry id was told the known set is "openrouter", which points the
    fix at the wrong file. (Same config-vs-registry confusion that produced a
    live defect in content-pipeline-kit's model-endpoint backend.)
    """

    def test_registry_entries_appear_in_the_known_list(self, monkeypatch):
        import sys
        import types

        from llm_scripting_kit import models

        fake = types.ModuleType("llm_scripting_kit.model_endpoints")
        fake.EndpointRegistryError = RuntimeError
        fake.load_endpoint_registry = lambda *a, **k: types.SimpleNamespace(
            entries={"qwen38": object(), "m5": object()}, default_id="qwen38"
        )
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.model_endpoints", fake)
        monkeypatch.setattr(models, "_registry_endpoint", lambda name: None)

        with pytest.raises(EndpointResolveError) as e:
            models.resolve_endpoint("qwen39", config={"endpoints": {"openrouter": {}}})

        msg = str(e.value)
        assert "qwen38" in msg and "m5" in msg, msg
        assert "openrouter" in msg, "config endpoints must still be listed"

    def test_an_unreadable_registry_does_not_replace_the_real_error(
        self, monkeypatch
    ):
        """The diagnostic helper must never raise over the caller's own error."""
        import sys
        import types

        from llm_scripting_kit import models

        fake = types.ModuleType("llm_scripting_kit.model_endpoints")
        fake.EndpointRegistryError = RuntimeError

        def boom(*a, **k):
            raise RuntimeError("registry is broken")

        fake.load_endpoint_registry = boom
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.model_endpoints", fake)
        monkeypatch.setattr(models, "_registry_endpoint", lambda name: None)

        with pytest.raises(EndpointResolveError, match="unknown endpoint 'nope'"):
            models.resolve_endpoint("nope", config={"endpoints": {"openrouter": {}}})


class TestDirectModelEntryPredicate:
    """The three direct-model decisions must hang off ONE predicate.

    An endpoint carrying both a `model` string and a nested `models:` alias map
    is the shape that catches a split predicate: choose the alias map but force
    the selectors to the endpoint id, and resolve_model() looks up an id that
    map cannot contain.
    """

    MIXED_CFG = {
        "endpoints": {
            "mixed": {
                "base_url": "http://mixed.invalid/v1",
                "key_env": None,
                "model": "ignored/direct-slug",
                "models": {"alias": {"slug": "vendor/aliased-slug"}},
                "default": "alias",
            }
        },
        "models": {},
        "default": None,
    }

    DIRECT_CFG = {
        "endpoints": {
            "direct": {
                "base_url": "http://direct.invalid/v1",
                "key_env": None,
                "model": "vendor/direct-slug",
            }
        },
        "models": {},
        "default": None,
    }

    def test_nested_alias_map_wins_and_selectors_stay_consistent(self):
        ep = resolve_endpoint("mixed", config=self.MIXED_CFG)
        assert ep["default"] in ep["models"]
        assert resolve_model(config=self.MIXED_CFG, endpoint="mixed") == (
            "vendor/aliased-slug"
        )

    def test_direct_model_entry_supplies_its_own_map_and_selectors(self):
        ep = resolve_endpoint("direct", config=self.DIRECT_CFG)
        assert ep["models"] == {"direct": {"slug": "vendor/direct-slug"}}
        assert ep["default"] == "direct"
        assert ep["defaultCheap"] == "direct"
        assert resolve_model(config=self.DIRECT_CFG, endpoint="direct") == (
            "vendor/direct-slug"
        )


class TestFleetConfigLayer:
    """The tracked ~/.claude/config layer, below every machine-local one."""

    def test_the_fleet_layer_is_read(self, tmp_path, monkeypatch):
        from pathlib import Path

        from llm_scripting_kit.models import fleet_config_path, load_model_config

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        fleet = fleet_config_path()
        fleet.parent.mkdir(parents=True, exist_ok=True)
        fleet.write_text(
            "endpoints:\n  fable:\n    conserve_usage:\n      pool: seven_day\n"
        )
        cfg = load_model_config()
        # Deep-merged onto the shipped entry: the opt-in arrives, and the
        # shipped harness/model are still there rather than replaced.
        assert cfg["endpoints"]["fable"]["conserve_usage"] == {"pool": "seven_day"}
        assert cfg["endpoints"]["fable"]["harness"] == "claude"
        assert cfg["endpoints"]["fable"]["model"] == "claude-fable-5"

    def test_the_machine_layer_outranks_the_fleet_layer(self, tmp_path, monkeypatch):
        # Same reasoning as settings.local.json over settings.json: a
        # machine-specific answer beats the fleet-wide one.
        from pathlib import Path

        from llm_scripting_kit.models import fleet_config_path, load_model_config

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        fleet = fleet_config_path()
        fleet.parent.mkdir(parents=True, exist_ok=True)
        fleet.write_text("endpoints:\n  fable:\n    conserve_usage:\n      pool: seven_day\n")
        machine = (
            tmp_path / ".claude" / "plugins" / "data" / "plugins-kit"
            / "llm-scripting-kit" / "config.yaml"
        )
        machine.parent.mkdir(parents=True, exist_ok=True)
        machine.write_text("endpoints:\n  fable:\n    conserve_usage: false\n")
        cfg = load_model_config()
        assert cfg["endpoints"]["fable"]["conserve_usage"] is False

    def test_an_absent_fleet_layer_is_the_normal_state(self, tmp_path, monkeypatch):
        from pathlib import Path

        from llm_scripting_kit.models import load_model_config

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        cfg = load_model_config()
        assert "conserve_usage" not in cfg["endpoints"]["fable"]
