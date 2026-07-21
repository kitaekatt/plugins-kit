"""Tests for marketplace_lifecycle.py — CLI-based marketplace and plugin operations."""

import json
import os
import sys
from unittest.mock import patch

import pytest

from bootstrap_lib.marketplace_lifecycle import (
    LifecycleResult,
    ScopeCheckResult,
    VersionCheckResult,
    _find_claude_cli,
    _run_claude,
    _version_greater,
    check_marketplace_current,
    check_marketplace_exists,
    check_plugin_enabled_at_scope,
    check_plugin_installed,
    check_plugin_min_version,
    check_plugin_scope,
    ensure_registry_scope,
    update_marketplace,
)


@pytest.fixture
def isolate_claude_lookup(tmp_path, monkeypatch):
    """Isolate _find_claude_cli from the host environment.

    Clears all env vars and well-known install locations the resolver checks,
    and forces shutil.which to miss. Tests can opt back in by setting specific
    inputs after the fixture runs.
    """
    for var in ("CLAUDE_REAL_BIN", "CLAUDE_CODE_EXECPATH"):
        monkeypatch.delenv(var, raising=False)
    # Point all Windows install-location env vars at empty dirs so the
    # resolver's well-known candidates don't accidentally hit a real install.
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("APPDATA", str(empty_dir))
    monkeypatch.setenv("LOCALAPPDATA", str(empty_dir))
    monkeypatch.setenv("USERPROFILE", str(empty_dir))
    monkeypatch.setenv("HOME", str(empty_dir))
    monkeypatch.setattr(
        "bootstrap_lib.marketplace_lifecycle.shutil.which",
        lambda name: None,
    )
    monkeypatch.setattr(
        "bootstrap_lib.marketplace_lifecycle._query_system_shell_for_claude",
        lambda is_windows: None,
    )


class TestFindClaudeCli:
    """Tests for _find_claude_cli — env, PATH, and well-known location lookup."""

    def test_returns_path_when_env_set_and_file_exists(self, tmp_path, monkeypatch, isolate_claude_lookup):
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\n")
        monkeypatch.setenv("CLAUDE_REAL_BIN", str(fake_bin))
        assert _find_claude_cli() == str(fake_bin)

    def test_returns_none_when_env_not_set(self, isolate_claude_lookup):
        assert _find_claude_cli() is None

    def test_returns_none_when_env_set_but_file_missing(self, tmp_path, monkeypatch, isolate_claude_lookup):
        monkeypatch.setenv("CLAUDE_REAL_BIN", str(tmp_path / "nonexistent"))
        assert _find_claude_cli() is None

    def test_returns_none_when_env_is_empty(self, monkeypatch, isolate_claude_lookup):
        monkeypatch.setenv("CLAUDE_REAL_BIN", "")
        assert _find_claude_cli() is None

    def test_falls_back_to_exec_path_env(self, tmp_path, monkeypatch, isolate_claude_lookup):
        fake_bin = tmp_path / "claude.exe"
        fake_bin.write_text("")
        monkeypatch.setenv("CLAUDE_CODE_EXECPATH", str(fake_bin))
        assert _find_claude_cli() == str(fake_bin)

    def test_falls_back_to_shutil_which(self, tmp_path, monkeypatch, isolate_claude_lookup):
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("")
        monkeypatch.setattr(
            "bootstrap_lib.marketplace_lifecycle.shutil.which",
            lambda name: str(fake_bin) if name == "claude" else None,
        )
        assert _find_claude_cli() == str(fake_bin)

    def test_falls_back_to_npm_global_bin_on_windows(self, tmp_path, monkeypatch, isolate_claude_lookup):
        # Simulate Windows: APPDATA\npm\claude.cmd from `npm install -g`
        monkeypatch.setattr(sys, "platform", "win32")
        appdata = tmp_path / "appdata"
        npm_dir = appdata / "npm"
        npm_dir.mkdir(parents=True)
        claude_cmd = npm_dir / "claude.cmd"
        claude_cmd.write_text("@echo off\n")
        monkeypatch.setenv("APPDATA", str(appdata))
        assert _find_claude_cli() == str(claude_cmd)

    def test_falls_back_to_native_installer_on_windows(self, tmp_path, monkeypatch, isolate_claude_lookup):
        monkeypatch.setattr(sys, "platform", "win32")
        localappdata = tmp_path / "localappdata"
        prog_dir = localappdata / "Programs" / "claude"
        prog_dir.mkdir(parents=True)
        claude_exe = prog_dir / "claude.exe"
        claude_exe.write_text("")
        monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
        assert _find_claude_cli() == str(claude_exe)

    def test_appends_extension_when_real_bin_missing_suffix_on_windows(self, tmp_path, monkeypatch, isolate_claude_lookup):
        monkeypatch.setattr(sys, "platform", "win32")
        claude_cmd = tmp_path / "claude.cmd"
        claude_cmd.write_text("@echo off\n")
        # Env var without the .cmd suffix — the resolver should still find it.
        monkeypatch.setenv("CLAUDE_REAL_BIN", str(tmp_path / "claude"))
        assert _find_claude_cli() == str(claude_cmd)


