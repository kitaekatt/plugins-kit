"""Tests for tool->PATH linkage and install/exit-code reconciliation.

Covers the path-reachability behavior added per
docs/planning/bootstrap/path-reachability-check.md:
  - a tool found on disk but not on PATH gets its dir auto-added to PATH
  - winget-style "already installed" (install exits nonzero, re-check passes)
    is treated as installed, not install_failed
"""

import os

import bootstrap_lib.engine as engine
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths
from bootstrap_lib.tool_check import _tool_result


class TestLinkToolDirToPath:
    def test_off_path_tool_dir_added(self, monkeypatch):
        calls = []
        monkeypatch.setattr(path_check, "add_path_to_shell_config",
                            lambda d: calls.append(d) or (True, "added to .bashrc"))
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin"]))
        actions = []
        result = _tool_result("draw.io", True, "found",
                              path="/c/Program Files/draw.io/draw.io.exe", on_path=False)
        engine._link_tool_dir_to_path(result, "", actions)
        assert calls == ["/c/Program Files/draw.io"]
        assert any("on disk but not on PATH" in a for a in actions)
        # live process PATH now contains the dir
        assert "/c/Program Files/draw.io" in os.environ["PATH"]

    def test_on_path_tool_is_noop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(path_check, "add_path_to_shell_config",
                            lambda d: calls.append(d) or (True, "x"))
        actions = []
        result = _tool_result("git", True, "found",
                              path="/usr/bin/git", on_path=True)
        engine._link_tool_dir_to_path(result, "", actions)
        assert calls == []
        assert actions == []

    def test_check_resolved_no_path_is_noop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(path_check, "add_path_to_shell_config",
                            lambda d: calls.append(d) or (True, "x"))
        actions = []
        # check-cmd resolution: passed, on_path True, no concrete path
        result = _tool_result("appy", True, "check passed",
                              path=None, on_path=True)
        engine._link_tool_dir_to_path(result, "", actions)
        assert calls == []
        assert actions == []


