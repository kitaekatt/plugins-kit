"""Wiring tests for the elevated-command deferral in _strategy_install_command.

Sequence step 8 source (b): an ``install.<os>`` opaque command declaring
``elevated: true`` runs DIRECTLY when privileges are available (unchanged
behavior) but is DEFERRED -- never attempted -- when they are missing, producing
a needs_elevation failure carrying a {"method": "command", ...} descriptor for
the elevation queue. A command WITHOUT the elevated flag (or with it omitted --
audit note N2) always runs directly, exactly as today.
"""

import bootstrap_lib.engine as engine
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths


def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


def _priv(monkeypatch, available):
    monkeypatch.setattr(engine, "_privileges_available", lambda current_os: available)


class TestElevatedFlagParsing:
    def _ctx(self, tool_def, current_os):
        tool_def = engine._normalize_tool_entry(tool_def, current_os)
        return engine._ToolEntryCtx(tool_def, current_os, "", [], [], [], plugin_name="p")

    def test_elevated_true_object(self):
        ctx = self._ctx(
            {"name": "t", "install": {"ubuntu": {"command": "x", "elevated": True}}}, "ubuntu")
        assert ctx.elevated is True

    def test_elevated_omitted_defaults_false_N2(self):
        # An author-written command object may omit `elevated` -> False (N2).
        ctx = self._ctx(
            {"name": "t", "install": {"ubuntu": {"command": "x"}}}, "ubuntu")
        assert ctx.elevated is False

    def test_bare_string_install_is_not_elevated(self):
        # Normalizes to {"command": s, "elevated": False}.
        ctx = self._ctx({"name": "t", "install": {"ubuntu": "run-me"}}, "ubuntu")
        assert ctx.elevated is False


class TestElevatedCommandDeferral:
    def test_defers_when_unprivileged_ubuntu(self, monkeypatch):
        _stub(monkeypatch)
        _priv(monkeypatch, available=False)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool absent
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("elevated command must not run when unprivileged")))

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "chrome", "install": {"ubuntu": {"command": "curl x | sh", "elevated": True}}},
            "ubuntu", "/data", "", action_entries, [], [], plugin_name="p",
        )
        assert failure is not None
        assert failure["install_state"] == "needs_elevation"
        assert failure["install_cmd"] is None
        assert failure["persist_across_sessions"] is True
        assert failure["elevation"] == {
            "method": "command", "command": "curl x | sh", "os": "ubuntu"}
        assert engine._is_auto_fixable(failure) is False
        assert any("needs elevation" in a for a in action_entries)

    def test_runs_directly_when_privileged(self, monkeypatch):
        _stub(monkeypatch)
        _priv(monkeypatch, available=True)
        calls = []

        def fake_run(cmd):
            calls.append(cmd)
            return True, "ok"

        monkeypatch.setattr(tool_check, "run_install", fake_run)
        # First _tool_check (resolve) fails, second (post-install) passes.
        seq = iter([False, True])

        def fake_check(ctx):
            from bootstrap_lib.result import Result
            passed = next(seq)
            return Result(passed=passed, subject="chrome",
                          message="ok" if passed else "absent",
                          remediation_cmd=None,
                          extras={"path": "/usr/bin/chrome" if passed else None,
                                  "install_cmd": "curl x | sh", "on_path": True})

        monkeypatch.setattr(engine, "_tool_check", fake_check)

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "chrome", "install": {"ubuntu": {"command": "curl x | sh", "elevated": True}}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert calls == ["curl x | sh"]
        assert tools_installed and "chrome" == tools_installed[0][0]

    def test_non_elevated_command_runs_even_when_unprivileged_N3(self, monkeypatch):
        # elevated=False (or omitted): today's behavior -- the command runs
        # directly regardless of privilege. No manifest declares elevated yet, so
        # this path is unchanged.
        _stub(monkeypatch)
        _priv(monkeypatch, available=False)
        calls = []
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (calls.append(cmd), (True, "ok"))[1])

        seq = iter([False, True])

        def fake_check(ctx):
            from bootstrap_lib.result import Result
            passed = next(seq)
            return Result(passed=passed, subject="w3m",
                          message="ok" if passed else "absent",
                          remediation_cmd=None,
                          extras={"path": "/usr/bin/w3m" if passed else None,
                                  "install_cmd": "make-w3m", "on_path": True})

        monkeypatch.setattr(engine, "_tool_check", fake_check)

        failure = engine._process_tool_entry(
            {"name": "w3m", "install": {"ubuntu": {"command": "make-w3m"}}},
            "ubuntu", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure is None
        assert calls == ["make-w3m"]

    def test_defers_when_unprivileged_windows(self, monkeypatch):
        _stub(monkeypatch)
        _priv(monkeypatch, available=False)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("elevated command must not run when unprivileged")))

        failure = engine._process_tool_entry(
            {"name": "cap", "install": {"windows": {"command": "Enable-Feature X", "elevated": True}}},
            "windows", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "needs_elevation"
        assert failure["elevation"]["os"] == "windows"
