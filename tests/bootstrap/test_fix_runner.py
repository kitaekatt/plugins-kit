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
from unittest.mock import MagicMock, patch

import pytest

import bootstrap_lib.fix_runner as fr

# is_dead answers "dead" only after confirming the entry's VOLUME is reachable,
# and a volume is a Windows concept: os.path.splitdrive finds no drive in a
# posix path (nor even in a literal "C:\\dead" when running on posix), so
# _volume_root returns None and the predicate fails safe to ALIVE. That is the
# design -- path_prune.scan() is win32-only and returns None elsewhere -- so a
# test asserting a real DEAD verdict can only run on Windows.
_DEAD_VERDICT_IS_WIN32 = (
    "is_dead's DEAD verdict needs a reachable volume (win32-only by design; "
    "fails safe to alive elsewhere)")


def _fs_is_dead(entry):
    """is_dead's rule for an entry on a reachable local volume: no dir, no life.

    Substituted by tests that are about something OTHER than the predicate, so
    they exercise their own subject on every platform instead of going vacuous
    off Windows. Faithful, not a weakening: a tmp_path IS on a reachable local
    volume, so for the entries these tests use this agrees exactly with what the
    real predicate returns on Windows. The volume gate itself is TestIsDead's
    job, and stays win32-only there.
    """
    return not os.path.isdir(entry)


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
        monkeypatch.setattr(fr, "_run",
                            lambda argv, label, env=None: seen.update(argv=argv) or True)
        r = fr.Runner({"os": "macos", "bash": "/bin/bash", "tasks": []})
        r.run_brew_installer({"label": "Install Homebrew"})
        assert seen["argv"][0] != "sudo"


# --------------------------------------------------------------------------- #
# Child environment
# --------------------------------------------------------------------------- #

class TestChildEnv:
    def test_bash_dir_leads_the_child_path(self):
        """REGRESSION GUARD. On Windows the engine launches the runner via
        `Start-Process -Verb RunAs`, and an elevated process does NOT inherit
        the caller's environment -- it gets the user's default env block, whose
        PATH has no Git usr/bin. `bash -c` is non-login, so msys never prepends
        /usr/bin itself. Result (live, 0.49.0): every queued command died with
        exit 127 -- `ln: command not found` for the symlink task, `bash:
        command not found` for an env_check fix -- while the runner itself
        (absolute bash path baked into the queue) launched fine. The queue's
        baked bash path fixed only what launches BASH, not what bash launches.
        """
        env = fr._child_env(os.path.join("C:", "git", "usr", "bin", "bash.exe"))
        first = env["PATH"].split(os.pathsep)[0]
        assert first == os.path.abspath(os.path.join("C:", "git", "usr", "bin"))

    def test_the_rest_of_the_environment_survives(self, monkeypatch):
        """The fix is a prepend, not a replacement: scoop shims, TEMP, the
        user profile -- everything else the tasks rely on stays intact."""
        monkeypatch.setenv("FIXRUNNER_CANARY", "alive")
        env = fr._child_env("/usr/bin/bash")
        assert env["FIXRUNNER_CANARY"] == "alive"
        assert os.environ.get("PATH", "") in env["PATH"]

    def test_run_command_passes_the_child_env(self, monkeypatch, win_runner):
        seen = {}
        monkeypatch.setattr(
            fr, "_run",
            lambda argv, label, env=None: seen.update(env=env) or True)
        win_runner.run_command({"command": "x", "label": "x"})
        assert seen["env"] is win_runner.env
        assert seen["env"]["PATH"].split(os.pathsep)[0] == \
            os.path.dirname(os.path.abspath(win_runner.bash))

    def test_home_is_the_msys_profile_on_windows(self, monkeypatch):
        """REGRESSION GUARD. `bash -c` is non-login, so an elevated fresh bash
        defaults $HOME to the msys /home/<user>, not the Windows profile -- a
        queued `bash ~/.claude/scripts/env/ssh-server-windows.sh fix` then
        resolves to a path that does not exist and dies with exit 127 (observed
        live: 2060W, bootstrap 0.50.2). _child_env hands bash the real profile
        in login-Git-Bash form so `~` resolves as in a normal session."""
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setattr(os.path, "expanduser", lambda p: "C:\\Users\\truff")
        env = fr._child_env(os.path.join("C:", "git", "usr", "bin", "bash.exe"))
        assert env["HOME"] == "/c/Users/truff"

    def test_home_untouched_off_windows(self, monkeypatch):
        """On Unix the invoking user's HOME is already the right one (and the
        sudo path in _shell_argv restores it inside the elevation), so
        _child_env must not rewrite it."""
        monkeypatch.setattr(os, "name", "posix")
        monkeypatch.setenv("HOME", "/home/christina")
        env = fr._child_env("/usr/bin/bash")
        assert env["HOME"] == "/home/christina"

    def test_msys_home_converts_drive_paths(self):
        """C:\\Users\\you -> /c/Users/you; a driveless value just gets its
        separators flipped rather than a bogus leading slash."""
        assert fr._msys_home("C:\\Users\\truff") == "/c/Users/truff"
        assert fr._msys_home("D:\\dev\\x") == "/d/dev/x"
        assert fr._msys_home("\\\\server\\share") == "//server/share"


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
        monkeypatch.setattr(fr, "_run",
                            lambda argv, label, env=None: calls.append(argv) or True)
        runner.run_apt({"packages": ["net-tools", "tmux"], "label": "x"})
        assert calls[0] == ["sudo", "apt-get", "update"]
        assert calls[1] == ["sudo", "apt-get", "install", "-y", "net-tools", "tmux"]

    def test_apt_does_not_install_when_update_fails(self, monkeypatch, runner):
        monkeypatch.setattr(fr, "_run", lambda argv, label, env=None: False)
        assert runner.run_apt({"packages": ["x"], "label": "x"}) is False

    def test_apt_with_no_packages_is_a_noop(self, monkeypatch, runner):
        monkeypatch.setattr(fr, "_run",
                            lambda argv, label, env=None: pytest.fail("must not run"))
        assert runner.run_apt({"packages": [], "label": "x"}) is True