class TestRunClaude:
    """Tests for _run_claude — CLI invocation via _find_claude_cli."""

    def test_returns_failure_when_cli_not_found(self, isolate_claude_lookup):
        ok, stdout, stderr = _run_claude(["plugin", "list"])
        assert ok is False
        assert "claude CLI not found" in stderr

    def test_invokes_binary_from_env(self, tmp_path, monkeypatch, isolate_claude_lookup):
        fake_bin = tmp_path / "claude"
        fake_bin.write_text("#!/bin/sh\n")
        monkeypatch.setenv("CLAUDE_REAL_BIN", str(fake_bin))
        with patch("bootstrap_lib.marketplace_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            ok, stdout, stderr = _run_claude(["plugin", "list"])
        assert ok is True
        assert mock_run.call_args[0][0][0] == str(fake_bin)


class TestCheckMarketplaceExists:
    def test_exists(self, tmp_path, monkeypatch):
        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({
            "my-market": {"source": {"source": "git", "url": "https://example.com"}, "autoUpdate": True, "installLocation": str(tmp_path / "marketplaces" / "my-market")}
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_marketplace_exists("my-market")
        assert result.passed is True

    def test_not_exists(self, tmp_path, monkeypatch):
        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_marketplace_exists("missing-market")
        assert result.passed is False

    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = check_marketplace_exists("anything")
        assert result.passed is False


class TestCheckPluginInstalled:
    def test_installed_colon_format(self, tmp_path, monkeypatch):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {"plugins-kit:bootstrap": [{"installPath": "/some/path", "version": "1.0"}]}
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_plugin_installed("plugins-kit:bootstrap")
        assert result.passed is True

    def test_installed_at_format(self, tmp_path, monkeypatch):
        """Finds plugin even when registry uses plugin@marketplace format."""
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {"bootstrap@plugins-kit": [{"installPath": "/some/path", "version": "1.0"}]}
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_plugin_installed("plugins-kit:bootstrap")
        assert result.passed is True

    def test_not_installed(self, tmp_path, monkeypatch):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({"version": 2, "plugins": {}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_plugin_installed("plugins-kit:bootstrap")
        assert result.passed is False

    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = check_plugin_installed("plugins-kit:bootstrap")
        assert result.passed is False


class TestMarketplaceAlwaysUpdate:
    """Tests for alwaysUpdate marketplace behavior in the engine's _process_manifest."""

    @staticmethod
    def _setup_engine_path():
        """Add engine to sys.path for direct _process_manifest import."""
        bootstrap_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
        )
        engine_dir = os.path.join(bootstrap_root, "engine")
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)

    def test_always_update_calls_update_when_behind(self, tmp_path, monkeypatch):
        """When alwaysUpdate is true and marketplace is behind, update_marketplace is called."""
        self._setup_engine_path()

        # Setup known_marketplaces.json so check_marketplace_exists passes
        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(tmp_path / "marketplaces" / "my-market"),
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "marketplaces": [
                {"name": "my-market", "source": "https://example.com", "alwaysUpdate": True}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.check_marketplace_current",
                    return_value=LifecycleResult(passed=False, ref="my-market", message="updates available")), \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace",
                    return_value=LifecycleResult(passed=True, ref="my-market", message="updated")) as mock_update:
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_update.assert_called_once_with("my-market")

        # Marketplace refresh is verbose-only (plumbing, not user-visible outcome).
        assert any("updated (alwaysUpdate)" in e for e in ok_entries)
        assert not any("updated (alwaysUpdate)" in e for e in action_entries)

    def test_always_update_silent_when_current(self, tmp_path, monkeypatch):
        """When alwaysUpdate is true but marketplace is current, result is silent (ok_entries)."""
        self._setup_engine_path()

        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(tmp_path / "marketplaces" / "my-market"),
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "marketplaces": [
                {"name": "my-market", "source": "https://example.com", "alwaysUpdate": True}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.check_marketplace_current",
                    return_value=LifecycleResult(passed=True, ref="my-market", message="up to date")), \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace") as mock_update:
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_update.assert_not_called()

        assert any("up to date" in e for e in ok_entries)
        assert not any("updating" in e for e in action_entries)

    def test_no_always_update_skips_update(self, tmp_path, monkeypatch):
        """When alwaysUpdate is not set, marketplace just logs ok."""
        self._setup_engine_path()

        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(tmp_path / "marketplaces" / "my-market"),
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "marketplaces": [
                {"name": "my-market", "source": "https://example.com"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.update_marketplace") as mock_update:
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_update.assert_not_called()

        assert any("ok" in e for e in ok_entries)

    def test_always_update_failure_logged(self, tmp_path, monkeypatch):
        """When alwaysUpdate update fails, failure is recorded."""
        self._setup_engine_path()

        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True)
        km.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(tmp_path / "marketplaces" / "my-market"),
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "marketplaces": [
                {"name": "my-market", "source": "https://example.com", "alwaysUpdate": True}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.check_marketplace_current",
                    return_value=LifecycleResult(passed=False, ref="my-market", message="updates available")), \
             patch("bootstrap_lib.marketplace_lifecycle.update_marketplace",
                    return_value=LifecycleResult(passed=False, ref="my-market", message="network error")):
            failures = _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )

        assert any("update failed" in e for e in action_entries)
        assert any(f["type"] == "marketplace" for f in failures)


class TestVersionGreater:
    """Tests for _version_greater semver comparison."""

    def test_greater(self):
        assert _version_greater("0.5.2", "0.5.1") is True

    def test_equal(self):
        assert _version_greater("0.5.2", "0.5.2") is False

    def test_less(self):
        assert _version_greater("0.5.1", "0.5.2") is False

    def test_major_greater(self):
        assert _version_greater("1.0.0", "0.9.9") is True

    def test_installed_newer_than_marketplace(self):
        """The exact case that caused the bug: installed 0.5.2 vs marketplace 0.5.1."""
        assert _version_greater("0.5.1", "0.5.2") is False



class TestCheckPluginScope:
    """Tests for check_plugin_scope — detecting scope mismatches."""

    def _write_installed(self, tmp_path, monkeypatch, plugins_data):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True, exist_ok=True)
        ip.write_text(json.dumps({"version": 2, "plugins": plugins_data}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def test_scope_matches(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.5.4"}]
        })
        result = check_plugin_scope("plugins-kit:bootstrap", "user")
        assert result.matches is True
        assert result.installed_scope == "user"

    def test_scope_mismatch_project_vs_user(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "project", "version": "0.5.4"}]
        })
        result = check_plugin_scope("plugins-kit:bootstrap", "user")
        assert result.matches is False
        assert result.installed_scope == "project"
        assert "want user" in result.message

    def test_scope_mismatch_user_vs_project(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.5.4"}]
        })
        result = check_plugin_scope("plugins-kit:bootstrap", "project")
        assert result.matches is False
        assert result.installed_scope == "user"
        assert "want project" in result.message

    def test_not_installed_returns_matches(self, tmp_path, monkeypatch):
        """Not installed → matches=True (nothing to fix)."""
        self._write_installed(tmp_path, monkeypatch, {})
        result = check_plugin_scope("plugins-kit:bootstrap", "user")
        assert result.matches is True
        assert result.installed_scope == ""

    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = check_plugin_scope("plugins-kit:bootstrap", "user")
        assert result.matches is True


