"""Tests for bootstrap_lib/env_var_check.py and the engine env_vars phase."""

import json
import os
import sys
import types
from unittest.mock import patch

import pytest

from bootstrap_lib.engine import _process_manifest
from bootstrap_lib.env_var_check import (
    check_env_var,
    export_env_var,
    export_line,
    set_env_var,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so rc-file writes never touch the real home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


class TestUnixRcUpdate:
    def test_creates_rc_file_when_absent(self, isolated_home):
        ok, msg = set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")
        assert ok is True
        assert "created .bashrc" in msg
        content = (isolated_home / ".bashrc").read_text()
        assert 'export DEVROOT="/home/u/Dev"' in content
        assert "# Added by bootstrap" in content

    def test_appends_when_line_missing(self, isolated_home):
        rc = isolated_home / ".bashrc"
        rc.write_text("# my shell config\nalias ll='ls -l'\n")

        ok, msg = set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")

        assert ok is True
        assert "added to .bashrc" in msg
        content = rc.read_text()
        # Pre-existing content intact, export appended
        assert content.startswith("# my shell config\nalias ll='ls -l'\n")
        assert 'export DEVROOT="/home/u/Dev"' in content

    def test_updates_stale_line_in_place(self, isolated_home):
        rc = isolated_home / ".bashrc"
        rc.write_text(
            "# my shell config\n"
            'export DEVROOT="/old/path"\n'
            "alias ll='ls -l'\n"
        )

        ok, msg = set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")

        assert ok is True
        assert "updated .bashrc" in msg
        lines = rc.read_text().splitlines()
        # Replaced at the same position -- no stale second line appended
        assert lines == [
            "# my shell config",
            'export DEVROOT="/home/u/Dev"',
            "alias ll='ls -l'",
        ]

    def test_idempotent_second_set_no_rewrite(self, isolated_home):
        rc = isolated_home / ".bashrc"
        set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")
        first = rc.read_text()

        ok, msg = set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")

        assert ok is True
        assert msg == "already persisted"
        assert rc.read_text() == first

    def test_macos_writes_zshrc_and_bashrc(self, isolated_home):
        ok, msg = set_env_var("DEVROOT", "/Users/u/Dev", "macos")

        assert ok is True
        for rc_name in (".zshrc", ".bashrc"):
            content = (isolated_home / rc_name).read_text()
            assert 'export DEVROOT="/Users/u/Dev"' in content

    def test_check_fails_before_set_passes_after(self, isolated_home):
        assert check_env_var("DEVROOT", "/home/u/Dev", "ubuntu").passed is False
        set_env_var("DEVROOT", "/home/u/Dev", "ubuntu")
        result = check_env_var("DEVROOT", "/home/u/Dev", "ubuntu")
        assert result.passed is True
        assert result.subject == "DEVROOT"

    def test_check_fails_on_stale_value(self, isolated_home):
        (isolated_home / ".bashrc").write_text('export DEVROOT="/old/path"\n')
        result = check_env_var("DEVROOT", "/home/u/Dev", "ubuntu")
        assert result.passed is False
        assert ".bashrc" in result.message

    def test_check_macos_requires_both_rc_files(self, isolated_home):
        (isolated_home / ".zshrc").write_text('export DEVROOT="/Users/u/Dev"\n')
        result = check_env_var("DEVROOT", "/Users/u/Dev", "macos")
        assert result.passed is False
        assert ".bashrc" in result.message


class _FakeWinreg(types.ModuleType):
    """Minimal in-memory HKCU\\Environment stand-in (name -> value)."""

    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_WRITE = 2
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.store = {}
        self.writes = []

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, subkey, reserved, access):
        assert subkey == "Environment"
        return self._Key()

    def QueryValueEx(self, key, name):
        if name not in self.store:
            raise FileNotFoundError(name)
        return self.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, value_type, value):
        assert value_type == self.REG_SZ
        self.store[name] = value
        self.writes.append((name, value))


