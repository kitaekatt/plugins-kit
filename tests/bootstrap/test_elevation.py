"""Unit tests for bootstrap_lib/elevation.py.

Covers the elevation queue + per-OS remediation-script generator (sequence
step 8): descriptor harvesting into ElevationQueue, golden-file script content
per OS, script write/regenerate/clear (cleanup when the queue empties), the
aggregated fix-all item, and the privilege dispatcher.
"""

import os

import pytest

import bootstrap_lib.elevation as elev
from bootstrap_lib.elevation import ElevationQueue

# The engine-resolved Git Bash a Windows render would embed (tests pin the
# resolver so goldens are hermetic on every dev platform).
FAKE_BASH = "C:\\Program Files\\Git\\usr\\bin\\bash.exe"


# --------------------------------------------------------------------------- #
# Privilege dispatcher
# --------------------------------------------------------------------------- #

class TestPrivilegesAvailable:
    def test_windows_uses_admin_token(self, monkeypatch):
        monkeypatch.setattr(elev, "windows_admin_available", lambda: True)
        monkeypatch.setattr(elev, "sudo_noninteractive_available",
                            lambda: (_ for _ in ()).throw(AssertionError("must not probe sudo on windows")))
        assert elev.privileges_available("windows") is True

    def test_unix_uses_sudo_probe(self, monkeypatch):
        monkeypatch.setattr(elev, "sudo_noninteractive_available", lambda: True)
        monkeypatch.setattr(elev, "windows_admin_available",
                            lambda: (_ for _ in ()).throw(AssertionError("must not probe admin token on unix")))
        assert elev.privileges_available("ubuntu") is True
        assert elev.privileges_available("macos") is True

    def test_unix_sudo_missing_is_false(self, monkeypatch):
        monkeypatch.setattr(elev, "sudo_noninteractive_available", lambda: False)
        assert elev.privileges_available("ubuntu") is False


# --------------------------------------------------------------------------- #
# queue_from_failures: descriptor harvesting + accumulation across tools
# --------------------------------------------------------------------------- #

class TestQueueFromFailures:
    def test_empty_failures_is_empty_queue(self):
        q = elev.queue_from_failures([], "ubuntu")
        assert q.is_empty()

    def test_ignores_failures_without_descriptor(self):
        q = elev.queue_from_failures(
            [{"type": "tool", "name": "x", "install_state": "install_failed"}], "ubuntu")
        assert q.is_empty()

    def test_accumulates_multiple_apt_packages_in_order(self):
        failures = [
            {"elevation": {"method": "apt", "package": "net-tools", "os": "ubuntu"}},
            {"elevation": {"method": "apt", "package": "tmux", "os": "ubuntu"}},
        ]
        q = elev.queue_from_failures(failures, "ubuntu")
        assert q.apt_packages == ["net-tools", "tmux"]
        assert not q.is_empty()

    def test_accumulates_commands_and_apt_and_brew(self):
        failures = [
            {"elevation": {"method": "apt", "package": "net-tools", "os": "ubuntu"}},
            {"elevation": {"method": "command", "command": "curl x | sh", "os": "ubuntu"}},
        ]
        q = elev.queue_from_failures(failures, "ubuntu")
        assert q.apt_packages == ["net-tools"]
        assert q.commands == ["curl x | sh"]
        assert q.brew_installer is False

    def test_brew_installer_flag_from_descriptor(self):
        q = elev.queue_from_failures(
            [{"elevation": {"method": "brew_installer", "os": "macos"}}], "macos")
        assert q.brew_installer is True
        assert not q.is_empty()

    def test_filters_by_current_os(self):
        # A macos descriptor must not land in an ubuntu queue.
        failures = [{"elevation": {"method": "brew_installer", "os": "macos"}}]
        q = elev.queue_from_failures(failures, "ubuntu")
        assert q.is_empty()


# --------------------------------------------------------------------------- #
# Golden-file script content per OS
# --------------------------------------------------------------------------- #

