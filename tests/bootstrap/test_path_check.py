"""Tests for bootstrap lib/path_check.py."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from bootstrap_lib.path_check import (
    _add_path_to_windows_registry,
    _path_diagnostic,
    check_path_entry,
)


class TestCheckPathEntry:
    def test_existing_path_passes(self):
        # Use a directory known to be in PATH on both macOS/Linux and Windows
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        assert len(path_dirs) > 0, "PATH is empty"
        known_dir = path_dirs[0]
        result = check_path_entry(known_dir)
        assert result.passed is True
        assert result.subject == known_dir

    def test_missing_path_fails(self):
        result = check_path_entry("/nonexistent/path/xyz")
        assert result.passed is False
        assert "/nonexistent/path/xyz" in result.message

    def test_tilde_expansion(self):
        home = os.path.expanduser("~")
        # Add ~/.local/bin to PATH for this test if not present
        original_path = os.environ.get("PATH", "")
        local_bin = os.path.join(home, ".local", "bin")
        os.environ["PATH"] = local_bin + os.pathsep + original_path
        try:
            result = check_path_entry("~/.local/bin")
            assert result.passed is True
        finally:
            os.environ["PATH"] = original_path


class TestPathDiagnostic:
    def test_reports_length_and_count(self):
        path_value = "/usr/bin" + os.pathsep + "/usr/local/bin"
        with patch.dict(os.environ, {"PATH": path_value}, clear=True):
            diag = _path_diagnostic()
        assert f"PATH={len(path_value)} chars" in diag
        assert "2 entries" in diag

    def test_detects_system32_present(self):
        with patch.dict(
            os.environ,
            {"PATH": r"C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0"},
            clear=True,
        ):
            diag = _path_diagnostic()
        assert "System32=True" in diag
        assert "PowerShell=True" in diag

    def test_detects_system32_missing(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            diag = _path_diagnostic()
        assert "System32=False" in diag
        assert "PowerShell=False" in diag


@pytest.mark.skipif(
    sys.platform != "win32" and "MSYSTEM" not in os.environ,
    reason="winreg only available on Windows",
)
class TestAddPathToWindowsRegistry:
    """Tests for _add_path_to_windows_registry (mocked winreg)."""

    def _winreg_mocks(self, current_value="C:\\\\existing", value_type=None):
        """Build a mocked winreg module with QueryValueEx returning current_value."""
        import winreg as real_winreg

        if value_type is None:
            value_type = real_winreg.REG_EXPAND_SZ

        mock_winreg = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = real_winreg.HKEY_CURRENT_USER
        mock_winreg.KEY_READ = real_winreg.KEY_READ
        mock_winreg.KEY_WRITE = real_winreg.KEY_WRITE
        mock_winreg.REG_EXPAND_SZ = real_winreg.REG_EXPAND_SZ
        mock_winreg.REG_SZ = real_winreg.REG_SZ

        mock_key = MagicMock()
        mock_key.__enter__ = MagicMock(return_value=mock_key)
        mock_key.__exit__ = MagicMock(return_value=False)
        mock_winreg.OpenKey.return_value = mock_key
        mock_winreg.QueryValueEx.return_value = (current_value, value_type)
        return mock_winreg, mock_key

    def test_skip_registry_env_var(self, monkeypatch):
        monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
        ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is True
        assert "skipped" in msg

    def test_adds_new_entry(self, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
        mock_winreg, mock_key = self._winreg_mocks(current_value="C:\\Other\\dir")
        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("bootstrap_lib.path_check._broadcast_environment_change"):
            ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is True
        assert "added" in msg
        assert "registry" in msg
        # SetValueEx should have been called with the new path prepended.
        args, _ = mock_winreg.SetValueEx.call_args
        new_value = args[4]
        assert new_value.startswith(os.path.expanduser("~/.local/bin").replace("/", "\\"))
        assert "C:\\Other\\dir" in new_value

    def test_already_present_case_insensitive(self, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
        existing = os.path.expanduser("~/.local/bin").replace("/", "\\").upper()
        mock_winreg, mock_key = self._winreg_mocks(current_value=existing)
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is True
        assert "already" in msg
        mock_winreg.SetValueEx.assert_not_called()

    def test_already_present_trailing_slash(self, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
        existing = os.path.expanduser("~/.local/bin").replace("/", "\\") + "\\"
        mock_winreg, mock_key = self._winreg_mocks(current_value=existing)
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is True
        assert "already" in msg
        mock_winreg.SetValueEx.assert_not_called()

    def test_missing_path_value_creates_it(self, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
        mock_winreg, mock_key = self._winreg_mocks()
        mock_winreg.QueryValueEx.side_effect = FileNotFoundError()
        with patch.dict("sys.modules", {"winreg": mock_winreg}), \
             patch("bootstrap_lib.path_check._broadcast_environment_change"):
            ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is True
        # Should have written exactly the new path with no semicolon
        args, _ = mock_winreg.SetValueEx.call_args
        new_value = args[4]
        assert new_value == os.path.expanduser("~/.local/bin").replace("/", "\\")

    def test_open_key_failure_includes_diagnostic(self, monkeypatch):
        monkeypatch.delenv("BOOTSTRAP_SKIP_REGISTRY", raising=False)
        mock_winreg = MagicMock()
        mock_winreg.HKEY_CURRENT_USER = 0
        mock_winreg.KEY_READ = 0
        mock_winreg.KEY_WRITE = 0
        mock_winreg.OpenKey.side_effect = PermissionError("access denied")
        with patch.dict("sys.modules", {"winreg": mock_winreg}):
            ok, msg = _add_path_to_windows_registry("~/.local/bin")
        assert ok is False
        assert "failed to write Windows User PATH" in msg
        assert "diag:" in msg
        assert "PATH=" in msg


class TestAddPathToShellConfigWindowsIntegration:
    """Test that add_path_to_shell_config calls registry on Windows."""

    @patch("bootstrap_lib.path_check._add_path_to_windows_registry")
    def test_calls_registry_on_windows(self, mock_registry, tmp_path):
        from bootstrap_lib.path_check import add_path_to_shell_config
        mock_registry.return_value = (True, "added to registry")
        # Use a fake RC file path to prevent writing to real ~/.bashrc
        with patch.dict(os.environ, {"MSYSTEM": "MINGW64"}), \
             patch("bootstrap_lib.path_check.os.path.expanduser", side_effect=lambda p: str(tmp_path / p.lstrip("~/")) if p.startswith("~") else p):
            ok, msg = add_path_to_shell_config("/tmp/test_path_xyz_" + str(os.getpid()))
        mock_registry.assert_called_once()

    @patch("bootstrap_lib.path_check._add_path_to_windows_registry")
    def test_skips_registry_on_non_windows(self, mock_registry, tmp_path):
        from bootstrap_lib.path_check import add_path_to_shell_config
        # Ensure MSYSTEM is not set and sys.platform is not win32
        env = {k: v for k, v in os.environ.items() if k != "MSYSTEM"}
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_check.sys") as mock_sys, \
             patch("bootstrap_lib.path_check.os.path.expanduser", side_effect=lambda p: str(tmp_path / p.lstrip("~/")) if p.startswith("~") else p):
            mock_sys.platform = "linux"
            add_path_to_shell_config("/tmp/test_path_xyz_" + str(os.getpid()))
        mock_registry.assert_not_called()


class TestAddPathToShellConfigIdempotency:
    """Repeated calls must not append duplicate export lines."""

    def test_repeated_calls_dont_duplicate_export_line(self, tmp_path):
        from bootstrap_lib.path_check import add_path_to_shell_config

        bashrc = tmp_path / ".bashrc"
        bashrc.write_text("# existing config\n")
        fake_home = str(tmp_path)

        env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM",)}
        env["BOOTSTRAP_SKIP_REGISTRY"] = "1"
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_check.sys") as mock_sys, \
             patch("bootstrap_lib.path_check.os.path.expanduser",
                   side_effect=lambda p: p.replace("~", fake_home, 1) if p.startswith("~") else p):
            mock_sys.platform = "linux"
            for _ in range(5):
                add_path_to_shell_config("~/.local/share/node")

        content = bashrc.read_text()
        assert content.count("export PATH=") == 1, (
            f"Expected exactly one export line, got:\n{content}"
        )

    def test_idempotent_with_forward_slash_form_already_present(self, tmp_path, monkeypatch):
        """Existing $HOME/forward/slash line should suppress further writes
        even when expanduser returns backslashes (native Windows Python).

        chdir to tmp_path first: on POSIX the simulated backslash path is a
        RELATIVE filename (backslash is not a separator), so any file the
        function creates from it must land in the temp dir — without the
        chdir, a literal backslash-named file leaked into the repo root.
        """
        from bootstrap_lib.path_check import add_path_to_shell_config

        monkeypatch.chdir(tmp_path)
        bashrc = tmp_path / ".bashrc"
        bashrc.write_text(
            '# existing config\n'
            'export PATH="$HOME/.local/share/node:$PATH"\n'
        )
        fake_home = str(tmp_path).replace("/", "\\")

        env = {k: v for k, v in os.environ.items() if k not in ("MSYSTEM",)}
        env["BOOTSTRAP_SKIP_REGISTRY"] = "1"
        with patch.dict(os.environ, env, clear=True), \
             patch("bootstrap_lib.path_check.sys") as mock_sys, \
             patch("bootstrap_lib.path_check.os.path.expanduser",
                   side_effect=lambda p: (fake_home + p[1:].replace("/", "\\")) if p.startswith("~") else p):
            mock_sys.platform = "linux"
            add_path_to_shell_config("~/.local/share/node")

        assert bashrc.read_text().count("export PATH=") == 1
