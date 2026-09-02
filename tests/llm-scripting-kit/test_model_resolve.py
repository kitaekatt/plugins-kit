"""Unit tests for llm_scripting_kit.models (registry resolution + layered config)."""

from pathlib import Path

import pytest
import yaml

from llm_scripting_kit import (
    DEFAULT_MODEL_CONFIG,
    ModelResolveError,
    load_model_config,
    resolve_model,
)
from llm_scripting_kit import models as models_mod


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestBaselineSync:
    def test_default_yaml_matches_constant(self):
        """default_config.yaml (bootstrap seed source) must mirror DEFAULT_MODEL_CONFIG."""
        yaml_path = Path(models_mod.__file__).parent / "default_config.yaml"
        on_disk = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert on_disk == DEFAULT_MODEL_CONFIG


class TestResolveModelFromConfig:
    CFG = {
        "models": {
            "qwen": {"slug": "qwen/qwen3-32b"},
            "mini": {"slug": "openai/gpt-4o-mini"},
        },
        "default": "mini",
        "defaultCheap": "qwen",
    }

    def test_alias_resolves_to_slug(self):
        assert resolve_model("qwen", config=self.CFG) == "qwen/qwen3-32b"

    def test_raw_slug_passthrough(self):
        assert resolve_model("anthropic-free/whatever", config=self.CFG) == "anthropic-free/whatever"

    def test_unknown_bare_name_raises(self):
        with pytest.raises(ModelResolveError) as exc:
            resolve_model("nope", config=self.CFG)
        assert "not a known model alias" in str(exc.value)

    def test_default_selector(self):
        assert resolve_model(config=self.CFG) == "openai/gpt-4o-mini"

    def test_cheap_selector(self):
        assert resolve_model(cheap=True, config=self.CFG) == "qwen/qwen3-32b"

    def test_default_may_be_raw_slug(self):
        cfg = {"models": {}, "default": "vendor/model-x"}
        assert resolve_model(config=cfg) == "vendor/model-x"

    def test_missing_selector_raises(self):
        with pytest.raises(ModelResolveError) as exc:
            resolve_model(cheap=True, config={"models": {}, "default": "x"})
        assert "defaultCheap" in str(exc.value)

    def test_alias_without_slug_raises(self):
        cfg = {"models": {"broken": {}}, "default": "broken"}
        with pytest.raises(ModelResolveError) as exc:
            resolve_model(config=cfg)
        assert "no 'slug'" in str(exc.value)