class TestCheckPluginEnabledAtScope:
    """Tests for check_plugin_enabled_at_scope — reading settings files directly."""

    def test_enabled_at_user_scope(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "user")
        assert result.passed is True
        assert "user scope" in result.message

    def test_not_enabled_at_user_scope(self, tmp_path, monkeypatch):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"enabledPlugins": {}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "user")
        assert result.passed is False
        assert "not enabled at user scope" in result.message

    def test_enabled_at_project_scope(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "myproject"
        settings = project_dir / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "project", str(project_dir))
        assert result.passed is True
        assert "project scope" in result.message

    def test_project_scope_without_project_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "project")
        assert result.passed is False
        assert "missing project_dir" in result.message

    def test_no_settings_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "user")
        assert result.passed is False

    def test_enabled_at_user_not_at_project(self, tmp_path, monkeypatch):
        """Plugin enabled at user scope but not project scope."""
        # User settings has the plugin
        user_settings = tmp_path / ".claude" / "settings.json"
        user_settings.parent.mkdir(parents=True)
        user_settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        # Project settings does NOT have the plugin
        project_dir = tmp_path / "myproject"
        project_settings = project_dir / ".claude" / "settings.json"
        project_settings.parent.mkdir(parents=True)
        project_settings.write_text(json.dumps({"enabledPlugins": {}}))
        monkeypatch.setenv("HOME", str(tmp_path))

        user_result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "user")
        assert user_result.passed is True
        project_result = check_plugin_enabled_at_scope("plugins-kit:bootstrap", "project", str(project_dir))
        assert project_result.passed is False


