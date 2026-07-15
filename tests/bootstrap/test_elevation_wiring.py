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
import bootstrap_lib.fix_queue as elev
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
        # And the queue harvests it into a brew_installer task.
        tasks = elev.queue_from_failures([failure], "macos")
        assert [t.kind for t in tasks] == ["brew_installer"]

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
    def _agg(self, current_os, data_dir, fix_all_cmd=None):
        tasks = elev.queue_from_failures(
            [{"elevation": {"method": "apt", "package": "net-tools", "os": current_os}}],
            current_os)
        agg = elev.fix_queue_failure(tasks, current_os, data_dir)
        if fix_all_cmd:
            agg["fix_all_cmd"] = fix_all_cmd
        return agg

    def test_windows_aggregate_is_fix_all_eligible(self, capsys):
        """The engine LAUNCHES the runner on fix-all (0.37.0), so the footer must
        not claim otherwise. Before this, an elevation-only pass printed 'None of
        these are fix-all eligible' directly above an item saying to type
        fix-all."""
        agg = self._agg("windows", "C:/data", fix_all_cmd='bash "hook.sh" --console --fix-all')
        assert engine._is_auto_fixable(agg) is True

    def test_unix_aggregate_is_not_fix_all_eligible(self):
        """No fix_all_cmd on Unix: the fix-all run has no TTY to prompt on, so
        offering it would promise a prompt that cannot be answered."""
        agg = self._agg("ubuntu", "/data")
        assert engine._is_auto_fixable(agg) is False

    def test_aggregate_during_a_failed_fix_all_does_not_re_offer_fix_all(self):
        """Loop guard: the re-check pass drops --fix-all, so no fix_all_cmd is
        attached and the footer cannot invite another prompt."""
        agg = self._agg("windows", "C:/data")  # no fix_all_cmd
        assert engine._is_auto_fixable(agg) is False

    def test_elevation_only_pass_emits_the_focused_message(self, capsys):
        """Two lines, not a numbered policy essay."""
        agg = self._agg("windows", "C:/data", fix_all_cmd="cmd")
        engine.emit_failure_response(
            [agg], current_os="windows", log_content="LOGNOISE",
            label="plugins-kit:bootstrap@test",
        )
        payload = json.loads(capsys.readouterr().out)
        sm = payload["systemMessage"]
        assert "Bootstrap found issues that need admin access: Install net-tools." in sm
        assert "Type 'fix-all' to fix them." in sm
        assert "You'll be asked to approve an admin prompt." in sm
        # The focused path drops the log dump and the numbered-list boilerplate.
        assert "LOGNOISE" not in sm
        assert "Fix in order:" not in sm
        assert "'fixed'" not in sm

    def test_unix_focused_message_offers_the_shim(self, capsys):
        agg = self._agg("ubuntu", "/data")
        engine.emit_failure_response(
            [agg], current_os="ubuntu", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        sm = json.loads(capsys.readouterr().out)["systemMessage"]
        assert "bootstrap-fix.sh" in sm
        assert "Type 'fix-all'" not in sm

    def test_per_task_items_are_suppressed_by_the_aggregate(self, capsys):
        """The aggregate speaks for them; repeating the elevation rationale once
        per item is what made the old output unreadable."""
        per_item = {
            "type": "env_check", "name": "ssh-server-windows",
            "message": "m", "agent_msg": "PER_ITEM_PROSE",
            "elevation": {"method": "command", "command": "bash x.sh fix",
                          "os": "windows", "label": "ssh-server-windows"},
        }
        agg = self._agg("windows", "C:/data", fix_all_cmd="cmd")
        engine.emit_failure_response(
            [per_item, agg], current_os="windows", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        ac = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "PER_ITEM_PROSE" not in ac

    def test_per_task_items_surface_raw_when_no_aggregate_exists(self, capsys):
        """If the queue write failed there is nothing speaking for them, so they
        must not vanish silently."""
        per_item = {
            "type": "env_check", "name": "ssh", "message": "m",
            "agent_msg": "PER_ITEM_PROSE",
            "elevation": {"method": "command", "command": "x", "os": "windows"},
        }
        engine.emit_failure_response(
            [per_item], current_os="windows", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        ac = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "PER_ITEM_PROSE" in ac

    def test_mixed_failures_fall_back_to_the_numbered_list(self, capsys):
        """A non-elevation failure alongside the aggregate means the focused
        message would hide it."""
        other = {"type": "venv", "name": "p4-kit", "message": "venv broken",
                 "remediation_cmd": "uv sync"}
        agg = self._agg("windows", "C:/data", fix_all_cmd="cmd")
        engine.emit_failure_response(
            [other, agg], current_os="windows", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        ac = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
        assert "Fix in order:" in ac


# --------------------------------------------------------------------------- #
# Summary-first user-facing footer (systemMessage): labels, not index refs.
# The user never sees the numbered additionalContext list, so "item #2" points
# at nothing -- a concise label per fix-all item is what actually parses.
# --------------------------------------------------------------------------- #

class TestFixAllUserMsg:
    def _agg(self, current_os, data_dir, fix_all_cmd=None):
        tasks = elev.queue_from_failures(
            [{"elevation": {"method": "apt", "package": "net-tools", "os": current_os}}],
            current_os)
        agg = elev.fix_queue_failure(tasks, current_os, data_dir)
        if fix_all_cmd:
            agg["fix_all_cmd"] = fix_all_cmd
        return agg

    def test_mixed_lists_admin_labels_from_aggregate_and_approval_sentence(self):
        """The aggregate expands into one [admin] line per queued task, sourced
        from its `labels` field (not recomputed), plus the admin-approval note."""
        agg = self._agg("windows", "C:/data", fix_all_cmd="cmd")
        manual = {"type": "tool", "name": "foo", "install_state": "manual_install"}
        msg = engine._fix_all_user_msg([manual, agg])
        assert "[admin] Install net-tools" in msg   # label came from queue task
        assert "approve admin access" in msg
        assert "fix-all" in msg
        # No backwards index references leak into the user-facing copy.
        assert "#1" not in msg and "#2" not in msg

    def test_all_auto_without_admin_omits_approval_sentence(self):
        tool = {"type": "tool", "name": "jq", "install_cmd": "winget install jq"}
        msg = engine._fix_all_user_msg([tool])
        assert "Install jq" in msg
        assert "[admin]" not in msg
        assert "approve admin access" not in msg

    def test_mixed_footer_reaches_system_message_with_manual_sentence(self, tmp_path):
        """End to end through emit_failure_response (background mode): the label
        lines, the admin sentence, and a manual-items sentence (no index refs)
        all land in systemMessage."""
        agg = self._agg("windows", "C:/data", fix_all_cmd="cmd")
        manual = {"type": "tool", "name": "foo", "install_state": "manual_install"}
        out = tmp_path / "pending.json"
        engine.emit_failure_response(
            [manual, agg], current_os="windows", log_content="log",
            label="mkt:bootstrap@test", output_file=str(out))
        sm = json.loads(out.read_text())["systemMessage"]
        assert "[admin] Install net-tools" in sm
        assert "approve admin access" in sm
        assert "need manual attention" in sm
        assert "#1" not in sm and "#2" not in sm


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
            elev, "launch_fix_runner",
            lambda path, current_os, timeout=elev.LAUNCH_TIMEOUT, tasks=None:
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
        assert "fix runner completed successfully" in out
        assert "exit code 0" in out

    def test_decline_falls_back_to_manual_message_no_loop(self, tmp_path, monkeypatch):
        """UAC declined: fall back to today's message, never re-prompt."""
        self._pin_bash(monkeypatch)
        monkeypatch.setattr(
            elev, "launch_fix_runner",
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
            "fix-all launched the fix runner but it did not complete "
            "(The operation was canceled by the user).")
        # Manual instruction remains as the fallback.
        assert "bootstrap-fix.bat" in agg[0]["agent_msg"]

    def test_timeout_falls_back_to_manual_message(self, tmp_path, monkeypatch):
        self._pin_bash(monkeypatch)
        monkeypatch.setattr(
            elev, "launch_fix_runner",
            lambda *a, **k: elev.LaunchResult(
                launched=True, succeeded=False,
                detail="timed out after 600s waiting for the fix runner"))

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
            elev, "launch_fix_runner",
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
        assert "bootstrap-fix.sh" in agg["agent_msg"]

    def test_empty_queue_is_noop(self, tmp_path):
        failures = [{"type": "tool", "name": "x", "install_state": "install_failed"}]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=True, console=True), "/plugin/root")
        assert stopped is False
        assert all(f["type"] != "elevation_script" for f in failures)

    def test_queue_write_runtime_error_degrades_not_crashes(self, tmp_path, monkeypatch):
        """render_queue raises when bash can't be resolved and the queue holds
        shell-string tasks. A background SessionStart pass must DEGRADE (surface
        the explanation, keep going), never let the exception kill the whole
        pass's output -- so _elevation_step swallows it into a fallback failure."""
        boom = RuntimeError(
            "cannot write the bootstrap fix queue: bash was not found at write "
            "time... Install Git for Windows and start a new session.")
        monkeypatch.setattr(
            elev, "write_or_clear_queue",
            lambda *a, **k: (_ for _ in ()).throw(boom))

        failures = [_win_failure()]
        stopped = engine._elevation_step(
            failures, "windows", str(tmp_path),
            _args(tmp_path, fix_all=False, background=True), "/plugin/root")

        assert stopped is False
        # No aggregate: the per-task needs_elevation failures then surface raw.
        assert all(f["type"] != "elevation_script" for f in failures)
        # The bash-missing explanation is surfaced, not swallowed.
        writes = [f for f in failures if f["type"] == "fix_queue_write"]
        assert len(writes) == 1
        assert "Install Git for Windows" in writes[0]["agent_msg"]
        assert writes[0]["plugin"] == "bootstrap"
        assert writes[0]["persist_across_sessions"] is True
        # The generic fallback branch renders it and treats it as manual.
        assert engine._is_auto_fixable(writes[0]) is False


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
        p1 = elev.write_or_clear_queue(q1, data_dir, "ubuntu")
        assert p1 and os.path.isfile(p1)

        # Pass 2 (next session): the user ran the script, the tool now resolves,
        # so it produces no needs_elevation failure -> empty queue -> script gone.
        pass2 = []
        q2 = elev.queue_from_failures(pass2, "ubuntu")
        p2 = elev.write_or_clear_queue(q2, data_dir, "ubuntu")
        assert p2 is None
        assert not os.path.exists(p1)