class TestProcessToolEntry:
    @staticmethod
    def _stub(monkeypatch):
        """Neutralize side effects: PATH writes, tool_paths state, repair_path."""
        monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
        monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
        monkeypatch.setattr(path_repair, "repair_path", lambda: None)

    def test_resolved_off_path_links_and_records(self, tmp_path, monkeypatch):
        # tool present on disk in a dir not on PATH
        tool = tmp_path / "drawio"
        tool.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PATH", "/usr/bin")
        added = []
        monkeypatch.setattr(path_check, "add_path_to_shell_config",
                            lambda d: added.append(d) or (True, "added"))
        recorded = []
        monkeypatch.setattr(tool_paths, "record",
                            lambda dd, n, p: recorded.append((n, p)))

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "drawio", "installPath": str(tmp_path)},
            "linux", str(tmp_path), "", action_entries, ok_entries,
            tools_installed, plugin_name="bootstrap",
        )
        assert failure is None
        assert added == [str(tmp_path)]
        assert recorded and recorded[0][0] == "drawio"
        assert any("on disk but not on PATH" in a for a in action_entries)
        assert any("drawio: ok" in e for e in ok_entries)

    # The install-reconciliation tests stub run_install with a Python side
    # effect (create / don't create the binary) and let the REAL check_tool
    # resolve it via installPath. This exercises the exit-code-vs-recheck policy
    # deterministically without depending on a shell `touch`.
    def test_install_nonzero_but_recheck_passes(self, tmp_path, monkeypatch):
        """winget exit 43: install exits nonzero but the binary is now present."""
        self._stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tmp dir deliberately off PATH

        def fake_install(cmd):
            (tmp_path / "drawio").write_text("#!/bin/sh\n")  # appears...
            return (False, "No available upgrade found")     # ...but exit nonzero
        monkeypatch.setattr(tool_check, "run_install", fake_install)

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "drawio", "installPath": str(tmp_path),
             "install": {"linux": "pkg install drawio"}},
            "linux", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="bootstrap",
        )
        assert failure is None
        assert tools_installed and tools_installed[0][0] == "drawio"
        assert "already present after" in tools_installed[0][1]
        assert not any("install command failed" in a for a in action_entries)

    def test_install_fails_and_recheck_fails(self, tmp_path, monkeypatch):
        """Genuine failure: install errors AND the binary never appears."""
        self._stub(monkeypatch)
        monkeypatch.setattr(tool_check, "run_install", lambda cmd: (False, "error: boom"))

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "ghost", "installPath": str(tmp_path),
             "install": {"linux": "pkg install ghost"}},
            "linux", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="bootstrap",
        )
        assert failure is not None
        assert failure["install_state"] == "install_failed"
        assert any("install command failed" in a for a in action_entries)

    def test_installed_but_path_stale(self, tmp_path, monkeypatch):
        """install exits 0 but the binary still isn't findable."""
        self._stub(monkeypatch)
        monkeypatch.setattr(tool_check, "run_install", lambda cmd: (True, "Successfully installed"))

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "ghost", "installPath": str(tmp_path),
             "install": {"linux": "pkg install ghost"}},
            "linux", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="bootstrap",
        )
        assert failure is not None
        assert failure["install_state"] == "installed_but_path_stale"

    def test_manual_sentinel_not_executed(self, tmp_path, monkeypatch):
        """A missing tool whose install is the "manual" sentinel must NOT run
        `manual` as a command — it surfaces as a manual-attention failure."""
        self._stub(monkeypatch)
        # Fail loudly if the engine ever shells out for a "manual" tool.
        def boom(cmd):
            raise AssertionError(f"run_install should not be called for manual; got {cmd!r}")
        monkeypatch.setattr(tool_check, "run_install", boom)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool deliberately absent

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "p4", "install": {"linux": "manual"}},
            "linux", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="p4-kit",
        )
        assert failure is not None
        assert failure["install_state"] == "manual_install"
        # No runnable command -> not fix-all eligible.
        assert failure["install_cmd"] is None
        assert engine._is_auto_fixable(failure) is False
        assert any("manual install required" in a for a in action_entries)
        # The bogus "install command failed - `manual`" line must be gone.
        assert not any("install command failed" in a for a in action_entries)

    def test_manual_fix_all_message(self, tmp_path):
        """The fix-all directive for a manual tool guides the user to install it
        manually — it never tells them to re-run the bogus `manual` command."""
        import json
        failure = {
            "type": "tool", "name": "p4", "message": "not found in PATH",
            "install_state": "manual_install", "install_cmd": None, "plugin": "p4-kit",
        }
        out_pending = tmp_path / "bootstrap_display.pending"
        engine.emit_failure_response(
            [failure], "linux", "log", label="bootstrap",
            output_file=str(out_pending),
        )
        ac = json.loads(out_pending.read_text())["hookSpecificOutput"]["additionalContext"]
        assert "Install p4 [p4-kit] manually" in ac
        assert "on PATH" in ac
        assert "`manual`" not in ac
        assert engine._is_auto_fixable(failure) is False

    def test_scoop_fulfillment_installs_and_records(self, tmp_path, monkeypatch):
        """A `scoop` download fulfillment provisions Scoop (lazily) then installs
        the package via Scoop, recording the shim path."""
        self._stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # p4 deliberately absent
        import bootstrap_lib.scoop as scoop_mod
        calls = {}
        monkeypatch.setattr(scoop_mod, "ensure_scoop",
                            lambda: scoop_mod.ScoopResult(True, None, "already installed"))
        def fake_install(pkg, tool_name=None):
            calls["pkg"], calls["tool"] = pkg, tool_name
            return scoop_mod.ScoopResult(True, str(tmp_path / "p4.exe"),
                                         f"installed {pkg} via scoop")
        monkeypatch.setattr(scoop_mod, "scoop_install", fake_install)

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "p4", "download": {"windows": {"scoop": "main/p4"}}},
            "windows", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="p4-kit",
        )
        assert failure is None
        assert calls == {"pkg": "main/p4", "tool": "p4"}
        assert tools_installed and "via scoop" in tools_installed[0][1]

    def test_scoop_unavailable_is_failure_no_pkg_install(self, tmp_path, monkeypatch):
        """If Scoop can't be provisioned, don't try to install the package; emit a
        non-auto-fixable scoop_failed failure."""
        self._stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))
        import bootstrap_lib.scoop as scoop_mod
        monkeypatch.setattr(scoop_mod, "ensure_scoop",
                            lambda: scoop_mod.ScoopResult(False, None, "scoop install failed"))
        def boom(*a, **k):
            raise AssertionError("must not install a package when scoop is unavailable")
        monkeypatch.setattr(scoop_mod, "scoop_install", boom)

        action_entries, ok_entries, tools_installed = [], [], []
        failure = engine._process_tool_entry(
            {"name": "p4", "download": {"windows": {"scoop": "main/p4"}}},
            "windows", "/data", "", action_entries, ok_entries,
            tools_installed, plugin_name="p4-kit",
        )
        assert failure is not None
        assert failure["install_state"] == "scoop_failed"
        assert failure["install_cmd"] is None
        assert engine._is_auto_fixable(failure) is False