class TestScopeRemediation:
    """Tests for scope remediation in the engine — ensure-at-desired-scope pattern."""

    @staticmethod
    def _setup_engine_path():
        bootstrap_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
        )
        engine_dir = os.path.join(bootstrap_root, "engine")
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)

    def test_enabled_at_desired_scope_no_action(self, tmp_path, monkeypatch):
        """Plugin already enabled at desired scope → no install action needed."""
        self._setup_engine_path()

        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "bootstrap@plugins-kit": [{"scope": "user", "version": "0.5.4", "installPath": "/cache"}]
            }
        }))
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.install_plugin") as mock_inst, \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_inst.assert_not_called()

        assert not any("installing at" in e for e in action_entries)

    def test_not_enabled_at_desired_scope_triggers_install(self, tmp_path, monkeypatch):
        """Plugin not enabled at desired scope → install at that scope (no uninstall)."""
        self._setup_engine_path()

        # Plugin is in the registry (installed) but NOT in user settings
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "bootstrap@plugins-kit": [{"scope": "project", "version": "0.5.4", "installPath": "/cache"}]
            }
        }))
        # User settings does NOT have the plugin enabled
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"enabledPlugins": {}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.install_plugin",
                    return_value=LifecycleResult(passed=True, ref="plugins-kit:bootstrap", message="installed")) as mock_inst, \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            # Install at desired scope, no uninstall
            mock_inst.assert_called_once_with("plugins-kit:bootstrap", scope="user")

        # Scope-mismatch note kept as its own line; re-install action consolidated.
        assert any("not enabled at user scope" in e for e in action_entries)
        assert any("re-installed bootstrap [at user scope]" in e for e in action_entries)

    def test_stale_registry_scope_no_uninstall(self, tmp_path, monkeypatch):
        """Registry says 'project' but plugin is enabled at user scope → no action.

        This is the exact scenario from the bug report: installed_plugins.json
        has stale scope metadata, but the settings file shows the truth.
        """
        self._setup_engine_path()

        # Registry claims project scope (stale)
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "bootstrap@plugins-kit": [{"scope": "project", "version": "0.5.4", "installPath": "/cache"}]
            }
        }))
        # But user settings HAS it enabled (truth)
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.install_plugin") as mock_inst, \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_inst.assert_not_called()

        # No scope-related action entries
        assert not any("installing at" in e for e in action_entries)
        assert not any("scope mismatch" in e for e in action_entries)