class TestRenderUbuntu:
    def test_apt_update_precedes_install_of_all_packages(self):
        q = ElevationQueue(apt_packages=["net-tools", "tmux", "direnv"])
        out = elev.render_script(q, "ubuntu", "/data/elevate/install-elevated.sh")
        assert out.startswith("#!/usr/bin/env bash\n")
        assert "set -euo pipefail" in out
        # apt-get update comes before the single accumulated install line.
        upd = out.index("apt-get update")
        inst = out.index("apt-get install -y net-tools tmux direnv")
        assert upd < inst
        # header explains WHY elevation is needed.
        assert "must never prompt for a sudo password" in out
        assert 'sudo bash "/data/elevate/install-elevated.sh"' in out

    def test_commands_rendered_after_apt(self):
        q = ElevationQueue(apt_packages=["net-tools"], commands=["curl -fsSL x | sh"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        assert "apt-get install -y net-tools" in out
        # Label is a plain comment (zero execution/quoting surface), not an echo.
        assert "# bootstrap-elevate: curl -fsSL x | sh" in out
        assert "\ncurl -fsSL x | sh\n" in out
        assert out.index("apt-get install") < out.index("# bootstrap-elevate:")

    def test_command_label_never_echoed(self):
        # A command containing quotes/substitution must not appear inside an
        # echo: an unbalanced quote would make the script unparseable (bypassing
        # set -euo pipefail) and $(...) would execute during the label.
        hostile = 'sh -c "echo $(whoami)"'
        for os_key in ("ubuntu", "macos"):
            out = elev.render_script(ElevationQueue(commands=[hostile]), os_key, "/p.sh")
            assert f"# bootstrap-elevate: {hostile}" in out
            assert "echo \"bootstrap-elevate" not in out

    def test_commands_only_no_apt_section(self):
        q = ElevationQueue(commands=["some-elevated-cmd"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        assert "apt-get update" not in out
        assert "apt-get install" not in out
        assert "some-elevated-cmd" in out


class TestRenderMacos:
    def test_brew_installer_leads_when_missing(self):
        q = ElevationQueue(brew_installer=True, commands=["tic -x kitty.terminfo"])
        out = elev.render_script(q, "macos", "/p.sh")
        assert out.startswith("#!/usr/bin/env bash\n")
        assert elev.HOMEBREW_INSTALLER in out
        # installer precedes the elevated commands.
        assert out.index(elev.HOMEBREW_INSTALLER) < out.index("tic -x kitty.terminfo")
        assert 'bash "/p.sh"' in out
        assert "apt-get" not in out

    def test_no_brew_line_when_present(self):
        q = ElevationQueue(commands=["tic -x kitty.terminfo"])
        out = elev.render_script(q, "macos", "/p.sh")
        assert "Homebrew" not in out
        assert "tic -x kitty.terminfo" in out


class TestHomeExpansion:
    """Queued commands with ~ / $HOME are pre-expanded to the INVOKING user's
    home at render time: the Ubuntu script runs under `sudo bash` where
    HOME=/root, so verbatim home references would resolve to root's home and
    abort the script. Rule: ~/ at start-of-string or after whitespace, and
    word-bounded $HOME / ${HOME}, become os.path.expanduser("~")."""

    @pytest.fixture(autouse=True)
    def fake_home(self, monkeypatch):
        monkeypatch.setenv("HOME", "/home/christina")

    def test_ubuntu_tilde_command_expanded_in_label_and_body(self):
        q = ElevationQueue(commands=["bash ~/.claude/scripts/env/sudoers.sh fix"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        expanded = "bash /home/christina/.claude/scripts/env/sudoers.sh fix"
        assert f"# bootstrap-elevate: {expanded}\n{expanded}\n" in out
        assert "~/.claude" not in out
        # header notes the pre-expansion for the invoking user.
        assert "pre-expanded to the invoking user's home" in out

    def test_macos_home_var_command_expanded(self):
        q = ElevationQueue(commands=['install -m 0644 "$HOME/kitty.terminfo" /usr/share/x'])
        out = elev.render_script(q, "macos", "/p.sh")
        assert 'install -m 0644 "/home/christina/kitty.terminfo" /usr/share/x' in out
        assert "$HOME/kitty.terminfo" not in out

    def test_all_embedded_occurrences_expanded(self):
        q = ElevationQueue(commands=["bash ~/.claude/x.sh && cp ~/a ~/b && mv ${HOME}/c $HOME/d"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        assert ("bash /home/christina/.claude/x.sh && cp /home/christina/a "
                "/home/christina/b && mv /home/christina/c /home/christina/d") in out

    def test_rule_boundaries_no_shell_parsing(self):
        # $HOMEBREW_PREFIX is a different variable (word boundary); a tilde not
        # at start/after-whitespace is not a home reference. Both pass through.
        q = ElevationQueue(commands=["echo $HOMEBREW_PREFIX --opt=~/x a~/b"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        assert "echo $HOMEBREW_PREFIX --opt=~/x a~/b" in out

    def test_command_without_home_refs_unchanged(self):
        cmd = "curl -fsSL https://example.com/i.sh | sh -s -- --system"
        out = elev.render_script(ElevationQueue(commands=[cmd]), "ubuntu", "/p.sh")
        assert f"# bootstrap-elevate: {cmd}\n{cmd}\n" in out

    def test_apt_packages_not_touched(self):
        # Expansion applies to command strings only; apt lines are package names.
        q = ElevationQueue(apt_packages=["net-tools"])
        out = elev.render_script(q, "ubuntu", "/p.sh")
        assert "apt-get install -y net-tools" in out

    def test_windows_render_never_pre_expands(self, monkeypatch):
        # Windows commands are NOT pre-expanded at render time: the queued
        # command rides verbatim inside `"<bash.exe>" -c "..."`, where the
        # tilde expands natively as the invoking user when the .bat runs.
        monkeypatch.setattr(elev, "resolve_bash", lambda: FAKE_BASH)
        q = ElevationQueue(commands=["copy ~/x /y"])
        out = elev.render_script(q, "windows", "ignored")
        assert f'"{FAKE_BASH}" -c "copy ~/x /y"' in out
        assert "/home/christina" not in out


class TestRenderWindows:
    """Queued method:"command" entries run under elevated cmd.exe, which
    neither tilde-expands nor has bash on PATH (Git for Windows exposes
    Git\\cmd only; SessionStart finds bash because it runs inside Git Bash).
    So each command is rendered as `"<abs bash.exe>" -c "<command>"` --
    run_env_command's in-pass semantics: ~/$HOME expand INSIDE bash -c,
    launch is PATH-independent. bash is resolved at render time via the
    shim's single resolver (tool_check.resolve_bash)."""

    @pytest.fixture(autouse=True)
    def pinned_bash(self, monkeypatch):
        monkeypatch.setattr(elev, "resolve_bash", lambda: FAKE_BASH)

    def test_self_elevating_preamble_and_commands(self):
        q = ElevationQueue(commands=["Enable-WindowsOptionalFeature -Online -FeatureName X"])
        out = elev.render_script(q, "windows", "ignored")
        # self-elevation preamble (mirrors fix_python_path.bat).
        assert out.startswith("@echo off")
        assert "fsutil dirty query" in out
        assert "Start-Process -FilePath '%~f0' -Verb RunAs" in out
        assert ":is_admin" in out
        # the deferred command is present, wrapped in the absolute bash. The
        # body is authored with plain \n; CRLF is applied ONCE at write time
        # (see the on-disk bytes test below).
        assert (f'"{FAKE_BASH}" -c '
                '"Enable-WindowsOptionalFeature -Online -FeatureName X"') in out
        assert "\r" not in out
        # self-delete idiom present.
        assert '(goto) 2>nul & del "%~f0"' in out

    def test_tilde_command_golden_lines(self):
        # E4's shape: an env_check fix queued for elevation. Golden: echo
        # label (verbatim command), then the two-level cmd.exe line -- quoted
        # absolute bash.exe, -c, the command verbatim in ONE pair of double
        # quotes (tilde untouched: it expands inside bash -c on the target
        # machine) -- then the errorlevel check.
        cmd = "bash ~/.claude/scripts/env/ssh-server-windows.sh fix"
        out = elev.render_script(ElevationQueue(commands=[cmd]), "windows", "ignored")
        assert (
            f"echo bootstrap-elevate: {cmd}\n"
            f'"{FAKE_BASH}" -c "{cmd}"\n'
            "if %errorlevel% neq 0 goto :failed\n"
        ) in out
        # never rendered bare: every occurrence of the command is the echo
        # label or inside the bash -c wrapper.
        assert f"\n{cmd}\n" not in out
        # tilde is NOT pre-expanded (contrast with the unix renders).
        assert "~/.claude" in out

    def test_multiple_commands_each_wrapped(self):
        q = ElevationQueue(commands=["bash ~/a.sh fix", "bash ~/b.sh fix"])
        out = elev.render_script(q, "windows", "ignored")
        assert f'"{FAKE_BASH}" -c "bash ~/a.sh fix"' in out
        assert f'"{FAKE_BASH}" -c "bash ~/b.sh fix"' in out
        assert out.index("a.sh") < out.index("b.sh")

    def test_bash_unresolvable_fails_render_descriptively(self, monkeypatch):
        # No bash at render time -> fail the render (never emit a .bat whose
        # commands cannot run), with a descriptive, actionable message.
        monkeypatch.setattr(elev, "resolve_bash", lambda: None)
        with pytest.raises(RuntimeError, match="bash not found on PATH"):
            elev.render_script(ElevationQueue(commands=["bash ~/x.sh fix"]), "windows", "p")

    def test_double_quote_in_command_rejected(self):
        # The two-level `"bash.exe" -c "cmd"` line has no escaping rule; a
        # command carrying a double quote would break the .bat's quoting, so
        # the render rejects it descriptively instead.
        with pytest.raises(ValueError, match="double quote"):
            elev.render_script(
                ElevationQueue(commands=['sh -c "echo hi"']), "windows", "p")

    def test_unknown_os_raises(self):
        with pytest.raises(ValueError):
            elev.render_script(ElevationQueue(commands=["x"]), "plan9", "p")

    def test_engine_launch_arg_gates_success_pause_only(self):
        # The engine launches the .bat with /engine on a fix-all run and WAITS
        # on it; the success path must not block on a keypress then. The
        # failure path always pauses so errors stay readable.
        out = elev.render_script(ElevationQueue(commands=["x"]), "windows", "p")
        assert 'if /I "%~1"=="/engine" set "BOOTSTRAP_ENGINE_LAUNCH=1"' in out
        # Success path: pause is conditional on NOT being engine-launched.
        assert ("echo This script will now delete itself.\n"
                "echo.\n"
                "if not defined BOOTSTRAP_ENGINE_LAUNCH pause\n") in out
        # Failure path: unconditional pause (window stays visible on errors).
        failed_section = out.split("\n:failed\n", 1)[1]
        assert "\npause\n" in failed_section
        assert "if not defined" not in failed_section


# --------------------------------------------------------------------------- #
# write_or_clear_script: regenerate + cleanup semantics
# --------------------------------------------------------------------------- #

class TestWriteOrClearScript:
    def test_writes_script_when_nonempty(self, tmp_path):
        q = ElevationQueue(apt_packages=["net-tools"])
        path = elev.write_or_clear_script(q, str(tmp_path), "ubuntu")
        assert path is not None
        assert os.path.isfile(path)
        assert path.endswith(os.path.join("elevate", "install-elevated.sh"))
        assert "apt-get install -y net-tools" in open(path).read()

    def test_windows_script_basename_is_bat(self, tmp_path, monkeypatch):
        monkeypatch.setattr(elev, "resolve_bash", lambda: FAKE_BASH)
        q = ElevationQueue(commands=["x"])
        path = elev.write_or_clear_script(q, str(tmp_path), "windows")
        assert path.endswith("install-elevated.bat")

    def test_windows_on_disk_bytes_are_crlf_never_crcrlf(self, tmp_path, monkeypatch):
        # The .bat must land with CRLF endings on EVERY platform (batch parsing
        # requires them), applied exactly once: text-mode translation of a
        # pre-joined \r\n body would produce \r\r\n on Windows.
        monkeypatch.setattr(elev, "resolve_bash", lambda: FAKE_BASH)
        q = ElevationQueue(commands=["Enable-Feature X"])
        path = elev.write_or_clear_script(q, str(tmp_path), "windows")
        raw = open(path, "rb").read()
        assert b"\r\r\n" not in raw
        assert b"\r\n" in raw
        # No bare-\n lines: every \n is preceded by \r.
        assert raw.count(b"\n") == raw.count(b"\r\n")

    def test_bash_on_disk_bytes_have_no_cr(self, tmp_path):
        # The newline override is .bat-only; bash scripts stay LF.
        q = ElevationQueue(apt_packages=["net-tools"])
        path = elev.write_or_clear_script(q, str(tmp_path), "ubuntu")
        raw = open(path, "rb").read()
        assert b"\r" not in raw

    def test_empty_queue_removes_stale_script(self, tmp_path):
        # First pass writes a script; a later empty pass must delete it.
        q1 = ElevationQueue(apt_packages=["net-tools"])
        path = elev.write_or_clear_script(q1, str(tmp_path), "ubuntu")
        assert os.path.isfile(path)
        cleared = elev.write_or_clear_script(ElevationQueue(), str(tmp_path), "ubuntu")
        assert cleared is None
        assert not os.path.exists(path)

    def test_empty_queue_no_prior_script_is_noop(self, tmp_path):
        assert elev.write_or_clear_script(ElevationQueue(), str(tmp_path), "ubuntu") is None

    def test_regenerated_from_current_queue(self, tmp_path):
        # Second write with a different queue overwrites the first content.
        elev.write_or_clear_script(ElevationQueue(apt_packages=["net-tools"]), str(tmp_path), "ubuntu")
        path = elev.write_or_clear_script(ElevationQueue(apt_packages=["tmux"]), str(tmp_path), "ubuntu")
        content = open(path).read()
        assert "tmux" in content
        assert "net-tools" not in content


# --------------------------------------------------------------------------- #
# Aggregated fix-all item
# --------------------------------------------------------------------------- #

class TestElevationScriptFailure:
    def test_names_path_and_what_it_does(self):
        q = ElevationQueue(apt_packages=["net-tools", "tmux"], commands=["curl x | sh"])
        f = elev.elevation_script_failure(q, "ubuntu", "/data/elevate/install-elevated.sh")
        assert f["type"] == "elevation_script"
        assert f["persist_across_sessions"] is True
        assert f["script_path"] == "/data/elevate/install-elevated.sh"
        assert "/data/elevate/install-elevated.sh" in f["agent_msg"]
        assert "sudo bash" in f["agent_msg"]
        assert "apt-get install net-tools tmux" in f["agent_msg"]

    def test_macos_brew_summary(self):
        q = ElevationQueue(brew_installer=True)
        f = elev.elevation_script_failure(q, "macos", "/p.sh")
        assert "install Homebrew" in f["agent_msg"]
        assert 'bash "/p.sh"' in f["agent_msg"]

    def test_windows_double_click_instruction(self):
        q = ElevationQueue(commands=["x"])
        f = elev.elevation_script_failure(q, "windows", "C:/data/elevate/install-elevated.bat")
        assert "double-click" in f["agent_msg"]

    def test_no_launch_detail_leaves_messages_unprefixed(self):
        f = elev.elevation_script_failure(
            ElevationQueue(commands=["x"]), "windows", "C:/p.bat")
        assert "did not complete" not in f["user_msg"]
        assert "did not complete" not in f["agent_msg"]

    def test_launch_detail_prefixes_fallback_messages(self):
        # A fix-all run launched the script but it failed (UAC declined /
        # command failed / timeout): the item leads with the outcome and falls
        # back to the manual instruction -- never a re-prompt loop.
        detail = "The operation was canceled by the user"
        f = elev.elevation_script_failure(
            ElevationQueue(commands=["x"]), "windows", "C:/p.bat",
            launch_detail=detail)
        for key in ("user_msg", "agent_msg", "message"):
            assert f[key].startswith(
                "fix-all launched the elevation script but it did not "
                f"complete ({detail}). ")
        # Manual instruction still present as the fallback.
        assert "double-click" in f["agent_msg"]
        assert f["script_path"] == "C:/p.bat"


# --------------------------------------------------------------------------- #
# launch_elevation_script: the fix-all interactive launch (mocked -- no UAC)
# --------------------------------------------------------------------------- #

class _FakeProc:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


class TestLaunchElevationScript:
    def test_unix_returns_none_without_launching(self, monkeypatch):
        # No TTY in the fix-all run -> foreground sudo is not feasible; the
        # function must not even attempt a launch on ubuntu/macos.
        monkeypatch.setattr(
            elev.subprocess, "run",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("must not launch on unix")))
        assert elev.launch_elevation_script("/p.sh", "ubuntu") is None
        assert elev.launch_elevation_script("/p.sh", "macos") is None

    def test_windows_success_launches_runas_wait(self, monkeypatch):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return _FakeProc(returncode=0)

        monkeypatch.setattr(elev.subprocess, "run", fake_run)
        r = elev.launch_elevation_script(r"C:\data\elevate\install-elevated.bat", "windows")
        assert r.launched is True
        assert r.succeeded is True
        # The wait covers the REAL elevated process: the engine itself starts
        # the .bat elevated (-Verb RunAs -Wait -PassThru), so the .bat's UAC
        # self-relaunch hop never fires (no wrapper-exits-early early return).
        ps_command = calls["cmd"][-1]
        assert "-Verb RunAs" in ps_command
        assert "-Wait" in ps_command
        assert "-PassThru" in ps_command
        assert "-ArgumentList '/engine'" in ps_command
        assert r"C:\data\elevate\install-elevated.bat" in ps_command
        assert calls["cmd"][0].lower().endswith("powershell.exe")
        # Bounded wait: a walked-away UAC prompt cannot hang the hook forever.
        assert calls["kwargs"]["timeout"] == elev.ELEVATION_LAUNCH_TIMEOUT

    def test_windows_uac_decline_reports_stderr_detail(self, monkeypatch):
        monkeypatch.setattr(
            elev.subprocess, "run",
            lambda *a, **k: _FakeProc(
                returncode=1,
                stderr="Start-Process : This command cannot be run...\n"
                       "The operation was canceled by the user"))
        r = elev.launch_elevation_script("C:/p.bat", "windows")
        assert r.launched is True
        assert r.succeeded is False
        assert "canceled by the user" in r.detail

    def test_windows_failed_command_reports_exit_code(self, monkeypatch):
        monkeypatch.setattr(elev.subprocess, "run",
                            lambda *a, **k: _FakeProc(returncode=2))
        r = elev.launch_elevation_script("C:/p.bat", "windows")
        assert r.succeeded is False
        assert "exit code 2" in r.detail

    def test_windows_timeout_is_bounded_not_hung(self, monkeypatch):
        import subprocess as _sp

        def fake_run(*a, **k):
            raise _sp.TimeoutExpired(cmd="powershell", timeout=k["timeout"])

        monkeypatch.setattr(elev.subprocess, "run", fake_run)
        r = elev.launch_elevation_script("C:/p.bat", "windows", timeout=600)
        assert r.launched is True
        assert r.succeeded is False
        assert "timed out after 600s" in r.detail

    def test_windows_oserror_is_not_launched(self, monkeypatch):
        def fake_run(*a, **k):
            raise OSError("powershell missing")

        monkeypatch.setattr(elev.subprocess, "run", fake_run)
        r = elev.launch_elevation_script("C:/p.bat", "windows")
        assert r.launched is False
        assert r.succeeded is False
        assert "could not launch" in r.detail

    def test_windows_path_apostrophe_escaped_for_powershell(self, monkeypatch):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            return _FakeProc(returncode=0)

        monkeypatch.setattr(elev.subprocess, "run", fake_run)
        elev.launch_elevation_script(r"C:\Users\o'brien\install-elevated.bat", "windows")
        assert r"C:\Users\o''brien\install-elevated.bat" in calls["cmd"][-1]
