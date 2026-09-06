"""Tests for marketplace removal -- bootstrap.json marketplaces[].remove / enabled:false.

Engine-flow tests mirror TestEnginePinFlow in test_marketplace_pin.py: real
registry files under a monkeypatched HOME, the lifecycle function mocked.
`check_marketplace_exists` reads known_marketplaces.json from ~ (HOME is
monkeypatched), so it is driven by the fixture rather than mocked.
"""

import json

import pytest
from unittest.mock import patch

from bootstrap_lib.marketplace_lifecycle import (
    LifecycleResult,
    check_plugin_enabled_at_scope,
    load_pin_markers,
    save_pin_markers,
)


class TestEngineMarketplaceRemove:
    def _setup_home(self, tmp_path, monkeypatch, registered=True):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        km = plugins_dir / "known_marketplaces.json"
        entries = {}
        if registered:
            entries["plugins-kit-sf"] = {
                "source": {"source": "git", "url": "https://github.com/example-org/plugins-kit.git"},
                "installLocation": str(tmp_path / "marketplaces" / "plugins-kit-sf"),
                "autoUpdate": True,
            }
            (tmp_path / "marketplaces" / "plugins-kit-sf").mkdir(parents=True)
        km.write_text(json.dumps(entries))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return km

    def _run(self, tmp_path, manifest):
        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
            action_entries, ok_entries, plugin_name="test",
        )
        return action_entries, ok_entries, failures

    def test_remove_true_deregisters_when_registered(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [{"name": "plugins-kit-sf", "remove": True}]}
        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace",
                   return_value=LifecycleResult(True, "plugins-kit-sf", "marketplace removed")) as mock_rm:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_rm.assert_called_once_with("plugins-kit-sf")
        assert any("plugins-kit-sf: removed" in e for e in action_entries)
        assert failures == []

    def test_enabled_false_also_removes(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [{"name": "plugins-kit-sf", "enabled": False}]}
        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace",
                   return_value=LifecycleResult(True, "plugins-kit-sf", "marketplace removed")) as mock_rm:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_rm.assert_called_once_with("plugins-kit-sf")
        assert any("plugins-kit-sf: removed" in e for e in action_entries)
        assert failures == []

    def test_already_absent_is_quiet_ok(self, tmp_path, monkeypatch):
        """A checked-in remove directive must not error every session once the
        marketplace is gone -- absence is a verbose-only ok, not an action."""
        self._setup_home(tmp_path, monkeypatch, registered=False)
        manifest = {"marketplaces": [{"name": "plugins-kit-sf", "remove": True}]}
        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace") as mock_rm:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_rm.assert_not_called()
        assert any("plugins-kit-sf: already removed" in e for e in ok_entries)
        assert not any("already removed" in e for e in action_entries)
        assert failures == []

    def test_remove_failure_recorded(self, tmp_path, monkeypatch):
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [{"name": "plugins-kit-sf", "remove": True}]}
        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace",
                   return_value=LifecycleResult(False, "plugins-kit-sf", "remove failed: not found")):
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
        assert any("remove failed" in e for e in action_entries)
        assert any(f["type"] == "marketplace" and "remove failed" in f["message"] for f in failures)

    def test_remove_takes_precedence_over_pin(self, tmp_path, monkeypatch):
        """remove wins over every other field -- a teardown never pins/sources."""
        self._setup_home(tmp_path, monkeypatch)
        manifest = {"marketplaces": [
            {"name": "plugins-kit-sf", "remove": True, "pin": "f7f6276a",
             "source": "https://github.com/example-org/plugins-kit.git"}
        ]}
        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace",
                   return_value=LifecycleResult(True, "plugins-kit-sf", "marketplace removed")) as mock_rm, \
             patch("bootstrap_lib.marketplace_lifecycle.apply_marketplace_pin") as mock_pin, \
             patch("bootstrap_lib.marketplace_lifecycle.add_marketplace") as mock_add:
            action_entries, ok_entries, failures = self._run(tmp_path, manifest)
            mock_rm.assert_called_once_with("plugins-kit-sf")
            mock_pin.assert_not_called()
            mock_add.assert_not_called()
        assert failures == []

    def test_project_scope_disable_mutates_project_settings(self, tmp_path, monkeypatch):
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        install_path = tmp_path / "cache" / "plugin"
        install_path.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(json.dumps({
            "version": 2,
            "plugins": {
                "tool@mkt": [{
                    "scope": "project",
                    "projectPath": str(tmp_path),
                    "installPath": str(install_path),
                    "version": "1.0.0",
                }],
            },
        }))
        project_settings = tmp_path / ".claude" / "settings.json"
        project_settings.write_text(json.dumps({
            "enabledPlugins": {"tool@mkt": True},
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(
            "bootstrap_lib.marketplace_lifecycle.resolve_claude_cli",
            lambda: "/usr/local/bin/claude",
        )

        from bootstrap_lib.engine import _process_manifest
        actions = []
        oks = []
        failures = _process_manifest(
            {"plugins": [{
                "ref": "mkt:tool", "enabled": False,
                "scope": "project",
            }]},
            "windows", str(tmp_path / "data"), str(tmp_path / "root"),
            actions, oks, plugin_name="test", project_dir=str(tmp_path),
        )

        assert failures == []
        assert json.loads(project_settings.read_text())["enabledPlugins"]["tool@mkt"] is False
        assert check_plugin_enabled_at_scope("mkt:tool", "project", str(tmp_path)).passed is False

    def test_remove_clears_pin_marker_before_marketplace_is_readded(
            self, tmp_path, monkeypatch):
        km = self._setup_home(tmp_path, monkeypatch)
        save_pin_markers({"plugins-kit-sf": {
            "pin": "abc", "resolved_sha": "abc", "prior_auto_update": True,
        }})

        with patch("bootstrap_lib.marketplace_lifecycle.remove_marketplace",
                   return_value=LifecycleResult(True, "plugins-kit-sf", "removed")):
            self._run(tmp_path, {"marketplaces": [{"name": "plugins-kit-sf", "remove": True}]})

        assert "plugins-kit-sf" not in load_pin_markers()
        km.write_text(json.dumps({}))
        with patch("bootstrap_lib.marketplace_lifecycle.add_marketplace",
                   return_value=LifecycleResult(True, "plugins-kit-sf", "added")) as mock_add:
            _actions, _oks, failures = self._run(tmp_path, {"marketplaces": [{
                "name": "plugins-kit-sf",
                "source": "https://github.com/example-org/plugins-kit.git",
            }]})

        mock_add.assert_called_once()
        assert failures == []
