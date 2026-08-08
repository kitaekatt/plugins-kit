"""Tests for marketplace removal — bootstrap.json marketplaces[].remove / enabled:false.

Engine-flow tests mirror TestEnginePinFlow in test_marketplace_pin.py: real
registry files under a monkeypatched HOME, the lifecycle function mocked.
`check_marketplace_exists` reads known_marketplaces.json from ~ (HOME is
monkeypatched), so it is driven by the fixture rather than mocked.
"""

import json

import pytest
from unittest.mock import patch

from bootstrap_lib.marketplace_lifecycle import LifecycleResult


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
        marketplace is gone — absence is a verbose-only ok, not an action."""
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
        """remove wins over every other field — a teardown never pins/sources."""
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