# --------------------------------------------------------------------------- #
# Secret gathering
# --------------------------------------------------------------------------- #

class TestIsDead:
    """The predicate that decides whether a PATH entry gets deleted.

    Every ambiguous case must resolve to ALIVE: a false "alive" leaves a stale
    entry nobody notices; a false "dead" silently deletes a directory the user
    needs. Review found three ways this was getting that backwards -- each test
    below marked REGRESSION GUARD is one of them, and each was verified against
    the real filesystem, not a mock.
    """

    @pytest.mark.skipif(sys.platform != "win32", reason=_DEAD_VERDICT_IS_WIN32)
    def test_a_missing_directory_is_dead(self, tmp_path):
        assert fr.is_dead(str(tmp_path / "nope")) is True

    def test_an_existing_directory_is_alive(self, tmp_path):
        assert fr.is_dead(str(tmp_path)) is False

    @pytest.mark.skipif(sys.platform != "win32", reason=_DEAD_VERDICT_IS_WIN32)
    def test_a_file_is_dead_because_a_path_entry_must_be_a_directory(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x")
        assert fr.is_dead(str(f)) is True

    def test_variables_are_expanded_before_testing(self, tmp_path, monkeypatch):
        """A Windows User PATH is often REG_EXPAND_SZ, so entries legitimately
        read `%JAVA_HOME%\\bin`; probing that literal finds nothing on disk."""
        monkeypatch.setenv("PRUNE_TEST_HOME", str(tmp_path))
        assert fr.is_dead("%PRUNE_TEST_HOME%") is False

    def test_an_undefined_variable_is_alive(self, monkeypatch):
        monkeypatch.delenv("PRUNE_TEST_UNSET", raising=False)
        assert fr.is_dead("%PRUNE_TEST_UNSET%\\bin") is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows volume semantics")
    def test_an_unreachable_volume_is_alive(self):
        """REGRESSION GUARD #1. os.path.isdir NEVER raises -- it swallows OSError
        and returns False -- so an offline \\\\nas\\share read as DEAD and was
        queued for deletion. os.stat is no better: an offline UNC host and an
        unmapped drive both surface as FileNotFoundError winerror 3, exactly like
        a genuinely missing directory. Hence the volume-reachability check.

        The original test for this monkeypatched isdir to raise -- something real
        isdir never does -- so it passed while the bug shipped. It validated
        fiction. This one asks the real filesystem."""
        assert fr.is_dead(r"\\no-such-host-xyz123\share\tools") is False
        assert fr.is_dead(r"Z:\unmapped-drive\tools") is False

    @pytest.mark.skipif(sys.platform != "win32", reason="needs C:\\WINDOWS")
    def test_a_quoted_live_directory_is_alive(self):
        """REGRESSION GUARD #2. cmd.exe strips quotes when resolving PATH, so
        `"C:\\Program Files\\Foo"` is a working entry (and quoting is mandatory
        for any path containing a semicolon). Probing the quoted string found
        nothing and condemned it -- verified: a quoted C:\\WINDOWS was classed
        dead."""
        assert fr.is_dead('"C:\\WINDOWS"') is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
    def test_a_driveless_entry_is_alive(self):
        """REGRESSION GUARD #3. `\\from\\local` resolves against whatever the
        current drive happens to be -- not a question with a stable answer, so
        not ours to delete."""
        assert fr.is_dead("\\from\\local") is False

    def test_an_empty_entry_is_not_dead(self):
        assert fr.is_dead("   ") is False


class TestPathPrune:
    """The destructive task. Every test here fakes winreg -- see
    tests/conftest.py for why touching the real one is a firing offense."""

    def _fake_winreg(self, current, value_type=2):
        store = {"Path": (current, value_type)}
        key = MagicMock()
        key.__enter__ = MagicMock(return_value=key)
        key.__exit__ = MagicMock(return_value=False)
        reg = MagicMock()
        reg.HKEY_CURRENT_USER = 0
        reg.KEY_READ = 0
        reg.KEY_WRITE = 0
        reg.OpenKey.return_value = key
        reg.QueryValueEx.side_effect = lambda k, n: store[n]
        reg.SetValueEx.side_effect = (
            lambda k, n, r, t, v: store.__setitem__(n, (v, t)))
        return reg, store

    def _run(self, reg, task, os_name="windows"):
        """Removal mechanics only: is_dead is stubbed True so these stay about
        matching and rewriting, and stay meaningful off Windows (a literal
        `C:\\dead` has no drive on posix, so the real predicate would keep it).
        The prune-time re-check is covered by _run_real_fs below."""
        r = fr.Runner({"os": os_name, "bash": "C:/git/bash.exe", "tasks": []})
        with patch.dict("sys.modules", {"winreg": reg}), \
             patch.object(fr, "is_dead", lambda entry: True), \
             patch.object(fr, "_broadcast_environment_change"):
            return r.run_path_prune(task)

    def _run_real_fs(self, reg, task):
        """Like _run but with no FIXED verdict -- the filesystem decides.

        Uses _fs_is_dead rather than the real is_dead so the re-check these
        tests guard stays live off Windows, where the real predicate can never
        say "dead" (see _DEAD_VERDICT_IS_WIN32). What is under test is the
        RUNNER re-probing at prune time instead of trusting the queue's
        sessions-old verdict -- platform-independent logic, and the thing whose
        loss would silently delete a live directory.
        """
        r = fr.Runner({"os": "windows", "bash": "C:/git/bash.exe", "tasks": []})
        with patch.dict("sys.modules", {"winreg": reg}), \
             patch.object(fr, "is_dead", _fs_is_dead), \
             patch.object(fr, "_broadcast_environment_change"):
            return r.run_path_prune(task)

    def test_an_entry_that_came_back_to_life_is_not_deleted(self, tmp_path):
        """REGRESSION GUARD. Detection can be sessions old -- the finding
        persists until the user consents -- and deadness is a property of the
        FILESYSTEM while the engine's cache keys on the PATH TEXT. Uninstall a
        tool (entry -> dead), decline the prune, reinstall to the same location:
        the installer sees its PATH entry already present and changes nothing,
        so the text never moves, the hash never moves, and the stale verdict
        would delete a live directory. Verified against a real dir."""
        revived = tmp_path / "reinstalled"; revived.mkdir()
        gone = tmp_path / "really-gone"
        reg, store = self._fake_winreg(f"{revived};{gone}")
        assert self._run_real_fs(
            reg, {"label": "x", "entries": [str(revived), str(gone)]}) is True
        assert store["Path"][0] == str(revived)

    def test_all_entries_revived_removes_nothing(self, tmp_path):
        alive = tmp_path / "back"; alive.mkdir()
        reg, store = self._fake_winreg(str(alive))
        assert self._run_real_fs(reg, {"label": "x", "entries": [str(alive)]}) is True
        reg.SetValueEx.assert_not_called()

    def test_removes_only_the_named_entries(self):
        reg, store = self._fake_winreg("C:\\keep;C:\\dead;C:\\also-keep")
        assert self._run(reg, {"label": "x", "entries": ["C:\\dead"]}) is True
        assert store["Path"][0] == "C:\\keep;C:\\also-keep"

    def test_an_entry_added_since_detection_survives(self):
        """The queue names entries; the runner re-reads at execution time. So a
        PATH that changed between detection and consent loses only what was
        named -- an install's fresh entry is untouched."""
        reg, store = self._fake_winreg("C:\\dead;C:\\cuda\\bin")
        assert self._run(reg, {"label": "x", "entries": ["C:\\dead"]}) is True
        assert store["Path"][0] == "C:\\cuda\\bin"

    def test_matching_ignores_case_and_trailing_slash(self):
        """Windows paths are case-insensitive and C:\\x == C:\\x\\ ."""
        reg, store = self._fake_winreg("C:\\Dead\\;C:\\keep")
        assert self._run(reg, {"label": "x", "entries": ["c:\\dead"]}) is True
        assert store["Path"][0] == "C:\\keep"

    def test_the_value_type_is_preserved(self):
        """REG_EXPAND_SZ (2) must not be rewritten as REG_SZ (1): that would
        stop Windows expanding every %VAR% in the PATH -- breaking exactly the
        entries the prune took care not to touch."""
        reg, store = self._fake_winreg("C:\\dead;%JAVA_HOME%\\bin", value_type=2)
        self._run(reg, {"label": "x", "entries": ["C:\\dead"]})
        assert store["Path"] == ("%JAVA_HOME%\\bin", 2)

    def test_the_previous_path_is_backed_up_before_writing(self, tmp_path):
        """30 entries is not something a user reconstructs by hand."""
        backup = tmp_path / "path_backup.txt"
        reg, _ = self._fake_winreg("C:\\dead;C:\\keep")
        self._run(reg, {"label": "x", "entries": ["C:\\dead"],
                        "backup": str(backup)})
        assert backup.read_text() == "C:\\dead;C:\\keep"

    def test_already_pruned_is_success_not_failure(self):
        """Idempotent: the entries are gone, which is the requested end state."""
        reg, store = self._fake_winreg("C:\\keep")
        assert self._run(reg, {"label": "x", "entries": ["C:\\dead"]}) is True
        assert store["Path"][0] == "C:\\keep"

    def test_no_path_value_is_a_failure_not_a_crash(self):
        reg, _ = self._fake_winreg("x")
        reg.QueryValueEx.side_effect = FileNotFoundError()
        assert self._run(reg, {"label": "x", "entries": ["C:\\dead"]}) is False

    def test_a_registry_error_is_contained(self):
        reg, _ = self._fake_winreg("C:\\dead")
        reg.OpenKey.side_effect = OSError("denied")
        assert self._run(reg, {"label": "x", "entries": ["C:\\dead"]}) is False

    def test_empty_entries_is_a_noop(self):
        reg, _ = self._fake_winreg("C:\\keep")
        assert self._run(reg, {"label": "x", "entries": []}) is True
        reg.SetValueEx.assert_not_called()


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

    def test_path_prune_without_entries_is_reported(self):
        """Not a no-op to shrug at: the plan would promise the user a prune and
        then do nothing, which reads as success to the re-check."""
        problems = fr.validate({"version": 1, "tasks": [
            {"kind": "path_prune", "label": "L"}]})
        assert any("no entries" in p for p in problems)

    def test_path_prune_with_junk_entries_is_reported(self):
        problems = fr.validate({"version": 1, "tasks": [
            {"kind": "path_prune", "label": "L", "entries": ["ok", ""]}]})
        assert any("non-empty strings" in p for p in problems)

    def test_good_path_prune_task_validates(self):
        assert fr.validate({"version": 1, "tasks": [
            {"kind": "path_prune", "label": "L", "entries": ["C:\\dead"]}]}) == []

    def test_unknown_cost_is_reported(self):
        problems = fr.validate({"version": 1, "tasks": [
            {"kind": "command", "label": "L", "command": "x", "cost": "medium"}]})
        assert any("unknown cost" in p for p in problems)

    def test_absent_cost_is_fine(self):
        """An older engine's queue has no cost anywhere; it must still run."""
        assert fr.validate({"version": 1, "tasks": [
            {"kind": "command", "label": "L", "command": "x"}]}) == []


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

    def test_plan_flags_only_the_slow_tasks(self, capsys):
        """The note is what stops a 3GB download reading as a hang -- and what
        stops a symlink reading as one if it were applied to everything."""
        fr.print_plan({"tasks": [
            {"label": "Link starship-config", "elevated": True},
            {"label": "CUDA Toolkit", "elevated": True, "cost": "slow"}]})
        out = capsys.readouterr().out
        cuda = [l for l in out.splitlines() if "CUDA" in l][0]
        link = [l for l in out.splitlines() if "starship" in l][0]
        assert fr.SLOW_NOTE in cuda
        assert fr.SLOW_NOTE not in link

    def test_plan_numbers_tasks_in_queue_order(self, capsys):
        fr.print_plan({"tasks": [{"label": "alpha"}, {"label": "beta"}]})
        listed = [l.strip() for l in capsys.readouterr().out.splitlines()
                  if "alpha" in l or "beta" in l]
        assert listed == ["1. [     ] alpha", "2. [     ] beta"]

    def test_each_step_reports_progress_and_a_verdict(self, monkeypatch, capsys):
        """A step with no outcome line is indistinguishable from a skipped one
        to the person watching the window."""
        monkeypatch.setattr(fr.Runner, "dispatch",
                            lambda self, task: task["label"] == "good")
        fr.run_queue({"os": "ubuntu", "bash": "/usr/bin/bash", "tasks": [
            {"kind": "command", "label": "good", "command": "x"},
            {"kind": "command", "label": "bad", "command": "y"}]})
        out = capsys.readouterr().out
        assert "[1/2] good" in out
        assert "[2/2] bad" in out
        assert "-> done" in out
        assert "-> FAILED" in out

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

    def test_engine_launch_holds_on_success(self, monkeypatch, tmp_path):
        """REGRESSION GUARD. The hold used to be skipped exactly here, so a
        clean fix-all closed its window instantly -- hiding the only account the
        user gets of what just ran elevated on their machine. The engine budgets
        for the wait (fix_queue.ACK_GRACE); it must not be reintroduced as an
        optimization."""
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: held.append(prompt))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_OK
        assert held

    def test_engine_launch_still_holds_on_failure(self, monkeypatch, tmp_path):
        """Errors must stay legible in the window that is about to close."""
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: False)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: held.append(prompt))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_TASK_FAILED
        assert held

    def test_human_launch_holds_on_success(self, monkeypatch, tmp_path):
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: held.append(prompt))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        fr.main([path])
        assert held

    def test_hold_prompt_says_what_the_key_does(self, monkeypatch, tmp_path):
        """An engine-launched window resumes a waiting Claude session; a
        double-clicked one just closes. Same key, different consequence."""
        held = []
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: held.append(prompt))
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        fr.main([path, "--engine"])
        fr.main([path])
        assert "continue" in held[0] and "close" in held[1]
        assert all("Space or Enter" in p for p in held)

    def test_plan_is_printed_before_anything_runs(self, monkeypatch, tmp_path, capsys):
        """The disclosure that replaces reading the generated script."""
        order = []
        monkeypatch.setattr(fr, "print_plan", lambda q: order.append("plan"))
        monkeypatch.setattr(fr, "run_queue", lambda q: order.append("run") or fr.EXIT_OK)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: None)
        path = self._queue(tmp_path, [{"kind": "command", "label": "a", "command": "x"}])
        fr.main([path, "--engine"])
        assert order == ["plan", "run"]


