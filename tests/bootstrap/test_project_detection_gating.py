"""Tests for project detection gating in bootstrap engine.

When no project is detected (autodetect returns None, no project config file),
project-scoped phases (config required_fields, ini_settings) should be skipped.
"""

import os
from unittest.mock import patch

import pytest

from bootstrap_lib.config_check import load_yaml_config, save_yaml_config
from bootstrap_lib.engine import _process_config, _process_manifest, _process_project_config


def _make_config_section(plugin_root, defaults_source=None):
    """Build a config section with required_fields that would normally fail."""
    section = {
        "file": "config.yaml",
        "required_fields": {
            "uproject": {"user_msg": "UE project path", "agent_msg": "Set uproject in {config_path}"},
            "engine_dir": {"user_msg": "Engine directory", "agent_msg": "Set engine_dir in {config_path}"},
        },
    }
    if defaults_source:
        section["defaults_source"] = defaults_source
    return section


class TestProcessConfigProjectGating:
    def test_should_skip_required_fields_when_no_project(self, tmp_path):
        """project_detected=False -> no failures, ok_entry with skip message."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        # Create a config file with empty required fields (would normally fail)
        save_yaml_config(os.path.join(plugin_data_dir, "config.yaml"), {"uproject": "", "engine_dir": ""})

        section = _make_config_section(plugin_root)
        action_entries = []
        ok_entries = []

        failures = _process_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
            project_detected=False,
        )

        assert failures == []
        assert any("skipped required_fields" in e for e in ok_entries)

    def test_should_fail_required_fields_when_project_detected(self, tmp_path):
        """project_detected=True + missing fields -> failures (regression guard)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        # Create config with empty required fields
        save_yaml_config(os.path.join(plugin_data_dir, "config.yaml"), {"uproject": "", "engine_dir": ""})

        section = _make_config_section(plugin_root)
        action_entries = []
        ok_entries = []

        failures = _process_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
            project_detected=True,
        )

        assert len(failures) > 0
        assert all(f["type"] == "config" for f in failures)

    def test_should_still_copy_defaults_when_no_project(self, tmp_path):
        """project_detected=False still copies default config (ready for later use)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        defaults_dir = os.path.join(plugin_root, "defaults")
        os.makedirs(defaults_dir)
        save_yaml_config(os.path.join(defaults_dir, "config.yaml"), {"uproject": "", "engine_dir": ""})

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = _make_config_section(plugin_root, defaults_source="defaults/config.yaml")
        action_entries = []
        ok_entries = []

        _process_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
            project_detected=False,
        )

        # Defaults should have been copied even though project not detected
        config_path = os.path.join(plugin_data_dir, "config.yaml")
        assert os.path.isfile(config_path)

    def test_should_default_to_project_detected_true(self, tmp_path):
        """Omitting project_detected defaults to True (backward compatible)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        save_yaml_config(os.path.join(plugin_data_dir, "config.yaml"), {"uproject": "", "engine_dir": ""})

        section = _make_config_section(plugin_root)
        action_entries = []
        ok_entries = []

        # Call without project_detected — should behave as True (failures expected)
        failures = _process_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        assert len(failures) > 0


class TestProcessProjectConfigDetection:
    def test_stale_config_with_missing_fields_and_autodetect_none_returns_false(self, tmp_path, monkeypatch):
        """Stale .claude/unreal-kit.yaml exists (missing engine_dir) but autodetect
        returns None (no .uproject in CWD) -> should return False, not True."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        # Simulate stale project config: uproject set, engine_dir missing
        project_config_dir = tmp_path / ".claude"
        project_config_dir.mkdir()
        project_config_path = project_config_dir / "unreal-kit.yaml"
        save_yaml_config(str(project_config_path), {"uproject": "/some/old/Project.uproject"})

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []

        monkeypatch.chdir(tmp_path)
        with patch("bootstrap_lib.config_check.run_project_autodetect", return_value=None) as mock_ad:
            result = _process_project_config(
                section, plugin_data_dir, plugin_root,
                action_entries, ok_entries=ok_entries, plugin_name="test",
            )

        assert result is False, "Should return False when autodetect finds no project in CWD"
        assert any("no project detected" in e for e in ok_entries)

    def test_stale_config_all_fields_present_returns_true(self, tmp_path, monkeypatch):
        """Stale config with ALL required fields set -> return True (config phase will validate)."""
        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        project_config_dir = tmp_path / ".claude"
        project_config_dir.mkdir()
        project_config_path = project_config_dir / "unreal-kit.yaml"
        save_yaml_config(str(project_config_path), {
            "uproject": "/some/Project.uproject",
            "engine_dir": "/some/engine",
        })

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []

        monkeypatch.chdir(tmp_path)
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        assert result is True


class TestProcessManifestProjectGating:
    def test_should_skip_ini_settings_when_no_project(self, tmp_path):
        """project_detected=False -> ini_settings skipped, ok_entry logged."""
        manifest = {
            "ini_settings": [
                {
                    "file": "/fake/path/DefaultEngine.ini",
                    "section": "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]",
                    "settings": {"bDeveloperMode": "True"},
                }
            ],
        }

        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=False,
        )

        assert failures == []
        assert any("ini_settings: skipped" in e for e in ok_entries)
        # No ini actions should have been attempted
        assert not any("ini " in e for e in action_entries)

    def test_should_process_ini_settings_when_project_detected(self, tmp_path):
        """project_detected=True -> ini_settings processed (regression guard)."""
        # Create a real ini file so the check has something to work with
        ini_path = str(tmp_path / "DefaultEngine.ini")
        with open(ini_path, "w") as f:
            f.write("")

        manifest = {
            "ini_settings": [
                {
                    "file": ini_path,
                    "section": "[TestSection]",
                    "settings": {"TestKey": "TestValue"},
                }
            ],
        }

        action_entries = []
        ok_entries = []

        _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=True,
        )

        # ini_settings should have been processed (set or already ok)
        all_entries = action_entries + ok_entries
        assert any("ini " in e or "ini_settings" in e for e in all_entries)
        # Should NOT have the skip message
        assert not any("skipped (no project detected)" in e for e in ok_entries)

    def test_should_not_skip_when_no_ini_settings_in_manifest(self, tmp_path):
        """Manifest without ini_settings + project_detected=False -> no skip message."""
        manifest = {"tools": []}

        action_entries = []
        ok_entries = []

        _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=False,
        )

        assert not any("ini_settings" in e for e in ok_entries)
