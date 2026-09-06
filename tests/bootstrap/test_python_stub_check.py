"""Tests for bootstrap lib/python_stub_check.py."""

import os
from unittest.mock import patch

from bootstrap_lib.python_stub_check import (
    check_python_stub,
    write_fix_script,
    _BATCH_TEMPLATE,
    _find_first_python_in_dirs,
)


class TestCheckPythonStub:
    def test_passes_on_non_windows(self):
        """check_python_stub returns passed=True on non-Windows."""
        # Ensure no MSYSTEM env var so we hit the non-windows branch
        env_no_msystem = {k: v for k, v in os.environ.items() if k != "MSYSTEM"}
        with patch.dict(os.environ, env_no_msystem, clear=True), \
             patch("bootstrap_lib.python_stub_check.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = check_python_stub("~/standalone/python", ["WindowsApps"])
        assert result.passed is True
        assert "not windows" in result.message
        assert result.bad_python is None

    def test_passes_when_good_python_first(self, tmp_path):
        """When the first python.exe on persistent PATH is inside good_python_dir, pass."""
        good_dir = tmp_path / "standalone" / "python"
        good_dir.mkdir(parents=True)
        good_python = good_dir / "python.exe"
        good_python.write_text("")  # placeholder

        with patch("bootstrap_lib.python_stub_check.sys") as mock_sys, \
             patch("bootstrap_lib.python_stub_check._get_persistent_path_dirs") as mock_dirs, \
             patch("bootstrap_lib.python_stub_check._find_first_python_in_dirs") as mock_find:
            mock_sys.platform = "win32"
            mock_dirs.return_value = [str(good_dir), r"C:\Windows\System32"]
            mock_find.return_value = str(good_python)
            result = check_python_stub(str(good_dir), ["WindowsApps"])

        assert result.passed is True
        assert "good python" in result.message
        assert result.bad_python is None

    def test_fails_on_windowsapps_stub(self, tmp_path):
        """When the first python.exe on persistent PATH is in WindowsApps, fail."""
        good_dir = str(tmp_path / "standalone" / "python")
        bad_path = r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps\python.exe"

        with patch("bootstrap_lib.python_stub_check.sys") as mock_sys, \
             patch("bootstrap_lib.python_stub_check._get_persistent_path_dirs") as mock_dirs, \
             patch("bootstrap_lib.python_stub_check._find_first_python_in_dirs") as mock_find:
            mock_sys.platform = "win32"
            mock_dirs.return_value = [r"C:\Users\someone\AppData\Local\Microsoft\WindowsApps", good_dir]
            mock_find.return_value = bad_path
            result = check_python_stub(good_dir, ["WindowsApps"])

        assert result.passed is False
        assert result.bad_python == bad_path
        assert "WindowsApps" in result.message
        assert "shadows" in result.message

    def test_passes_for_unrelated_python(self, tmp_path):
        """A non-stub, non-good python (e.g. C:\\Python311\\python.exe) passes."""
        good_dir = str(tmp_path / "standalone" / "python")
        other_python = r"C:\Python311\python.exe"

        with patch("bootstrap_lib.python_stub_check.sys") as mock_sys, \
             patch("bootstrap_lib.python_stub_check._get_persistent_path_dirs") as mock_dirs, \
             patch("bootstrap_lib.python_stub_check._find_first_python_in_dirs") as mock_find:
            mock_sys.platform = "win32"
            mock_dirs.return_value = [r"C:\Python311", good_dir]
            mock_find.return_value = other_python
            result = check_python_stub(good_dir, ["WindowsApps"])

        assert result.passed is True
        assert "non-stub python" in result.message
        assert result.bad_python is None

    def test_passes_when_no_python_on_path(self, tmp_path):
        """When no python.exe is found on persistent PATH, pass (not our concern)."""
        good_dir = str(tmp_path / "standalone" / "python")

        with patch("bootstrap_lib.python_stub_check.sys") as mock_sys, \
             patch("bootstrap_lib.python_stub_check._get_persistent_path_dirs") as mock_dirs, \
             patch("bootstrap_lib.python_stub_check._find_first_python_in_dirs") as mock_find:
            mock_sys.platform = "win32"
            mock_dirs.return_value = [r"C:\Windows\System32"]
            mock_find.return_value = None
            result = check_python_stub(good_dir, ["WindowsApps"])

        assert result.passed is True
        assert "no python.exe on persistent PATH" in result.message
        assert result.bad_python is None

    def test_passes_when_no_persistent_path(self, tmp_path):
        """When the registry has no PATH at all, pass (can't make a determination)."""
        good_dir = str(tmp_path / "standalone" / "python")

        with patch("bootstrap_lib.python_stub_check.sys") as mock_sys, \
             patch("bootstrap_lib.python_stub_check._get_persistent_path_dirs") as mock_dirs:
            mock_sys.platform = "win32"
            mock_dirs.return_value = []
            result = check_python_stub(good_dir, ["WindowsApps"])

        assert result.passed is True
        assert "no persistent PATH found" in result.message
        assert result.bad_python is None


class TestFindFirstPythonInDirs:
    def test_returns_first_python_exe_found(self, tmp_path):
        """_find_first_python_in_dirs walks dirs in order, returns first match."""
        d1 = tmp_path / "first"
        d2 = tmp_path / "second"
        d1.mkdir()
        d2.mkdir()
        # Only d2 has python.exe
        (d2 / "python.exe").write_text("")

        result = _find_first_python_in_dirs([str(d1), str(d2)])
        assert result == str(d2 / "python.exe")

    def test_returns_none_when_no_python_anywhere(self, tmp_path):
        """Returns None when no dir has python.exe."""
        d1 = tmp_path / "empty1"
        d2 = tmp_path / "empty2"
        d1.mkdir()
        d2.mkdir()
        result = _find_first_python_in_dirs([str(d1), str(d2)])
        assert result is None

    def test_skips_nonexistent_dirs(self, tmp_path):
        """Nonexistent directories are skipped without error."""
        d = tmp_path / "real"
        d.mkdir()
        (d / "python.exe").write_text("")
        result = _find_first_python_in_dirs(
            [r"C:\does\not\exist", "", str(d)]
        )
        assert result == str(d / "python.exe")

    def test_returns_python_exe_before_python3_exe(self, tmp_path):
        """python.exe is preferred over python3.exe in the same dir."""
        d = tmp_path / "both"
        d.mkdir()
        (d / "python.exe").write_text("")
        (d / "python3.exe").write_text("")
        result = _find_first_python_in_dirs([str(d)])
        assert result == str(d / "python.exe")

    def test_searches_for_python_exe_across_all_dirs_before_python3(self, tmp_path):
        """python3.exe in an earlier dir must not hide python.exe later."""
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        (first / "python3.exe").write_text("")
        (second / "python.exe").write_text("")

        result = _find_first_python_in_dirs([str(first), str(second)])

        assert result == str(second / "python.exe")


class TestWriteFixScript:
    def test_writes_bat_file(self, tmp_path):
        """write_fix_script creates fix_python_path.bat in the output dir."""
        good_dir = "~/standalone/python"
        ok, msg, script_path = write_fix_script(good_dir, str(tmp_path))

        assert ok is True
        assert script_path.endswith("fix_python_path.bat")
        assert os.path.exists(script_path)
        assert "wrote" in msg

    def test_substitutes_good_python_dir(self, tmp_path):
        """The bat file contains the expanded good_python_dir with backslashes."""
        good_dir = str(tmp_path / "standalone" / "python")
        ok, _msg, script_path = write_fix_script(good_dir, str(tmp_path))
        assert ok is True

        with open(script_path, "r") as f:
            content = f.read()

        # Windows-style path with backslashes
        win_good = good_dir.replace("/", "\\")
        assert win_good in content
        # Placeholder should not remain
        assert "__GOOD_PYTHON_DIR__" not in content

    def test_contains_admin_detection(self, tmp_path):
        """The bat file uses fsutil dirty query for admin detection."""
        ok, _msg, script_path = write_fix_script("~/standalone/python", str(tmp_path))
        assert ok is True
        with open(script_path, "r") as f:
            content = f.read()
        assert "fsutil dirty query" in content

    def test_contains_self_elevation(self, tmp_path):
        """The bat file uses Start-Process -Verb RunAs for self-elevation."""
        ok, _msg, script_path = write_fix_script("~/standalone/python", str(tmp_path))
        assert ok is True
        with open(script_path, "r") as f:
            content = f.read()
        assert "Start-Process -FilePath '%~f0' -Verb RunAs" in content

    def test_contains_labels(self, tmp_path):
        """The bat file contains the :not_admin and :is_admin labels."""
        ok, _msg, script_path = write_fix_script("~/standalone/python", str(tmp_path))
        assert ok is True
        with open(script_path, "r") as f:
            content = f.read()
        assert ":not_admin" in content
        assert ":is_admin" in content

    def test_overwrites_existing_file(self, tmp_path):
        """write_fix_script is idempotent — overwrites an existing file."""
        # First write
        write_fix_script("~/standalone/python", str(tmp_path))
        script_path = os.path.join(str(tmp_path), "fix_python_path.bat")

        # Stomp it with garbage
        with open(script_path, "w") as f:
            f.write("garbage content")

        # Re-write
        ok, _msg, _path = write_fix_script("~/standalone/python", str(tmp_path))
        assert ok is True

        with open(script_path, "r") as f:
            content = f.read()
        assert "garbage content" not in content
        assert "fsutil dirty query" in content

    def test_uses_crlf_line_endings(self, tmp_path):
        """The bat file is written with CRLF line endings."""
        ok, _msg, script_path = write_fix_script("~/standalone/python", str(tmp_path))
        assert ok is True

        # Read in binary mode to inspect raw bytes
        with open(script_path, "rb") as f:
            raw = f.read()

        # Must contain at least one CRLF, must not contain bare LF without preceding CR
        assert b"\r\n" in raw
        # Count CRLFs vs LFs — every LF should be preceded by CR
        lf_count = raw.count(b"\n")
        crlf_count = raw.count(b"\r\n")
        assert lf_count == crlf_count, "Found bare LFs without preceding CR"

    def test_creates_output_dir_if_missing(self, tmp_path):
        """write_fix_script creates the output directory if it doesn't exist."""
        nonexistent = tmp_path / "deep" / "nested" / "out"
        ok, _msg, script_path = write_fix_script("~/standalone/python", str(nonexistent))
        assert ok is True
        assert os.path.exists(script_path)


class TestBatchTemplate:
    def test_template_has_placeholder(self):
        """Sanity: the template still has the substitution placeholder."""
        assert "__GOOD_PYTHON_DIR__" in _BATCH_TEMPLATE

    def test_template_has_self_delete(self):
        """The bat file deletes itself after a successful run."""
        assert '(goto) 2>nul & del "%~f0"' in _BATCH_TEMPLATE

    def test_template_skips_self_delete_on_powershell_error(self):
        """If PowerShell exits non-zero, the script does NOT delete itself."""
        assert "ERROR: PowerShell exited with error" in _BATCH_TEMPLATE
        # The error path exits with /b 3 BEFORE the (goto) self-delete trailer
        err_idx = _BATCH_TEMPLATE.index("ERROR: PowerShell exited with error")
        del_idx = _BATCH_TEMPLATE.index('(goto) 2>nul & del "%~f0"')
        exit_idx = _BATCH_TEMPLATE.index("exit /b 3", err_idx)
        assert exit_idx < del_idx, "error path must exit before the self-delete trailer"
