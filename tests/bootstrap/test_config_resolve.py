"""Unit tests for bootstrap_lib.config_resolve (runtime layered config)."""

import copy
import sys
from pathlib import PureWindowsPath

import pytest

from bootstrap_lib import config_resolve
from bootstrap_lib.config_resolve import (
    ConfigError,
    default_data_root,
    load_config_layer,
    resolve_plugin_data_dir,
    resolve_config,
    standard_config_layers,
)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadConfigLayer:
    def test_absent_file_returns_none(self, tmp_path):
        assert load_config_layer(tmp_path / "nope.yaml") is None

    def test_empty_file_returns_empty_dict(self, tmp_path):
        p = _write(tmp_path / "c.yaml", "")
        assert load_config_layer(p) == {}

    def test_comment_only_file_returns_empty_dict(self, tmp_path):
        p = _write(tmp_path / "c.yaml", "# just a comment\n")
        assert load_config_layer(p) == {}

    def test_valid_nested_mapping(self, tmp_path):
        p = _write(
            tmp_path / "c.yaml",
            "models:\n  cheap:\n    slug: qwen/qwen3-32b\ndefault: cheap\n",
        )
        assert load_config_layer(p) == {
            "models": {"cheap": {"slug": "qwen/qwen3-32b"}},
            "default": "cheap",
        }

    def test_malformed_yaml_raises(self, tmp_path):
        p = _write(tmp_path / "c.yaml", "key: [unterminated\n")
        with pytest.raises(ConfigError) as exc:
            load_config_layer(p)
        assert "malformed YAML" in str(exc.value)
        assert str(p) in str(exc.value)

    def test_invalid_utf8_raises_config_error(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_bytes(b"key: \xff\n")

        with pytest.raises(ConfigError, match="malformed YAML"):
            load_config_layer(p)

    def test_top_level_list_raises(self, tmp_path):
        p = _write(tmp_path / "c.yaml", "- a\n- b\n")
        with pytest.raises(ConfigError) as exc:
            load_config_layer(p)
        assert "must be a mapping" in str(exc.value)

    def test_top_level_scalar_raises(self, tmp_path):
        p = _write(tmp_path / "c.yaml", "just-a-string\n")
        with pytest.raises(ConfigError):
            load_config_layer(p)

    def test_missing_pyyaml_raises_configerror(self, tmp_path, monkeypatch):
        p = _write(tmp_path / "c.yaml", "default: cheap\n")
        # Setting the module to None makes `import yaml` raise ImportError.
        monkeypatch.setitem(sys.modules, "yaml", None)
        with pytest.raises(ConfigError) as exc:
            load_config_layer(p)
        assert "PyYAML is required" in str(exc.value)


class TestResolveConfig:
    def test_no_layers_returns_empty(self):
        assert resolve_config([]) == {}

    def test_all_absent_returns_empty(self, tmp_path):
        assert resolve_config([tmp_path / "a.yaml", tmp_path / "b.yaml"]) == {}

    def test_single_layer(self, tmp_path):
        p = _write(tmp_path / "a.yaml", "default: gpt\n")
        assert resolve_config([p]) == {"default": "gpt"}

    def test_higher_layer_overrides_scalar(self, tmp_path):
        low = _write(tmp_path / "low.yaml", "default: gpt\ndefaultCheap: qwen\n")
        high = _write(tmp_path / "high.yaml", "default: gemini\n")
        merged = resolve_config([low, high])
        assert merged["default"] == "gemini"  # project wins
        assert merged["defaultCheap"] == "qwen"  # preserved from lower layer

    def test_nested_registry_unions_and_project_wins(self, tmp_path):
        user = _write(
            tmp_path / "user.yaml",
            "models:\n"
            "  cheap: {slug: qwen/qwen3-32b}\n"
            "  mini: {slug: openai/gpt-4o-mini}\n"
            "default: mini\n",
        )
        project = _write(
            tmp_path / "project.yaml",
            "models:\n"
            "  cheap: {slug: qwen/qwen3-14b}\n"   # override one model
            "  flash: {slug: google/gemini-2.5-flash-lite}\n"  # add a new one
            "defaultCheap: cheap\n",
        )
        merged = resolve_config([user, project])
        # union of model names across layers
        assert set(merged["models"]) == {"cheap", "mini", "flash"}
        # project overrides the conflicting model
        assert merged["models"]["cheap"]["slug"] == "qwen/qwen3-14b"
        # sibling model from the lower layer is preserved
        assert merged["models"]["mini"]["slug"] == "openai/gpt-4o-mini"
        # scalars: lower-layer default kept, project adds defaultCheap
        assert merged["default"] == "mini"
        assert merged["defaultCheap"] == "cheap"

    def test_three_layer_precedence(self, tmp_path):
        shipped = _write(tmp_path / "shipped.yaml", "default: a\ndefaultCheap: a\n")
        user = _write(tmp_path / "user.yaml", "default: b\n")
        project = _write(tmp_path / "project.yaml", "default: c\n")
        merged = resolve_config([shipped, user, project])
        assert merged["default"] == "c"  # highest layer wins
        assert merged["defaultCheap"] == "a"  # only set in shipped

    def test_absent_middle_layer_skipped(self, tmp_path):
        shipped = _write(tmp_path / "shipped.yaml", "default: a\n")
        # user layer absent
        project = _write(tmp_path / "project.yaml", "default: c\n")
        merged = resolve_config([shipped, tmp_path / "missing.yaml", project])
        assert merged["default"] == "c"

    def test_inputs_not_mutated(self, tmp_path, monkeypatch):
        low = tmp_path / "low.yaml"
        high = tmp_path / "high.yaml"
        retained = {
            low: {"models": {"a": {"slug": "x"}}},
            high: {"models": {"b": {"slug": "y"}}},
        }

        def _retained_layer(path):
            return retained[path]

        monkeypatch.setattr(config_resolve, "load_config_layer", _retained_layer)
        before = copy.deepcopy(retained)

        merged = resolve_config([low, high])

        assert retained == before
        assert set(merged["models"]) == {"a", "b"}

    def test_malformed_layer_propagates(self, tmp_path):
        good = _write(tmp_path / "good.yaml", "default: a\n")
        bad = _write(tmp_path / "bad.yaml", "x: [oops\n")
        with pytest.raises(ConfigError):
            resolve_config([good, bad])


class TestStandardConfigLayers:
    def test_full_stack_order_and_paths(self, tmp_path):
        layers = standard_config_layers(
            "openrouter-models.yaml",
            plugin="llm-scripting-kit",
            marketplace="plugins-kit",
            shipped_default=tmp_path / "defaults" / "openrouter-models.yaml",
            project_root=tmp_path / "proj",
            data_root=tmp_path / "data",
        )
        assert layers == [
            tmp_path / "defaults" / "openrouter-models.yaml",
            tmp_path / "data" / "plugins-kit" / "llm-scripting-kit" / "openrouter-models.yaml",
            tmp_path / "proj" / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "openrouter-models.yaml",
        ]

    def test_user_only_when_no_shipped_or_project(self, tmp_path):
        layers = standard_config_layers(
            "c.yaml", plugin="p", data_root=tmp_path / "data"
        )
        assert layers == [tmp_path / "data" / "plugins-kit" / "p" / "c.yaml"]

    def test_filename_defaults_to_config_yaml(self, tmp_path):
        layers = standard_config_layers(
            plugin="p", project_root=tmp_path / "proj", data_root=tmp_path / "data"
        )
        assert layers == [
            tmp_path / "data" / "plugins-kit" / "p" / "config.yaml",
            tmp_path / "proj" / ".local-data" / "plugins-kit" / "p" / "config.yaml",
        ]

    def test_default_data_root_used_when_omitted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        layers = standard_config_layers("c.yaml", plugin="p")
        assert layers[0] == default_data_root() / "plugins-kit" / "p" / "c.yaml"

    def test_resolves_through_standard_layers(self, tmp_path):
        # End-to-end: seed user + project files at the standard locations, resolve.
        data_root = tmp_path / "data"
        proj = tmp_path / "proj"
        user_file = data_root / "plugins-kit" / "llm-scripting-kit" / "openrouter-models.yaml"
        proj_file = proj / ".local-data" / "plugins-kit" / "llm-scripting-kit" / "openrouter-models.yaml"
        _write(user_file, "models: {cheap: {slug: qwen/qwen3-32b}}\ndefault: cheap\n")
        _write(proj_file, "default: mini\nmodels: {mini: {slug: openai/gpt-4o-mini}}\n")
        layers = standard_config_layers(
            "openrouter-models.yaml",
            plugin="llm-scripting-kit",
            project_root=proj,
            data_root=data_root,
        )
        merged = resolve_config(layers)
        assert merged["default"] == "mini"  # project override wins
        assert set(merged["models"]) == {"cheap", "mini"}  # unioned


class TestResolvePluginDataDir:
    def test_default_is_namespaced_under_project(self, tmp_path):
        result = resolve_plugin_data_dir(
            tmp_path,
            marketplace="plugins-kit",
            plugin="demo-kit",
        )
        assert result == tmp_path / ".plugin-data" / "plugins-kit" / "demo-kit"

    def test_override_resolves_from_project_root(self, tmp_path):
        result = resolve_plugin_data_dir(
            tmp_path,
            marketplace="plugins-kit",
            plugin="demo-kit",
            config={"plugin_data_dir": "Generated/PluginData"},
        )
        assert result == (tmp_path / "Generated" / "PluginData").resolve()

    def test_absolute_override_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="relative to the project root"):
            resolve_plugin_data_dir(
                tmp_path,
                marketplace="plugins-kit",
                plugin="demo-kit",
                config={"plugin_data_dir": str((tmp_path / "absolute").resolve())},
            )

    def test_parent_escape_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="outside project root"):
            resolve_plugin_data_dir(
                tmp_path,
                marketplace="plugins-kit",
                plugin="demo-kit",
                config={"plugin_data_dir": "../shared"},
            )

    @pytest.mark.parametrize("override", ["C:tmp", r"\tmp", r"C:\tmp"])
    def test_windows_rooted_and_drive_relative_shapes_are_rejected(
        self, tmp_path, override
    ):
        assert PureWindowsPath(override).drive or PureWindowsPath(override).root

        with pytest.raises(ConfigError):
            resolve_plugin_data_dir(
                tmp_path,
                marketplace="plugins-kit",
                plugin="demo-kit",
                config={"plugin_data_dir": override},
            )

    def test_non_string_override_is_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="relative path string"):
            resolve_plugin_data_dir(
                tmp_path,
                marketplace="plugins-kit",
                plugin="demo-kit",
                config={"plugin_data_dir": 42},
            )
