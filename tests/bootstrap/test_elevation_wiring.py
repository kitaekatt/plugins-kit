"""Engine-integration tests for the step-8 elevation wiring.

Covers the three descriptor SOURCES as the engine emits them plus the pass-end
surfacing:
  * source (c): _strategy_brew signals a brew_installer descriptor when Homebrew
    is missing (and NOT when a present-brew install merely fails);
  * emit_failure_response renders the aggregated elevation_script item (its path
    + what it does) and classifies it manual-only (not fix-all);
  * next-session pickup: a pass whose deferred op has been satisfied harvests an
    empty queue and the stale remediation script is removed.
"""

import json
import os

import bootstrap_lib.engine as engine
import bootstrap_lib.brew as brew_mod
import bootstrap_lib.elevation as elev
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths


def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


# --------------------------------------------------------------------------- #
# Source (c): missing-brew installer signal
# --------------------------------------------------------------------------- #

class TestBrewInstallerSignal:
    def test_missing_brew_emits_brew_installer_descriptor(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: brew_mod.BrewResult(False, None, "Homebrew is not installed."))
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError("must not install when brew absent")))

        failure = engine._process_tool_entry(
            {"name": "direnv", "install": {"macos": {"brew": "direnv"}}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "brew_failed"
        assert failure["elevation"] == {"method": "brew_installer", "os": "macos"}
        # And the queue harvests it into the brew_installer flag.
        q = elev.queue_from_failures([failure], "macos")
        assert q.brew_installer is True

    def test_present_brew_install_failure_has_no_installer_descriptor(self, monkeypatch):
        # brew present but the formula install fails: NOT a missing-installer case.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: brew_mod.BrewResult(True, "/opt/homebrew/bin/brew", "already installed"))
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew_mod.BrewResult(False, None, "brew install nope failed"))

        failure = engine._process_tool_entry(
            {"name": "nope", "install": {"macos": {"brew": "nope"}}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "brew_failed"
        assert "elevation" not in failure


# --------------------------------------------------------------------------- #
# emit_failure_response rendering of the aggregated item
# --------------------------------------------------------------------------- #

class TestElevationScriptRendering:
    def test_failure_line_includes_script_path_and_is_manual(self, tmp_path, capsys):
        path = "/data/elevate/install-elevated.sh"
        q = elev.ElevationQueue(apt_packages=["net-tools"])
        agg = elev.elevation_script_failure(q, "ubuntu", path)
        # Not fix-all eligible: only the user can supply credentials.
        assert engine._is_auto_fixable(agg) is False

        engine.emit_failure_response(
            [agg], current_os="ubuntu", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        payload = json.loads(capsys.readouterr().out)
        ac = payload["hookSpecificOutput"]["additionalContext"]
        assert path in ac
        assert "sudo bash" in ac
        # All-manual footer wording (no fix-all-eligible items).
        assert "None of these are fix-all eligible" in ac


# --------------------------------------------------------------------------- #
# Step 7b fix-all interactive launch (_elevation_step) -- launch is MOCKED
# --------------------------------------------------------------------------- #

FAKE_BASH = "C:\\Program Files\\Git\\usr\\bin\\bash.exe"


def _args(data_dir, fix_all=False, console=False, background=False):
    import argparse
    return argparse.Namespace(
        data_dir=str(data_dir), project_dir=None, verbose=False,
        console=console, background=background, fix_all=fix_all)


def _win_failure():
    return {"type": "tool", "name": "cap", "install_state": "needs_elevation",
            "elevation": {"method": "command",
                          "command": "bash ~/x.sh fix", "os": "windows"}}


class TestFixAllInteractiveLaunch:
    def _pin_bash(self, monkeypatch):
        monkeypatch.setattr(elev, "resolve_bash", lambda: FAKE_BASH)

    def test_fix_all_launches_waits_and_spawns_recheck(self, tmp_path, monkeypatch, capsys):
        """fix-all + non-empty queue: launch, wait, then the re-check pass."""
        self._pin_bash(monkeypatch)
        launches = []
        monkeypatch.setattr(
            elev, "launch_elevation_script",
            lambda path, current_os, timeout=elev.ELEVATION_LAUNCH_TIMEOUT:
            (launches.append((path, current_os)),
             elev.LaunchResult(launched=True, succeeded=True, detail="exit code 0"))[1])
        rechecks = []
        monkeypatch.setattr(engine, "_spawn_recheck_pass",
                            lambda args, plugin_root: rechecks.append(plugin_root))

        failures = [_win_failure()]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")

        assert stopped is True            # caller returns; re-check pass owns output
        assert len(launches) == 1         # launched exactly once
        assert launches[0][1] == "windows"
        assert rechecks == ["/plugin/root"]
        # Success -> no aggregated elevation_script fallback item.
        assert all(f["type"] != "elevation_script" for f in failures)
        # Outcome reported (launched + exit status) on the console run.
        out = capsys.readouterr().out
        assert "elevation script completed successfully" in out
        assert "exit code 0" in out

    def test_decline_falls_back_to_manual_message_no_loop(self, tmp_path, monkeypatch):
        """UAC declined: fall back to today's message, never re-prompt."""
        self._pin_bash(monkeypatch)
        monkeypatch.setattr(
            elev, "launch_elevation_script",
            lambda *a, **k: elev.LaunchResult(
                launched=True, succeeded=False,
                detail="The operation was canceled by the user"))
        recheck_calls = []
        monkeypatch.setattr(engine, "_spawn_recheck_pass",
                            lambda *a, **k: recheck_calls.append(1))

        failures = [_win_failure()]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")

        assert stopped is False
        assert recheck_calls == []
        agg = [f for f in failures if f["type"] == "elevation_script"]
        assert len(agg) == 1
        assert agg[0]["user_msg"].startswith(
            "fix-all launched the elevation script but it did not complete "
            "(The operation was canceled by the user).")
        # Manual instruction remains as the fallback.
        assert "double-click" in agg[0]["agent_msg"]

    def test_timeout_falls_back_to_manual_message(self, tmp_path, monkeypatch):
        self._pin_bash(monkeypatch)
        monkeypatch.setattr(
            elev, "launch_elevation_script",
            lambda *a, **k: elev.LaunchResult(
                launched=True, succeeded=False,
                detail="timed out after 600s waiting for the elevated script"))

        failures = [_win_failure()]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")

        assert stopped is False
        agg = [f for f in failures if f["type"] == "elevation_script"][0]
        assert "timed out after 600s" in agg["user_msg"]

    def test_sessionstart_never_launches(self, tmp_path, monkeypatch):
        """A pass WITHOUT --fix-all (SessionStart/background) must never launch."""
        self._pin_bash(monkeypatch)
        monkeypatch.setattr(
            elev, "launch_elevation_script",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("SessionStart pass must never launch")))

        failures = [_win_failure()]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=False, background=True), "/plugin/root")

        assert stopped is False
        agg = [f for f in failures if f["type"] == "elevation_script"]
        assert len(agg) == 1
        # Unlaunched item: no launch-outcome prefix, and the agent_msg carries
        # the fix-all interactive re-run hint (the consent path).
        assert "did not complete" not in agg[0]["user_msg"]
        assert "--console --fix-all" in agg[0]["agent_msg"]

    def test_unix_fix_all_keeps_manual_message_only(self, tmp_path, monkeypatch):
        """No TTY for sudo in the fix-all run: unix keeps message-only behavior
        (launch_elevation_script returns None -- exercised for real here)."""
        failures = [{"type": "tool", "name": "net-tools",
                     "install_state": "needs_elevation",
                     "elevation": {"method": "apt", "package": "net-tools",
                                   "os": "ubuntu"}}]
        recheck_calls = []
        monkeypatch.setattr(engine, "_spawn_recheck_pass",
                            lambda *a, **k: recheck_calls.append(1))

        stopped = engine._elevation_step(
            failures, "ubuntu", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")

        assert stopped is False
        assert recheck_calls == []
        agg = [f for f in failures if f["type"] == "elevation_script"][0]
        assert "did not complete" not in agg["user_msg"]
        assert "sudo bash" in agg["agent_msg"]

    def test_empty_queue_is_noop(self, tmp_path):
        failures = [{"type": "tool", "name": "x", "install_state": "install_failed"}]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")
        assert stopped is False
        assert all(f["type"] != "elevation_script" for f in failures)


class TestSpawnRecheckPass:
    def test_recheck_drops_fix_all_and_carries_mode(self, tmp_path, monkeypatch):
        """The re-check child re-runs the engine in the same mode WITHOUT
        --fix-all -- the loop guard against re-prompting."""
        import subprocess as _sp
        calls = []
        monkeypatch.setattr(_sp, "run", lambda cmd, **k: calls.append(cmd))

        args = _args(tmp_path, fix_all=True, console=True)
        args.project_dir = "/proj"
        engine._spawn_recheck_pass(args, "/plugin/root")

        assert len(calls) == 1
        cmd = calls[0]
        assert "--fix-all" not in cmd
        assert "--console" in cmd
        assert "--background" not in cmd
        assert cmd[cmd.index("--project-dir") + 1] == "/proj"
        assert cmd[cmd.index("--data-dir") + 1] == str(tmp_path)
        assert cmd[1].endswith(os.path.join("engine", "bootstrap_engine.py"))


# --------------------------------------------------------------------------- #
# Next-session re-check pickup: stale script removed once the queue empties
# --------------------------------------------------------------------------- #

class TestNextSessionPickup:
    def test_satisfied_op_clears_the_script(self, tmp_path):
        data_dir = str(tmp_path)
        # Pass 1: an apt package was deferred -> script written.
        pass1 = [{"type": "tool", "name": "net-tools", "install_state": "needs_elevation",
                  "elevation": {"method": "apt", "package": "net-tools", "os": "ubuntu"}}]
        q1 = elev.queue_from_failures(pass1, "ubuntu")
        p1 = elev.write_or_clear_script(q1, data_dir, "ubuntu")
        assert p1 and os.path.isfile(p1)

        # Pass 2 (next session): the user ran the script, the tool now resolves,
        # so it produces no needs_elevation failure -> empty queue -> script gone.
        pass2 = []
        q2 = elev.queue_from_failures(pass2, "ubuntu")
        p2 = elev.write_or_clear_script(q2, data_dir, "ubuntu")
        assert p2 is None
        assert not os.path.exists(p1)
