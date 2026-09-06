"""Tests for config autodetect lifecycle."""

import os

import pytest

from bootstrap_lib.config_check import run_autodetect, run_project_autodetect, save_yaml_config, load_yaml_config
from bootstrap_lib.engine import _process_config, _process_project_config


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
        assert len(actions) == 1
        assert "invalid" in actions[0]
        assert ok == []

    def test_not_called_when_script_missing(self, tmp_path):
        """Missing script file returns False."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        config = {"P4PORT": ""}
        changed, actions, ok = run_autodetect(plugin_root, "nonexistent.py autodetect", config, "/path/c.yaml")
        assert changed is False
        assert len(actions) == 1
        assert "nonexistent.py" in actions[0]
        assert ok == []

    def test_missing_function_is_logged_as_action(self, tmp_path):
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="def other(config, config_path):\n    return False\n")

        changed, actions, ok = run_autodetect(
            plugin_root, "custom_bootstrap.py autodetect", {}, "/path/c.yaml"
        )

        assert changed is False
        assert len(actions) == 1
        assert "autodetect" in actions[0]
        assert ok == []

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
        assert actions == []
        assert len(ok) == 1
        assert "unchanged" in ok[0]

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

    def test_dict_return_passes_messages_through_unchanged(self, tmp_path):
        """The script's own action/ok messages are the outcome; nothing is joined or rewritten."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    return {"changed": True, "actions": ["first", "second"], "ok": ["steady"]}
""")

        changed, actions, ok = run_autodetect(
            plugin_root, "custom_bootstrap.py autodetect", {}, "/path/c.yaml"
        )

        assert changed is True
        assert actions == ["first", "second"]
        assert ok == ["steady"]

    def test_silent_dict_return_still_logs_one_outcome(self, tmp_path):
        """A script that reports nothing still produces exactly one entry (never silent)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    return {"changed": True}
""")
        changed, actions, ok = run_autodetect(
            plugin_root, "custom_bootstrap.py autodetect", {}, "/path/c.yaml"
        )
        assert changed is True
        assert len(actions) == 1 and "changed" in actions[0]
        assert ok == []

        _write_autodetect_script(plugin_root, body="""\
def autodetect(config, config_path):
    return {"changed": False}
""")
        changed, actions, ok = run_autodetect(
            plugin_root, "custom_bootstrap.py autodetect", {}, "/path/c.yaml"
        )
        assert changed is False
        assert actions == []
        assert len(ok) == 1 and "unchanged" in ok[0]


class TestMalformedConfigEngineHandling:
    def test_process_config_preserves_malformed_file_and_logs_error(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_path = data_dir / "config.yaml"
        original = b"key: [unterminated\n"
        config_path.write_bytes(original)

        action_entries = []
        ok_entries = []
        failures = _process_config(
            {
                "file": "config.yaml",
                "required_fields": {"key": {"default": "replacement"}},
            },
            str(data_dir),
            str(plugin_root),
            action_entries,
            ok_entries=ok_entries,
            plugin_name="test",
        )

        assert failures == []
        assert config_path.read_bytes() == original
        assert len(action_entries) == 1
        assert str(config_path) in action_entries[0]
        assert "malformed YAML" in action_entries[0]
        assert ok_entries == []

    def test_process_project_config_preserves_malformed_file_and_logs_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        config_path = tmp_path / "config.yaml"
        original = b"key: [unterminated\n"
        config_path.write_bytes(original)

        action_entries = []
        ok_entries = []
        result = _process_project_config(
            {
                "file": "config.yaml",
                "required_fields": {"key": {"default": "replacement"}},
            },
            str(data_dir),
            str(plugin_root),
            action_entries,
            ok_entries=ok_entries,
            plugin_name="test",
        )

        assert result is False
        assert config_path.read_bytes() == original
        assert len(action_entries) == 1
        assert str(config_path) in action_entries[0]
        assert "malformed YAML" in action_entries[0]
        assert ok_entries == []


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


class TestProcessConfigAcceptsListFormRequiredFields:
    """_process_config must accept required_fields in list form, same as
    _process_project_config already does via _normalize_project_required_fields.
    Passing the list straight to config_validate's .items()/.values() raises
    AttributeError out of an unwrapped phase."""

    def test_list_form_required_fields_does_not_raise(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("KEY: value\n")

        action_entries = []
        ok_entries = []
        failures = _process_config(
            {"file": "config.yaml", "required_fields": ["KEY"]},
            str(data_dir),
            str(plugin_root),
            action_entries,
            ok_entries=ok_entries,
            plugin_name="test",
        )

        assert failures == []
        assert any("config ok" in e for e in ok_entries)

    def test_list_form_and_dict_form_agree_on_missing_field(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "config.yaml").write_text("OTHER: value\n")

        action_entries = []
        failures = _process_config(
            {"file": "config.yaml", "required_fields": ["KEY"]},
            str(data_dir),
            str(plugin_root),
            action_entries,
            plugin_name="test",
        )

        assert len(failures) == 1
        assert failures[0]["field"] == "KEY"