@pytest.fixture
def fake_winreg(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
    return fake


class TestWindowsRegistry:
    def test_set_writes_user_registry(self, fake_winreg):
        ok, msg = set_env_var("DEVROOT", "C:/dev", "windows")
        assert ok is True
        assert "Windows User environment (registry)" in msg
        assert fake_winreg.store["DEVROOT"] == "C:/dev"

    def test_check_fails_when_absent_passes_after_set(self, fake_winreg):
        assert check_env_var("DEVROOT", "C:/dev", "windows").passed is False
        set_env_var("DEVROOT", "C:/dev", "windows")
        assert check_env_var("DEVROOT", "C:/dev", "windows").passed is True

    def test_check_fails_on_stale_value(self, fake_winreg):
        fake_winreg.store["DEVROOT"] = "C:/old"
        result = check_env_var("DEVROOT", "C:/dev", "windows")
        assert result.passed is False
        assert "C:/old" in result.message
        assert "C:/dev" in result.message

    def test_skip_registry_flag_short_circuits(self, fake_winreg, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
        result = check_env_var("DEVROOT", "C:/dev", "windows")
        assert result.passed is True
        assert "BOOTSTRAP_SKIP_REGISTRY" in result.message
        ok, msg = set_env_var("DEVROOT", "C:/dev", "windows")
        assert ok is True
        assert "BOOTSTRAP_SKIP_REGISTRY" in msg
        assert fake_winreg.writes == []


class TestExportEnvVar:
    def test_sets_process_env_and_appends_env_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / "claude-env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        with patch.dict(os.environ):
            exported = export_env_var("BOOTSTRAP_TEST_EV", "/some/dir")
            assert exported == "BOOTSTRAP_TEST_EV"
            assert os.environ["BOOTSTRAP_TEST_EV"] == "/some/dir"
        assert "export BOOTSTRAP_TEST_EV=/some/dir\n" in env_file.read_text()

    def test_no_env_file_still_sets_process_env(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        with patch.dict(os.environ):
            exported = export_env_var("BOOTSTRAP_TEST_EV", "/some/dir")
            assert exported is None
            assert os.environ["BOOTSTRAP_TEST_EV"] == "/some/dir"

    def test_value_with_spaces_is_quoted(self, tmp_path, monkeypatch):
        env_file = tmp_path / "claude-env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        with patch.dict(os.environ):
            export_env_var("BOOTSTRAP_TEST_EV", "/some dir/x")
        assert "export BOOTSTRAP_TEST_EV='/some dir/x'\n" in env_file.read_text()


class TestEnvVarsPhase:
    """Engine-level tests for the env_vars manifest phase."""

    def _run(self, manifest, tmp_path, current_os="ubuntu"):
        data_dir = tmp_path / "data"
        plugin_root = tmp_path / "plugin"
        data_dir.mkdir(exist_ok=True)
        plugin_root.mkdir(exist_ok=True)
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, current_os, str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="config",
        )
        return failures, action_entries, ok_entries

    def test_install_command_sees_var_in_same_pass(
        self, isolated_home, tmp_path, monkeypatch
    ):
        """env_vars runs before tools: a tool install command in the same
        pass resolves the just-exported variable."""
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        marker = tmp_path / "marker"
        target = str(tmp_path / "devhome")
        manifest = {
            "env_vars": [{"name": "BOOTSTRAP_TEST_DEVROOT", "value": target}],
            "tools": [{
                "name": "fake-ev-tool",
                "check": f"test -s {marker}",
                "install": {
                    "macos": f'printf %s "$BOOTSTRAP_TEST_DEVROOT" > {marker}',
                },
            }],
        }

        with patch.dict(os.environ):
            failures, action_entries, ok_entries = self._run(
                manifest, tmp_path, current_os="macos")

        assert failures == []
        assert marker.read_text() == target
        assert any("env vars set: BOOTSTRAP_TEST_DEVROOT" in e for e in action_entries)

    def test_tilde_value_expands_to_home(self, isolated_home, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        manifest = {"env_vars": [{"name": "BOOTSTRAP_TEST_DEVROOT", "value": "~/Dev"}]}

        with patch.dict(os.environ):
            failures, _action, _ok = self._run(manifest, tmp_path)
            assert os.environ["BOOTSTRAP_TEST_DEVROOT"] == str(isolated_home / "Dev")

        assert failures == []
        content = (isolated_home / ".bashrc").read_text()
        assert export_line("BOOTSTRAP_TEST_DEVROOT", str(isolated_home / "Dev")) in content

    def test_exports_to_claude_env_file(self, isolated_home, tmp_path, monkeypatch):
        env_file = tmp_path / "claude-env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        manifest = {"env_vars": [{"name": "BOOTSTRAP_TEST_DEVROOT", "value": "~/Dev"}]}

        with patch.dict(os.environ):
            failures, _action, ok_entries = self._run(manifest, tmp_path)

        assert failures == []
        assert f"export BOOTSTRAP_TEST_DEVROOT={isolated_home / 'Dev'}\n" \
            in env_file.read_text()
        assert any("exported to CLAUDE_ENV_FILE" in e for e in ok_entries)

    def test_already_persisted_logs_ok_not_action(
        self, isolated_home, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        manifest = {"env_vars": [{"name": "BOOTSTRAP_TEST_DEVROOT", "value": "~/Dev"}]}

        with patch.dict(os.environ):
            self._run(manifest, tmp_path)  # first pass persists
            failures, action_entries, ok_entries = self._run(manifest, tmp_path)

        assert failures == []
        assert action_entries == []
        assert any(
            "env_var BOOTSTRAP_TEST_DEVROOT: ok" in e for e in ok_entries
        )

    def test_invalid_entry_records_failure(self, isolated_home, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        manifest = {"env_vars": [{"name": "BOOTSTRAP_TEST_DEVROOT"}]}

        with patch.dict(os.environ):
            failures, action_entries, _ok = self._run(manifest, tmp_path)

        assert len(failures) == 1
        assert failures[0]["type"] == "env_var"
        assert "needs string 'name' and 'value'" in failures[0]["message"]
        assert any("INVALID" in e for e in action_entries)

    def test_unnamed_invalid_entry_uses_placeholder(
        self, isolated_home, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("CLAUDE_ENV_FILE", raising=False)
        manifest = {"env_vars": [{"value": "~/Dev"}]}

        with patch.dict(os.environ):
            failures, _action, _ok = self._run(manifest, tmp_path)

        assert len(failures) == 1
        assert failures[0]["name"] == "(unnamed)"

    @pytest.mark.parametrize("name", ["PATH", "Path", "path"])
    def test_path_entry_is_rejected(
        self, isolated_home, tmp_path, monkeypatch, name
    ):
        """PATH (any case) is a hard failure: PATH edits belong exclusively
        to path_entries + tool->PATH linkage (spec directive 3). The guard
        fires before any export or persistence write."""
        env_file = tmp_path / "claude-env"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        manifest = {"env_vars": [{"name": name, "value": "/clobbered"}]}

        with patch.dict(os.environ):
            failures, action_entries, _ok = self._run(manifest, tmp_path)
            assert os.environ.get(name) != "/clobbered"

        assert len(failures) == 1
        assert failures[0]["type"] == "env_var"
        assert "path_entries" in failures[0]["message"]
        assert any("REFUSED" in e for e in action_entries)
        assert not env_file.exists()  # no CLAUDE_ENV_FILE export happened
        assert not (isolated_home / ".bashrc").exists()  # no persistence


class TestEnvVarsLayerMerge:
    """env_vars merges across manifest layers by name (identity-keyed)."""

    def test_higher_layer_overrides_value_by_name(self):
        from bootstrap_lib.manifest_merge import merge_manifests

        base = {"env_vars": [{"name": "DEVROOT", "value": "~/Dev"}]}
        override = {"env_vars": [{"name": "DEVROOT", "value": "C:/dev"}]}

        merged = merge_manifests(base, override)

        assert merged["env_vars"] == [{"name": "DEVROOT", "value": "C:/dev"}]

    def test_disjoint_names_union(self):
        from bootstrap_lib.manifest_merge import merge_manifests

        base = {"env_vars": [{"name": "DEVROOT", "value": "~/Dev"}]}
        override = {"env_vars": [{"name": "OTHER", "value": "x"}]}

        merged = merge_manifests(base, override)

        assert [e["name"] for e in merged["env_vars"]] == ["DEVROOT", "OTHER"]