class TestLoadModelConfig:
    def test_baseline_only_when_no_files(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        cfg = load_model_config()
        assert cfg == DEFAULT_MODEL_CONFIG

    def test_user_and_project_layers_merge(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        user_file = (
            tmp_path / ".claude" / "plugins" / "data"
            / "plugins-kit" / "llm-scripting-kit" / "config.yaml"
        )
        proj_file = proj / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "config.yaml"
        _write(user_file, "default: qwen\n")  # override the default selector
        _write(
            proj_file,
            "models: {custom: {slug: foo/bar}}\ndefaultCheap: custom\n",
        )
        cfg = load_model_config(project_root=str(proj))
        # baseline models preserved + project's new model unioned in
        assert set(cfg["models"]) >= {"qwen", "gpt-mini", "gemini-lite", "custom"}
        assert cfg["default"] == "qwen"  # user layer wins over baseline
        assert cfg["defaultCheap"] == "custom"  # project layer wins
        # and resolution honors the merged result
        assert resolve_model(project_root=str(proj)) == "qwen/qwen3-32b"
        assert resolve_model(cheap=True, project_root=str(proj)) == "foo/bar"

    def test_baseline_not_mutated_across_calls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        proj_file = proj / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "config.yaml"
        _write(proj_file, "models: {custom: {slug: foo/bar}}\n")
        load_model_config(project_root=str(proj))
        # the module constant must be unchanged by the merge
        assert "custom" not in DEFAULT_MODEL_CONFIG["models"]

    def test_marketplace_less_project_layer_is_read(self, tmp_path, monkeypatch):
        """A config.yaml at the API key's superseded project path still applies.

        The pre-0.6.6 project key path omitted the ``plugins-kit`` segment, so a
        config.yaml placed at the same shape by analogy was silently ignored.
        It is now read, below the canonical project layer.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        _write(
            proj / ".local-data" / "llm-scripting-kit" / "config.yaml",
            "models: {custom: {slug: foo/bar}}\ndefaultCheap: custom\n",
        )
        cfg = load_model_config(project_root=str(proj))
        assert cfg["models"]["custom"]["slug"] == "foo/bar"
        assert cfg["defaultCheap"] == "custom"

    def test_canonical_project_layer_wins_over_marketplace_less(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        _write(
            proj / ".local-data" / "llm-scripting-kit" / "config.yaml",
            "default: qwen\n",
        )
        _write(
            proj / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "config.yaml",
            "default: gemini-lite\n",
        )
        cfg = load_model_config(project_root=str(proj))
        assert cfg["default"] == "gemini-lite"

    def test_marketplace_less_project_layer_wins_over_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        proj = tmp_path / "proj"
        _write(
            tmp_path / ".claude" / "plugins" / "data"
            / "plugins-kit" / "llm-scripting-kit" / "config.yaml",
            "default: qwen\n",
        )
        _write(
            proj / ".local-data" / "llm-scripting-kit" / "config.yaml",
            "default: gemini-lite\n",
        )
        cfg = load_model_config(project_root=str(proj))
        assert cfg["default"] == "gemini-lite"

    def test_bootstrap_lib_fallback_warns_on_stderr(self, monkeypatch, capsys):
        """When bootstrap_lib is unavailable the baseline fallback must not be
        silent -- the silence is what made the missing shared_lib_imports
        declaration in consumers (workflow-kit) latent."""
        import sys as _sys

        # None in sys.modules makes `from bootstrap_lib.config_resolve import ...`
        # raise ImportError, simulating an unprovisioned venv.
        monkeypatch.setitem(_sys.modules, "bootstrap_lib.config_resolve", None)
        cfg = load_model_config()
        assert cfg == DEFAULT_MODEL_CONFIG
        captured = capsys.readouterr()
        assert "bootstrap_lib unavailable" in captured.err
        assert captured.out == ""

    def test_unreadable_layer_degrades_to_baseline(self, tmp_path, monkeypatch, capsys):
        """A ConfigError from the layer read must degrade to the baseline, not
        propagate. bootstrap_lib is importable but PyYAML is not when the CLI
        runs under the standalone Python -- letting that escape took down every
        subcommand, `set-key` included, leaving no way to fix the machine."""
        from bootstrap_lib import config_resolve

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        user_file = (
            tmp_path / ".claude" / "plugins" / "data"
            / "plugins-kit" / "llm-scripting-kit" / "config.yaml"
        )
        _write(user_file, "default: qwen\n")

        def _boom(_path):
            raise config_resolve.ConfigError("PyYAML is required to read layered config files")

        monkeypatch.setattr(config_resolve, "load_config_layer", _boom)

        cfg = load_model_config()
        assert cfg == DEFAULT_MODEL_CONFIG
        captured = capsys.readouterr()
        assert "cannot read layered config" in captured.err
        assert "PyYAML" in captured.err
        assert captured.out == ""


class TestShippedHarnessEntries:
    """sol and luna ship in the baseline; nothing else in the fleet has to.

    They are codex subscription models, so a fresh machine with codex
    reproduces the routing from shipped defaults. Opencode models are
    deliberately absent -- they name the user's own opencode providers.
    """

    def test_shipped_harness_entries_are_discoverable_by_kind(self):
        from llm_scripting_kit import HARNESS_KIND, discover_model_entries

        found = discover_model_entries(config=DEFAULT_MODEL_CONFIG)
        for entry_id, model, effort in (
            ("sol", "gpt-5.6-sol", "high"),
            ("luna", "gpt-5.6-luna", "high"),
        ):
            entry = found[entry_id]
            assert entry.kind == HARNESS_KIND
            assert entry.harness == "codex"
            assert entry.model == model
            assert entry.effort == effort
            assert entry.base_url is None

    def test_a_shipped_harness_entry_is_refused_as_an_http_endpoint(self):
        from llm_scripting_kit import EndpointResolveError, resolve_endpoint

        with pytest.raises(EndpointResolveError) as excinfo:
            resolve_endpoint("sol", config=DEFAULT_MODEL_CONFIG)
        message = str(excinfo.value)
        assert "harness" in message
        assert "codex" in message
        assert "no 'base_url'" not in message

    def test_no_opencode_entry_ships(self):
        # Shipping one would name a provider from the author's own opencode
        # config, which resolves on no other machine.
        for endpoint in (DEFAULT_MODEL_CONFIG["endpoints"] or {}).values():
            assert endpoint.get("harness") != "opencode"