class TestUpdateMarketplaceFallback:
    """Tests for update_marketplace CLI fallback when directory already exists."""

    def _write_known_marketplaces(self, tmp_path, monkeypatch, install_location):
        km = tmp_path / ".claude" / "plugins" / "known_marketplaces.json"
        km.parent.mkdir(parents=True, exist_ok=True)
        km.write_text(json.dumps({
            "my-market": {
                "source": {"source": "git", "url": "https://example.com"},
                "installLocation": str(install_location),
            }
        }))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def test_should_fallback_to_git_pull_when_cli_fails_with_already_exists(self, tmp_path, monkeypatch):
        """When CLI update fails with 'already exists', git pull the install location directly."""
        install_dir = tmp_path / "marketplaces" / "my-market"
        install_dir.mkdir(parents=True)
        self._write_known_marketplaces(tmp_path, monkeypatch, install_dir)

        already_exists_error = (
            "✘ Failed to update marketplace(s): Failed to refresh marketplace 'my-market': "
            "Failed to clone marketplace repository: fatal: destination path "
            f"'{install_dir}' already exists and is not an empty directory."
        )

        with patch("bootstrap_lib.marketplace_lifecycle._run_claude",
                   return_value=(False, "", already_exists_error)), \
             patch("bootstrap_lib.marketplace_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            result = update_marketplace("my-market")

        assert result.passed is True
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0] == ["git", "pull"]
        assert str(install_dir) in str(call_args[1].get("cwd", ""))

    def test_should_return_failure_when_cli_fails_without_already_exists(self, tmp_path, monkeypatch):
        """Non-'already exists' CLI failures propagate as failures (no git fallback)."""
        install_dir = tmp_path / "marketplaces" / "my-market"
        install_dir.mkdir(parents=True)
        self._write_known_marketplaces(tmp_path, monkeypatch, install_dir)

        with patch("bootstrap_lib.marketplace_lifecycle._run_claude",
                   return_value=(False, "", "network timeout")), \
             patch("bootstrap_lib.marketplace_lifecycle.subprocess.run") as mock_run:
            result = update_marketplace("my-market")

        assert result.passed is False
        mock_run.assert_not_called()

    def test_should_return_failure_when_git_pull_fails_in_fallback(self, tmp_path, monkeypatch):
        """If git pull also fails during fallback, return failure."""
        install_dir = tmp_path / "marketplaces" / "my-market"
        install_dir.mkdir(parents=True)
        self._write_known_marketplaces(tmp_path, monkeypatch, install_dir)

        already_exists_error = "already exists and is not an empty directory"

        with patch("bootstrap_lib.marketplace_lifecycle._run_claude",
                   return_value=(False, "", already_exists_error)), \
             patch("bootstrap_lib.marketplace_lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = type("R", (), {"returncode": 1, "stdout": "", "stderr": "merge conflict"})()
            result = update_marketplace("my-market")

        assert result.passed is False
        assert "merge conflict" in result.message or "git pull" in result.message

    def test_should_return_failure_when_install_location_not_found(self, tmp_path, monkeypatch):
        """If CLI fails with 'already exists' but installLocation unknown, return failure."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # No known_marketplaces.json written

        already_exists_error = "already exists and is not an empty directory"

        with patch("bootstrap_lib.marketplace_lifecycle._run_claude",
                   return_value=(False, "", already_exists_error)), \
             patch("bootstrap_lib.marketplace_lifecycle.subprocess.run") as mock_run:
            result = update_marketplace("my-market")

        assert result.passed is False
        mock_run.assert_not_called()


class TestEnsureRegistryScope:
    """Tests for ensure_registry_scope — fixing stale scope in installed_plugins.json."""

    def _write_registry(self, tmp_path, monkeypatch, plugins_data):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True, exist_ok=True)
        ip.write_text(json.dumps({"version": 2, "plugins": plugins_data}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return ip

    def test_fixes_stale_scope(self, tmp_path, monkeypatch):
        """Registry says 'project', desired is 'user' → updates to 'user'."""
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "project", "version": "0.8.4"}]
        })
        result = ensure_registry_scope("plugins-kit:bootstrap", "user")
        assert result is True
        data = json.loads(ip.read_text())
        assert data["plugins"]["bootstrap@plugins-kit"][0]["scope"] == "user"

    def test_already_correct_scope(self, tmp_path, monkeypatch):
        """Registry already has correct scope → no change needed."""
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.8.4"}]
        })
        result = ensure_registry_scope("plugins-kit:bootstrap", "user")
        assert result is True
        data = json.loads(ip.read_text())
        assert data["plugins"]["bootstrap@plugins-kit"][0]["scope"] == "user"

    def test_plugin_not_in_registry(self, tmp_path, monkeypatch):
        """Plugin not in registry → returns True (nothing to fix)."""
        self._write_registry(tmp_path, monkeypatch, {})
        result = ensure_registry_scope("plugins-kit:bootstrap", "user")
        assert result is True

    def test_no_registry_file(self, tmp_path, monkeypatch):
        """No registry file → returns False."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = ensure_registry_scope("plugins-kit:bootstrap", "user")
        assert result is False

    def test_no_op_leaves_registry_file_untouched(self, tmp_path, monkeypatch):
        """Scope already correct → NO write at all (B3).

        The registry's mtime arms the SessionStart cooldown's registry-change
        bypass. ensure_registry_scope runs every pass, so an unconditional
        rewrite would make the registry permanently newer than the cooldown
        stamp and re-arm a full bootstrap pass every session.
        """
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.8.4"}]
        })
        os.utime(ip, (1_000_000_000, 1_000_000_000))
        before = ip.stat().st_mtime_ns

        result = ensure_registry_scope("plugins-kit:bootstrap", "user")

        assert result is True
        assert ip.stat().st_mtime_ns == before, (
            "no-op pass must not rewrite installed_plugins.json"
        )

    def test_changed_write_is_atomic_with_no_temp_leftovers(self, tmp_path, monkeypatch):
        """A real scope fix rewrites via tmp+os.replace (no fixed-name temp
        files left behind, valid JSON afterwards)."""
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "project", "version": "0.8.4"}]
        })
        result = ensure_registry_scope("plugins-kit:bootstrap", "user")
        assert result is True
        data = json.loads(ip.read_text())
        assert data["plugins"]["bootstrap@plugins-kit"][0]["scope"] == "user"
        assert sorted(os.listdir(ip.parent)) == ["installed_plugins.json"], (
            "atomic write must not leave temp files next to the registry"
        )


