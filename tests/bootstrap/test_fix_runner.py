"""Unit tests for bootstrap_lib/fix_runner.py.

The runner is the executing half of the interactive-remediation system. Its
load-bearing contracts, in rough order of "what breaks the user if wrong":

  * per-task privilege -- sudo wraps ONLY elevated tasks on Unix, so a secret
    prompt and its write stay the user's (a root-owned secret file in the
    user's home breaks every later unelevated write);
  * commands reach bash as ONE argv element, never re-parsed by an outer shell
    (this is what removes the old renderer's double-quote ban);
  * an unknown task kind fails loudly -- a silently skipped elevated task looks
    like success to the re-check;
  * the plan is printed BEFORE anything runs (the disclosure that replaces
    "read the generated script before approving it").

The runner is executed as a SCRIPT, so it is also imported here the way the
launcher imports it -- by path, with no package context. That is the trap that
made the harvest silently no-op in 0.22.0 (relative imports raised, main()
swallowed it), so it is asserted rather than assumed.
"""

import json
import os
import subprocess
import sys

import pytest

import bootstrap_lib.fix_runner as fr


@pytest.fixture
def runner():
    return fr.Runner({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": []})


@pytest.fixture
def win_runner():
    return fr.Runner({"os": "windows", "bash": "C:/git/bash.exe", "tasks": []})


# --------------------------------------------------------------------------- #
# Privilege model
# --------------------------------------------------------------------------- #

class TestPerTaskPrivilege:
    def test_elevated_task_is_sudo_wrapped_on_unix(self, runner):
        argv = runner._shell_argv("apt-get install x", elevated=True)
        assert argv[0] == "sudo"
        assert argv[-3:] == ["/usr/bin/bash", "-c", "apt-get install x"]

    def test_elevated_task_keeps_the_invoking_users_home(self, runner):
        """REGRESSION GUARD. sudo's default env_reset sets HOME to the TARGET
        user's home (/root), so a queued fix spelling `~` -- the documented
        env_check form, e.g. `bash ~/.claude/scripts/env/sudoers.sh fix` --
        silently resolves against /root and fails.

        The deleted elevation.py had `_expand_home_refs` (added in ae0dd3d for
        exactly this production failure) which rewrote the command TEXT. This
        refactor removed it on the reasoning that "the runner runs as the user,
        so $HOME is already right" -- which is true only for UNELEVATED tasks,
        and queue_from_failures hardcodes elevated=True for every command task.
        So the fix moved to the environment instead; this test is what stops the
        justification from drifting back.
        """
        runner.home = "/home/christina"
        argv = runner._shell_argv("bash ~/.claude/scripts/env/sudoers.sh fix",
                                  elevated=True)
        assert argv[:3] == ["sudo", "env", "HOME=/home/christina"]

    def test_unelevated_task_is_not_sudo_wrapped(self, runner):
        """The whole point of per-task privilege: a task that does not need root
        must not get it, or the files it writes end up root-owned."""
        assert runner._shell_argv("echo hi", elevated=False) == \
            ["/usr/bin/bash", "-c", "echo hi"]

    def test_windows_never_sudo_wraps(self, win_runner):
        """The engine already launched the whole runner elevated; UAC has no
        per-command granularity, `sudo` does not exist there, and UAC preserves
        the user profile so HOME needs no restoring."""
        assert win_runner._shell_argv("x", elevated=True) == ["C:/git/bash.exe", "-c", "x"]

    def test_command_is_one_argv_element(self, runner):
        """No outer shell re-parses it, so quotes need no escaping. This is the
        regression guard for the renderer's double-quote ban."""
        cmd = 'sh -c "echo $(whoami)" && echo \'done\''
        argv = runner._shell_argv(cmd, elevated=False)
        assert argv[-1] == cmd
        assert len(argv) == 3

    def test_brew_installer_is_never_sudo_wrapped(self, monkeypatch):
        """Homebrew's installer refuses to run as root."""
        seen = {}
        monkeypatch.setattr(fr, "_run", lambda argv, label: seen.update(argv=argv) or True)
        r = fr.Runner({"os": "macos", "bash": "/bin/bash", "tasks": []})
        r.run_brew_installer({"label": "Install Homebrew"})
        assert seen["argv"][0] != "sudo"


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

class TestDispatch:
    def test_unknown_kind_raises_rather_than_skipping(self, runner):
        """A silently skipped elevated task looks like success to the re-check,
        which would then report the item as still-broken with no explanation."""
        with pytest.raises(ValueError, match="unknown task kind"):
            runner.dispatch({"kind": "wat", "label": "x"})

    def test_apt_refreshes_lists_before_installing(self, monkeypatch, runner):
        """A fresh machine can have stale/empty lists, so an install against
        them fails on a package that exists."""
        calls = []
        monkeypatch.setattr(fr, "_run", lambda argv, label: calls.append(argv) or True)
        runner.run_apt({"packages": ["net-tools", "tmux"], "label": "x"})
        assert calls[0] == ["sudo", "apt-get", "update"]
        assert calls[1] == ["sudo", "apt-get", "install", "-y", "net-tools", "tmux"]

    def test_apt_does_not_install_when_update_fails(self, monkeypatch, runner):
        monkeypatch.setattr(fr, "_run", lambda argv, label: False)
        assert runner.run_apt({"packages": ["x"], "label": "x"}) is False

    def test_apt_with_no_packages_is_a_noop(self, monkeypatch, runner):
        monkeypatch.setattr(fr, "_run", lambda argv, label: pytest.fail("must not run"))
        assert runner.run_apt({"packages": [], "label": "x"}) is True


# --------------------------------------------------------------------------- #
# Secret gathering
# --------------------------------------------------------------------------- #

class TestSecret:
    def test_secret_is_written_0600_and_never_echoed(self, monkeypatch, tmp_path, capsys):
        target = tmp_path / "sub" / "key.txt"
        monkeypatch.setattr(fr.getpass, "getpass", lambda prompt: "s3cret")
        r = fr.Runner({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": []})
        assert r.run_secret({"label": "API key", "target": str(target),
                             "prompt": "Enter key"}) is True
        assert target.read_text() == "s3cret"
        # The value must not reach stdout -- this console output is the one place
        # the secret exists, and the engine never sees it either way.
        assert "s3cret" not in capsys.readouterr().out
        if os.name != "nt":
            assert oct(target.stat().st_mode)[-3:] == "600"

    def test_empty_secret_is_a_failure_not_an_empty_file(self, monkeypatch, tmp_path):
        target = tmp_path / "key.txt"
        monkeypatch.setattr(fr.getpass, "getpass", lambda prompt: "")
        r = fr.Runner({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": []})
        assert r.run_secret({"label": "API key", "target": str(target)}) is False
        assert not target.exists()

    def test_secret_target_is_user_expanded(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.setattr(fr.getpass, "getpass", lambda prompt: "v")
        r = fr.Runner({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": []})
        r.run_secret({"label": "k", "target": "~/key.txt"})
        assert (tmp_path / "key.txt").read_text() == "v"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

class TestValidate:
    def test_good_queue_has_no_problems(self):
        assert fr.validate({"version": 1, "tasks": [
            {"kind": "command", "label": "L", "command": "x"}]}) == []

    def test_version_skew_is_reported(self):
        problems = fr.validate({"version": 99, "tasks": [
            {"kind": "command", "label": "L", "command": "x"}]})
        assert any("version" in p for p in problems)

    def test_unknown_kind_is_reported(self):
        problems = fr.validate({"version": 1, "tasks": [{"kind": "wat", "label": "L"}]})
        assert any("unknown kind" in p for p in problems)

    def test_command_task_without_command_is_reported(self):
        problems = fr.validate({"version": 1, "tasks": [{"kind": "command", "label": "L"}]})
        assert any("no command" in p for p in problems)

    def test_secret_task_without_target_is_reported(self):
        problems = fr.validate({"version": 1, "tasks": [{"kind": "secret", "label": "L"}]})
        assert any("no target" in p for p in problems)

    def test_missing_label_is_reported(self):
        """The label is the only thing a human reads, in the plan AND in the
        session message."""
        problems = fr.validate({"version": 1, "tasks": [{"kind": "command", "command": "x"}]})
        assert any("missing label" in p for p in problems)

    def test_empty_queue_is_reported(self):
        assert any("no tasks" in p for p in fr.validate({"version": 1, "tasks": []}))


# --------------------------------------------------------------------------- #
# Plan + run
# --------------------------------------------------------------------------- #

class TestPlanAndRun:
    def test_plan_lists_every_label_and_marks_admin(self, capsys):
        fr.print_plan({"tasks": [
            {"label": "Install net-tools", "elevated": True},
            {"label": "OpenRouter API key", "elevated": False}]})
        out = capsys.readouterr().out
        assert "Install net-tools" in out
        assert "OpenRouter API key" in out
        assert "[admin] Install net-tools" in out
        assert "[admin] OpenRouter API key" not in out

    def test_run_continues_past_a_failed_task(self, monkeypatch, capsys):
        """Tasks are independent; one broken fix must not block the rest. The
        re-check pass is the authority on what actually cleared."""
        ran = []

        def fake_dispatch(self, task):
            ran.append(task["label"])
            return task["label"] != "bad"

        monkeypatch.setattr(fr.Runner, "dispatch", fake_dispatch)
        code = fr.run_queue({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": [
            {"kind": "command", "label": "bad", "command": "x"},
            {"kind": "command", "label": "good", "command": "y"}]})
        assert ran == ["bad", "good"]
        assert code == fr.EXIT_TASK_FAILED
        assert "bad" in capsys.readouterr().out

    def test_a_raising_task_does_not_kill_the_run(self, monkeypatch):
        def fake_dispatch(self, task):
            if task["label"] == "boom":
                raise RuntimeError("kaboom")
            return True

        monkeypatch.setattr(fr.Runner, "dispatch", fake_dispatch)
        code = fr.run_queue({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": [
            {"kind": "command", "label": "boom", "command": "x"},
            {"kind": "command", "label": "fine", "command": "y"}]})
        assert code == fr.EXIT_TASK_FAILED

    def test_all_ok_exits_zero(self, monkeypatch):
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        assert fr.run_queue({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": [
            {"kind": "command", "label": "a", "command": "x"}]}) == fr.EXIT_OK


class TestMain:
    def _queue(self, tmp_path, tasks):
        p = tmp_path / "queue.json"
        p.write_text(json.dumps({"version": 1, "os": "ubuntu",
                                 "bash": "/usr/bin/bash", "tasks": tasks}))
        return str(p)

    def test_missing_queue_file_is_reported_not_traceback(self, tmp_path, capsys):
        assert fr.main([str(tmp_path / "nope.json")]) == fr.EXIT_BAD_QUEUE
        assert "could not read queue" in capsys.readouterr().err

    def test_invalid_queue_is_reported(self, tmp_path, capsys):
        p = tmp_path / "queue.json"
        p.write_text(json.dumps({"version": 1, "tasks": [{"kind": "wat", "label": "L"}]}))
        assert fr.main([str(p)]) == fr.EXIT_BAD_QUEUE
        assert "not executable" in capsys.readouterr().err

    def test_no_args_is_usage(self, capsys):
        assert fr.main([]) == fr.EXIT_BAD_QUEUE

    def test_engine_launch_skips_the_success_hold(self, monkeypatch, tmp_path):
        """The engine is waiting on this process, so a `press Enter` on success
        would hang the fix-all run until a human noticed."""
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not hold"))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_OK

    def test_engine_launch_still_holds_on_failure(self, monkeypatch, tmp_path):
        """Errors must stay legible in the window that is about to close."""
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: False)
        monkeypatch.setattr("builtins.input", lambda *a: held.append(True))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_TASK_FAILED
        assert held

    def test_human_launch_holds_on_success(self, monkeypatch, tmp_path):
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr("builtins.input", lambda *a: held.append(True))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        fr.main([path])
        assert held

    def test_plan_is_printed_before_anything_runs(self, monkeypatch, tmp_path, capsys):
        """The disclosure that replaces reading the generated script."""
        order = []
        monkeypatch.setattr(fr, "print_plan", lambda q: order.append("plan"))
        monkeypatch.setattr(fr, "run_queue", lambda q: order.append("run") or fr.EXIT_OK)
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        fr.main([path, "--engine"])
        assert order == ["plan", "run"]


class TestScriptInvocation:
    def test_runs_as_a_script_with_no_package_context(self, tmp_path):
        """The launcher invokes `python fix_runner.py <queue>` directly, so the
        module must not rely on package-relative imports -- exactly the failure
        that made the harvest a silent no-op in 0.22.0. A unit test that imports
        the module as part of its package cannot catch it; only a subprocess can.
        """
        queue = tmp_path / "queue.json"
        queue.write_text(json.dumps({"version": 1, "os": "ubuntu",
                                     "bash": "/usr/bin/bash", "tasks": []}))
        proc = subprocess.run(
            [sys.executable, fr.__file__, str(queue), "--engine"],
            capture_output=True, text=True, timeout=60,
        )
        # An empty queue is a validation error (EXIT_BAD_QUEUE), NOT an import
        # crash -- reaching validation at all proves the module loaded standalone.
        assert proc.returncode == fr.EXIT_BAD_QUEUE
        assert "no tasks" in proc.stderr
        assert "Traceback" not in proc.stderr
