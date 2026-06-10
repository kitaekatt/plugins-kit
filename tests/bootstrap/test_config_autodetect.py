"""Tests for config autodetect lifecycle."""

import os

import pytest

from bootstrap_lib.config_check import run_autodetect, run_project_autodetect, save_yaml_config, load_yaml_config


def _write_autodetect_script(plugin_root, script_name="custom_bootstrap.py", body=""):
    """Write an autodetect script to plugin_root."""
    path = os.path.join(plugin_root, script_name)
    with open(path, "w") as f:
        f.write(body)
    return path


class TestRunAutodetect:
    def test_calls_function_when_fields_empty(self, tmp_path):
        """Autodetect function is called and can modify config."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    config["P4PORT"] = "detected:1666"
    return True
""")
        config = {"P4PORT": "", "P4USER": ""}
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, "/path/c.yaml")
        assert changed is True
        assert config["P4PORT"] == "detected:1666"

    def test_not_called_when_spec_invalid(self, tmp_path):
        """Invalid autodetect spec (no function name) returns False."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        config = {"P4PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "just-a-script.py", config, "/path/c.yaml")
        assert changed is False

    def test_not_called_when_script_missing(self, tmp_path):
        """Missing script file returns False."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        config = {"P4PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "nonexistent.py autodetect", config, "/path/c.yaml")
        assert changed is False

    def test_errors_caught_gracefully(self, tmp_path):
        """Script that raises exception returns False."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    raise RuntimeError("boom")
""")
        config = {"P4PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, "/path/c.yaml")
        assert changed is False
        # B8: the error must surface as an action message, never be swallowed.
        assert any("autodetect FAILED" in a and "boom" in a for a in actions)

    def test_returns_false_no_changes(self, tmp_path):
        """Script that returns False means no changes."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    return False
""")
        config = {"P4PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, "/path/c.yaml")
        assert changed is False

    def test_config_written_back_after_changes(self, tmp_path):
        """When autodetect changes config, caller should save it (tested via save_yaml_config)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    config["DETECTED"] = "yes"
    return True
""")
        config_path = str(tmp_path / "config.yaml")
        save_yaml_config(config_path, {"DETECTED": ""})

        config = load_yaml_config(config_path)
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, config_path)
        assert changed is True

        save_yaml_config(config_path, config)
        reloaded = load_yaml_config(config_path)
        assert reloaded["DETECTED"] == "yes"

    def test_dict_return_with_messages(self, tmp_path):
        """Autodetect returning a dict provides messages to the engine."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    config["PORT"] = "1666"
    return {"changed": True, "actions": ["config: created /foo/bar.yaml"], "ok": []}
""")
        config = {"PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, "/path/c.yaml")
        assert changed is True
        assert actions == ["config: created /foo/bar.yaml"]
        assert ok == []

    def test_dict_return_ok_messages(self, tmp_path):
        """Autodetect returning ok messages for existing config."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    return {"changed": False, "actions": [], "ok": ["config: ok - /existing.yaml"]}
""")
        config = {"PORT": "val"}
        changed, actions, ok = run_autodetect(plugin_root, "custom_bootstrap.py autodetect", config, "/path/c.yaml")
        assert changed is False
        assert actions == []
        assert ok == ["config: ok - /existing.yaml"]


class TestRunProjectAutodetect:
    def test_returns_dict(self, tmp_path):
        """Project autodetect returns a dict of discovered values."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def discover():
    return {"uproject": "/path/to/Game.uproject", "engine_dir": "/path/to/engine"}
""")
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover")
        assert result == {"uproject": "/path/to/Game.uproject", "engine_dir": "/path/to/engine"}

    def test_returns_none(self, tmp_path):
        """Project autodetect returns None when nothing found."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def discover():
    return None
""")
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover")
        assert result is None

    def test_error_returns_none(self, tmp_path):
        """Project autodetect that raises returns None."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def discover():
    raise RuntimeError("boom")
""")
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover")
        assert result is None

    def test_invalid_spec_returns_none(self, tmp_path):
        """Invalid spec (no function name) returns None."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        result = run_project_autodetect(plugin_root, "just-a-script.py")
        assert result is None

    def test_missing_script_returns_none(self, tmp_path):
        """Missing script returns None."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        result = run_project_autodetect(plugin_root, "nonexistent.py discover")
        assert result is None

    def test_non_dict_return_is_none(self, tmp_path):
        """Non-dict return (e.g. bool) is treated as None."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def discover():
    return True
""")
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover")
        assert result is None


class TestAutodetectErrorSurfacing:
    """B8: a crashed autodetect script is logged, not silently swallowed."""

    def test_run_project_autodetect_appends_error(self, tmp_path):
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def discover():
    raise RuntimeError("kaboom")
""")
        errors = []
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover", errors=errors)
        assert result is None
        assert errors and "kaboom" in errors[0]
        assert "project autodetect FAILED" in errors[0]

    def test_run_project_autodetect_no_error_for_clean_none(self, tmp_path):
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def discover():
    return None
""")
        errors = []
        result = run_project_autodetect(plugin_root, "custom_bootstrap.py discover", errors=errors)
        assert result is None
        assert errors == []