class TestCheckPluginMinVersion:
    """Tests for check_plugin_min_version — constraint satisfaction against installed version."""

    def _write_installed(self, tmp_path, monkeypatch, plugins_data):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True, exist_ok=True)
        ip.write_text(json.dumps({"version": 2, "plugins": plugins_data}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

    def test_should_pass_when_installed_equals_min_version(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.9.1"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is True
        assert result.installed_version == "0.9.1"
        assert "satisfies >= 0.9.1" in result.message

    def test_should_pass_when_installed_greater_than_min_version(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.9.5"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is True
        assert result.installed_version == "0.9.5"

    def test_should_fail_when_installed_less_than_min_version(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.8.3"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is False
        assert result.installed_version == "0.8.3"
        assert "< required 0.9.1" in result.message

    def test_should_fail_on_major_version_gap(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.9.9"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "1.0.0")
        assert result.up_to_date is False

    def test_should_pass_on_major_version_jump(self, tmp_path, monkeypatch):
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "1.0.0"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.9")
        assert result.up_to_date is True

    def test_should_skip_when_plugin_not_installed(self, tmp_path, monkeypatch):
        """Not installed → up_to_date=True (skip-check semantics; install path handles missing plugin)."""
        self._write_installed(tmp_path, monkeypatch, {})
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is True
        assert result.installed_version == ""
        assert "skipping" in result.message

    def test_should_skip_when_min_version_is_empty(self, tmp_path, monkeypatch):
        """Empty min_version → up_to_date=True (no constraint to check)."""
        self._write_installed(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [{"scope": "user", "version": "0.8.3"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "")
        assert result.up_to_date is True
        assert "no min_version declared" in result.message

    def test_should_read_via_marketplace_colon_plugin_format(self, tmp_path, monkeypatch):
        """Installed registry entry stored in marketplace:plugin format is found."""
        self._write_installed(tmp_path, monkeypatch, {
            "plugins-kit:bootstrap": [{"scope": "user", "version": "0.9.5"}]
        })
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is True
        assert result.installed_version == "0.9.5"

    def test_should_skip_when_registry_file_missing(self, tmp_path, monkeypatch):
        """No registry file → treat as not-installed, skip check."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = check_plugin_min_version("plugins-kit:bootstrap", "0.9.1")
        assert result.up_to_date is True


class TestEngineMinVersionFlow:
    """Tests for the engine's min_version check in the plugins loop.

    Verifies the full flow: detect constraint, trigger update, recheck, record failure
    if unsatisfied after update.
    """

    @staticmethod
    def _setup_engine_path():
        bootstrap_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "bootstrap")
        )
        engine_dir = os.path.join(bootstrap_root, "engine")
        if engine_dir not in sys.path:
            sys.path.insert(0, engine_dir)

    def _write_registry_and_settings(self, tmp_path, monkeypatch, installed_version):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True)
        ip.write_text(json.dumps({
            "version": 2,
            "plugins": {
                "bootstrap@plugins-kit": [{"scope": "user", "version": installed_version, "installPath": "/cache"}]
            }
        }))
        settings = tmp_path / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"bootstrap@plugins-kit": True}}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return ip

    def test_should_not_trigger_update_when_min_version_satisfied(self, tmp_path, monkeypatch):
        """Installed version >= min_version → no update call, no action entries mentioning min_version."""
        self._setup_engine_path()
        self._write_registry_and_settings(tmp_path, monkeypatch, "0.9.5")

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin") as mock_update, \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            failures = _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_update.assert_not_called()

        assert not any("< required" in e for e in action_entries)
        assert not any(f["type"] == "plugin" and "min_version" in f.get("message", "") for f in failures)

    def test_should_trigger_update_and_record_success_when_update_satisfies_constraint(self, tmp_path, monkeypatch):
        """Installed < min_version, update bumps it to satisfy → recheck passes, no failure."""
        self._setup_engine_path()
        ip = self._write_registry_and_settings(tmp_path, monkeypatch, "0.8.3")

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        def _bump_installed_version(*args, **kwargs):
            """Simulate update_plugin bumping the installed version in the registry."""
            data = json.loads(ip.read_text())
            data["plugins"]["bootstrap@plugins-kit"][0]["version"] = "0.9.1"
            ip.write_text(json.dumps(data))
            return LifecycleResult(passed=True, ref="plugins-kit:bootstrap", message="updated")

        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin", side_effect=_bump_installed_version), \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            failures = _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )

        assert any("updated bootstrap [0.8.3 -> 0.9.1, satisfies >= 0.9.1]" in e for e in action_entries)
        assert not any(f["type"] == "plugin" and "min_version" in f.get("message", "") for f in failures)

    def test_should_record_failure_when_update_fails(self, tmp_path, monkeypatch):
        """Installed < min_version, update_plugin returns failure → record a plugin failure entry."""
        self._setup_engine_path()
        self._write_registry_and_settings(tmp_path, monkeypatch, "0.8.3")

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin",
                   return_value=LifecycleResult(passed=False, ref="plugins-kit:bootstrap", message="network error")), \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            failures = _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )

        assert any("update failed - network error" in e for e in action_entries)
        assert any(f["type"] == "plugin" and "min_version 0.9.1 not satisfied" in f.get("message", "") for f in failures)

    def test_should_record_failure_when_update_succeeds_but_still_too_old(self, tmp_path, monkeypatch):
        """Update succeeds but installed version is still < min_version (marketplace stale) → failure."""
        self._setup_engine_path()
        ip = self._write_registry_and_settings(tmp_path, monkeypatch, "0.8.3")

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user", "min_version": "0.9.1"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        def _bump_to_still_too_old(*args, **kwargs):
            data = json.loads(ip.read_text())
            data["plugins"]["bootstrap@plugins-kit"][0]["version"] = "0.8.9"
            ip.write_text(json.dumps(data))
            return LifecycleResult(passed=True, ref="plugins-kit:bootstrap", message="updated")

        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin", side_effect=_bump_to_still_too_old), \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            failures = _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )

        assert any("update failed to satisfy constraint" in e for e in action_entries)
        assert any(f["type"] == "plugin" and "min_version 0.9.1 not satisfied (installed 0.8.9)" in f.get("message", "") for f in failures)

    def test_should_skip_check_when_no_min_version_declared(self, tmp_path, monkeypatch):
        """No min_version field → no constraint check, no update call triggered by min_version logic."""
        self._setup_engine_path()
        self._write_registry_and_settings(tmp_path, monkeypatch, "0.8.3")

        manifest = {
            "plugins": [
                {"ref": "plugins-kit:bootstrap", "enabled": True, "scope": "user"}
            ]
        }

        from bootstrap_lib.engine import _process_manifest
        action_entries = []
        ok_entries = []

        with patch("bootstrap_lib.marketplace_lifecycle.update_plugin") as mock_update, \
             patch("bootstrap_lib.marketplace_lifecycle.check_plugin_version") as mock_ver:
            mock_ver.return_value = type("R", (), {"up_to_date": True})()
            _process_manifest(
                manifest, "windows", str(tmp_path / "data"), str(tmp_path / "root"),
                action_entries, ok_entries, plugin_name="test",
            )
            mock_update.assert_not_called()

        assert not any("< required" in e for e in action_entries)


class TestCheckMarketplaceCurrentNoUpstream:
    """B17: a clone without an upstream tracking branch must read as current,
    not as "updates available" (which triggered a doomed update every pass)."""

    def test_no_upstream_reads_as_current(self, tmp_path, monkeypatch):
        import subprocess

        # A real repo with one commit and no remote/upstream.
        repo = tmp_path / "mkt_clone"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "f.txt").write_text("x")
        env = {**os.environ,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True, env=env)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, capture_output=True, env=env)

        home = tmp_path / "home"
        plugins_dir = home / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "known_marketplaces.json").write_text(json.dumps({
            "test-mkt": {"installLocation": str(repo)},
        }))
        monkeypatch.setenv("HOME", str(home))
        # Windows expanduser('~') prefers USERPROFILE over HOME, so isolating the
        # home dir needs both (the pattern every other test in this file follows).
        monkeypatch.setenv("USERPROFILE", str(home))

        result = check_marketplace_current("test-mkt")
        assert result.passed is True
        assert "no upstream" in result.message


class TestEnsureRegistryScopeProjectPathGuard:
    """ensure_registry_scope must never scope-flip a record carrying
    projectPath -- stamping scope onto it manufactures the chimera record
    that wedges updates (claude-code#79892)."""

    def _write_registry(self, tmp_path, monkeypatch, plugins_data):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True, exist_ok=True)
        ip.write_text(json.dumps({"version": 2, "plugins": plugins_data}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        return ip

    def test_projectpath_record_untouched_healthy_sibling_fixed(self, tmp_path, monkeypatch):
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [
                {"scope": "project", "version": "0.45.0", "projectPath": "D:/dev/x"},
                {"scope": "project", "version": "0.52.0"},
            ]
        })
        assert ensure_registry_scope("plugins-kit:bootstrap", "user") is True
        entries = json.loads(ip.read_text())["plugins"]["bootstrap@plugins-kit"]
        assert entries[0]["scope"] == "project"  # projectPath record: untouched
        assert entries[1]["scope"] == "user"     # healthy record: fixed

    def test_only_projectpath_records_no_write(self, tmp_path, monkeypatch):
        ip = self._write_registry(tmp_path, monkeypatch, {
            "bootstrap@plugins-kit": [
                {"scope": "project", "version": "0.45.0", "projectPath": "D:/dev/x"},
            ]
        })
        before = ip.read_text()
        assert ensure_registry_scope("plugins-kit:bootstrap", "user") is True
        assert ip.read_text() == before


class TestCheckPluginScopeDuplicateRecords:
    def test_reads_scope_from_healthy_record(self, tmp_path, monkeypatch):
        ip = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
        ip.parent.mkdir(parents=True, exist_ok=True)
        ip.write_text(json.dumps({"version": 2, "plugins": {
            "bootstrap@plugins-kit": [
                {"scope": "project", "version": "0.45.0", "projectPath": "D:/dev/x"},
                {"scope": "user", "version": "0.52.0"},
            ],
        }}))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        result = check_plugin_scope("plugins-kit:bootstrap", "user")
        assert result.matches is True
        assert result.installed_scope == "user"