class TestHold:
    def test_no_tty_falls_back_to_line_input(self, monkeypatch, capsys):
        """REGRESSION GUARD. msvcrt reads the CONSOLE, not stdin, so branching
        on os.name before checking isatty() made the hold block forever wherever
        no console exists (a test runner, a piped run) -- a hang, not a
        fallback. The TTY check must come first on every platform."""
        monkeypatch.setattr(fr.sys.stdin, "isatty", lambda: False, raising=False)
        called = []
        monkeypatch.setattr("builtins.input", lambda *a: called.append(True) or "")
        fr.wait_for_key("  Press Space or Enter. ")
        assert called
        assert "Press Space or Enter." in capsys.readouterr().out

    def test_hold_never_raises(self, monkeypatch):
        """A courtesy pause must not be the thing that fails the run."""
        monkeypatch.setattr(fr.sys.stdin, "isatty", lambda: False, raising=False)
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError))
        fr.wait_for_key("  x ")  # must not raise

    def test_space_and_enter_are_both_accepted(self):
        assert fr._accepted(" ")
        assert fr._accepted("\r")
        assert fr._accepted("\n")
        assert not fr._accepted("q")


class TestTranscript:
    """The elevated window is the only console the runner ever has, and it
    closes on a keypress -- the transcript is what makes a failed fix-all
    diagnosable afterwards."""

    def _queue(self, tmp_path, tasks):
        p = tmp_path / "queue.json"
        p.write_text(json.dumps({"version": 1, "os": "ubuntu",
                                 "bash": "/usr/bin/bash", "tasks": tasks}))
        return str(p)

    def test_transcript_lands_next_to_the_queue(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: None)
        path = self._queue(tmp_path, [{"kind": "command", "label": "alpha",
                                       "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_OK
        log = (tmp_path / fr.LOG_BASENAME).read_text()
        assert "Bootstrap remediation" in log   # the plan
        assert "alpha" in log
        assert "-> done" in log                 # the verdict

    def test_child_output_flows_through_stdout_and_so_into_the_transcript(
            self, capsys):
        """REGRESSION GUARD. The child used to inherit the console fd directly,
        so its output -- the ONE line that explains a failure, e.g. `ln:
        command not found` -- bypassed any tee and died with the window. The
        pump routes it through sys.stdout, which is what the transcript wraps.
        """
        ok = fr._run([sys.executable, "-c",
                      "import sys; print('marker-out'); "
                      "print('marker-err', file=sys.stderr)"], "child")
        out = capsys.readouterr().out
        assert ok is True
        assert "marker-out" in out
        assert "marker-err" in out   # stderr is folded into the same stream

    def test_a_failing_child_reports_its_exit_code(self, capsys):
        ok = fr._run([sys.executable, "-c", "raise SystemExit(127)"], "child")
        assert ok is False
        assert "! exited 127" in capsys.readouterr().out

    def test_an_unwritable_transcript_never_fails_the_run(
            self, monkeypatch, tmp_path):
        """Logging is a courtesy; the fixes are the job."""
        monkeypatch.setattr(fr, "_open_transcript", lambda path: None)
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: None)
        path = self._queue(tmp_path, [{"kind": "command", "label": "a",
                                       "command": "x"}])
        assert fr.main([path, "--engine"]) == fr.EXIT_OK

    def test_streams_are_restored_after_main(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fr.Runner, "dispatch", lambda self, task: True)
        monkeypatch.setattr(fr, "wait_for_key", lambda prompt: None)
        before_out, before_err = sys.stdout, sys.stderr
        path = self._queue(tmp_path, [{"kind": "command", "label": "a",
                                       "command": "x"}])
        fr.main([path, "--engine"])
        assert sys.stdout is before_out
        assert sys.stderr is before_err


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
