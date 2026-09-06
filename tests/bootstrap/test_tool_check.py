"""Tests for bootstrap lib/tool_check.py."""

import os
import stat
import sys
from unittest.mock import patch

import bootstrap_lib.tool_check as tool_check
from bootstrap_lib.tool_check import check_tool, run_install


class TestCheckTool:
    def test_installed_tool_passes(self):
        result = check_tool("python3")
        assert result.passed is True
        assert result.subject == "python3"
        assert "found at" in result.message

    def test_missing_tool_fails(self):
        result = check_tool("nonexistent_xyz_tool_abc")
        assert result.passed is False
        assert result.subject == "nonexistent_xyz_tool_abc"
        assert result.install_cmd is None

    def test_missing_tool_includes_install_cmd(self):
        install_cmds = {"macos": "brew install fake", "ubuntu": "apt install fake"}
        result = check_tool("nonexistent_xyz", install_cmds=install_cmds, current_os="macos")
        assert result.passed is False
        assert result.install_cmd == "brew install fake"

    def test_missing_tool_no_install_for_unknown_os(self):
        install_cmds = {"macos": "brew install fake"}
        result = check_tool("nonexistent_xyz", install_cmds=install_cmds, current_os="freebsd")
        assert result.passed is False
        assert result.install_cmd is None


class TestInstallPath:
    """Tests for the install_path parameter of check_tool."""

    def test_found_in_install_path(self, tmp_path):
        """Tool binary found in install_path passes."""
        tool_name = "mytool"
        tool_file = tmp_path / tool_name
        tool_file.write_text("#!/bin/sh\necho hi")
        tool_file.chmod(tool_file.stat().st_mode | stat.S_IEXEC)

        result = check_tool(tool_name, install_path=str(tmp_path))
        assert result.passed is True
        assert str(tmp_path) in result.message

    def test_found_exe_in_install_path(self, tmp_path):
        """On Windows/MSYSTEM, tool.exe found in install_path passes."""
        tool_name = "mytool"
        exe_file = tmp_path / (tool_name + ".exe")
        exe_file.write_text("fake exe")

        # This test relies on the platform check in check_tool;
        # on non-Windows the .exe candidate is skipped, so place the bare name too
        bare_file = tmp_path / tool_name
        bare_file.write_text("fake binary")

        result = check_tool(tool_name, install_path=str(tmp_path))
        assert result.passed is True

    def test_install_path_missing_dir(self, tmp_path):
        """install_path pointing to nonexistent dir falls through to which."""
        result = check_tool("nonexistent_xyz_tool", install_path=str(tmp_path / "nope"))
        assert result.passed is False

    def test_install_path_empty_dir(self, tmp_path):
        """install_path exists but tool binary not in it falls through."""
        result = check_tool("nonexistent_xyz_tool", install_path=str(tmp_path))
        assert result.passed is False

    def test_install_path_with_tilde(self, tmp_path, monkeypatch):
        """Tilde in install_path is expanded."""
        tool_name = "tildetool"
        tool_file = tmp_path / tool_name
        tool_file.write_text("#!/bin/sh\necho hi")
        tool_file.chmod(tool_file.stat().st_mode | stat.S_IEXEC)

        monkeypatch.setenv("HOME", str(tmp_path))
        # Also set USERPROFILE for Windows expanduser
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        result = check_tool(tool_name, install_path="~")
        assert result.passed is True

    def test_which_fallback_when_install_path_misses(self):
        """Even with install_path set, falls back to which for system tools."""
        result = check_tool("python3", install_path="/nonexistent/path")
        assert result.passed is True
        assert "found at" in result.message


class TestInstallPathList:
    """installPath may be a list of candidate directories; first hit wins."""

    def test_first_candidate_hits(self, tmp_path):
        d1 = tmp_path / "a"
        d1.mkdir()
        tool = d1 / "mytool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        result = check_tool("mytool", install_path=[str(d1), str(tmp_path / "b")])
        assert result.passed is True
        assert str(d1) in result.message

    def test_second_candidate_hits(self, tmp_path):
        d1 = tmp_path / "a"
        d1.mkdir()
        d2 = tmp_path / "b"
        d2.mkdir()
        tool = d2 / "mytool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        result = check_tool("mytool", install_path=[str(d1), str(d2)])
        assert result.passed is True
        assert str(d2) in result.message

    def test_no_candidate_hits_falls_through(self, tmp_path):
        result = check_tool(
            "nonexistent_xyz_tool",
            install_path=[str(tmp_path / "a"), str(tmp_path / "b")],
        )
        assert result.passed is False

    def test_env_var_expansion(self, tmp_path, monkeypatch):
        d = tmp_path / "progs"
        d.mkdir()
        tool = d / "mytool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("MYPROGS", str(d))
        result = check_tool("mytool", install_path="$MYPROGS")
        assert result.passed is True


class TestCheckCommand:
    """A `check` command resolves a tool when its exit code is 0."""

    def test_check_cmd_pass(self):
        result = check_tool("whatever_name", check_cmd="exit 0")
        assert result.passed is True
        assert result.on_path is True  # no concrete dir to link

    def test_check_cmd_fail_falls_through_to_which(self):
        # check fails, name not on PATH -> overall fail
        result = check_tool("nonexistent_xyz_tool", check_cmd="exit 1")
        assert result.passed is False

    def test_install_path_takes_priority_over_check(self, tmp_path):
        tool = tmp_path / "mytool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        # install_path hits first; check_cmd (would fail) never runs
        result = check_tool("mytool", install_path=str(tmp_path), check_cmd="exit 1")
        assert result.passed is True


class TestOnPath:
    """on_path reports whether the resolved tool is reachable by bare name."""

    def test_which_resolution_is_on_path(self):
        result = check_tool("python3")
        assert result.passed is True
        assert result.on_path is True

    def test_install_path_off_path_reports_false(self, tmp_path, monkeypatch):
        # A tool found only via install_path in a dir NOT on PATH -> on_path False
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
        tool = tmp_path / "offpathtool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        result = check_tool("offpathtool", install_path=str(tmp_path))
        assert result.passed is True
        assert result.on_path is False

    def test_install_path_on_path_reports_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path))
        tool = tmp_path / "onpathtool"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
        result = check_tool("onpathtool", install_path=str(tmp_path))
        assert result.passed is True
        assert result.on_path is True

    def test_case_differing_path_entry_uses_shared_normalizer(self, monkeypatch):
        monkeypatch.setattr(
            tool_check, "normalize_path_for_compare", lambda path: path.casefold(),
            raising=False,
        )
        monkeypatch.setenv("PATH", "/Opt/Tool")

        assert tool_check._dir_on_path("/opt/tool") is True


class TestRunInstall:
    def test_run_install_closes_stdin_and_bounds_timeout(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            return 0, "stdout", "stderr"

        monkeypatch.setattr(tool_check, "run_captured", fake_run, raising=False)

        ok, output = run_install("echo ok", timeout=47)

        assert ok is True
        assert output == "stdoutstderr"
        assert seen["stdin_devnull"] is True
        assert seen["timeout"] == 47

    def test_success(self):
        ok, output = run_install("echo ok")
        assert ok is True

    def test_failure(self):
        ok, output = run_install("false")
        assert ok is False

    def test_output_captured(self):
        ok, output = run_install("echo hello")
        assert ok is True
        assert "hello" in output
