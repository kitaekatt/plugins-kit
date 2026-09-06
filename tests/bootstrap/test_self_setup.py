"""Tests for _process_self_setup in bootstrap engine."""

import os
import sys

import pytest

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
)

# Add bootstrap plugin root to path so bootstrap_lib package is importable
sys.path.insert(0, BOOTSTRAP_ROOT)

from bootstrap_lib.engine import _process_self_setup


class TestProcessSelfSetup:
    def test_empty_self_setup(self, tmp_path):
        """Empty self_setup produces no entries or failures."""
        action_entries = []
        ok_entries = []
        failures = _process_self_setup({}, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)
        assert failures == []
        assert action_entries == []
        assert ok_entries == []

    def test_tool_found(self, tmp_path):
        """Tool that exists (git) produces ok entry with prefix."""
        self_setup = {
            "tools": [{"name": "git", "install": {"macos": "brew install git"}}],
        }
        action_entries = []
        ok_entries = []
        failures = _process_self_setup(self_setup, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)
        assert failures == []
        assert any("[bootstrap-setup]" in e and "git" in e and "ok" in e for e in ok_entries)

    def test_tool_missing(self, tmp_path):
        """Missing tool produces failure with prefix."""
        self_setup = {
            "tools": [{"name": "nonexistent_tool_xyz_self_setup"}],
        }
        action_entries = []
        ok_entries = []
        failures = _process_self_setup(self_setup, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)
        assert len(failures) == 1
        assert failures[0]["type"] == "tool"
        assert failures[0]["name"] == "nonexistent_tool_xyz_self_setup"
        assert failures[0]["plugin"] == "bootstrap"
        assert any("[bootstrap-setup]" in e for e in action_entries)

    def test_path_entry_added_to_env(self, tmp_path, monkeypatch):
        """Path entries are added to the current process PATH."""
        from unittest.mock import patch

        test_path = str(tmp_path / "test_bin")
        os.makedirs(test_path, exist_ok=True)
        original_path = os.environ.get("PATH", "")
        monkeypatch.setenv("PATH", original_path)

        self_setup = {"path_entries": [test_path]}
        action_entries = []
        ok_entries = []
        # Mock add_path_to_shell_config to prevent writing to real ~/.bashrc
        with patch("bootstrap_lib.path_check.add_path_to_shell_config", return_value=(True, "mocked")):
            _process_self_setup(self_setup, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)

        assert test_path in os.environ["PATH"]

    def test_tool_failure_plugin_name_matches_the_forwarded_param(self, tmp_path):
        """A non-default plugin_name is forwarded to the venv failure
        (existing test_venv_failure pins plugin="bootstrap" for the default);
        a tool failure must be consistent with it too, instead of hardcoding
        "bootstrap" regardless of what the caller passed."""
        self_setup = {
            "tools": [{"name": "nonexistent_tool_xyz_self_setup"}],
        }
        action_entries = []
        ok_entries = []
        failures = _process_self_setup(
            self_setup, "windows", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="other-plugin",
        )
        assert len(failures) == 1
        assert failures[0]["plugin"] == "other-plugin"

    def test_venv_failure(self, tmp_path):
        """Venv check that fails produces failure entry."""
        self_setup = {
            "venv": {"check_imports": ["nonexistent_module_xyz_self_setup"]},
        }
        action_entries = []
        ok_entries = []
        failures = _process_self_setup(self_setup, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)
        assert len(failures) == 1
        assert failures[0]["type"] == "venv"
        assert failures[0]["plugin"] == "bootstrap"

    def test_only_processes_three_phases(self, tmp_path):
        """self_setup ignores keys that belong to bootstrap.json (marketplaces, plugins, etc.)."""
        self_setup = {
            "tools": [{"name": "git", "install": {}}],
            "marketplaces": [{"name": "should-be-ignored"}],
            "plugins": [{"ref": "should:be-ignored"}],
        }
        action_entries = []
        ok_entries = []
        failures = _process_self_setup(self_setup, "windows", str(tmp_path), str(tmp_path), action_entries, ok_entries)
        # No marketplace or plugin processing — only tool check
        assert all("marketplace" not in e for e in action_entries)
        assert all("plugin" not in e for e in action_entries)
        # All entries should have the prefix
        for e in action_entries + ok_entries:
            assert e.startswith("[bootstrap-setup] ")
