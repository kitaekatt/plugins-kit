"""The relaunch-kit launcher: lib/relaunch.py, bin/clauder and bin/clauder.cmd.

Logic runs in-process against a fake claude (fake_claude.py). The shims are run
for real under /bin/sh, bash and zsh, and the whole chain runs under a pty.
The cmd.exe and Windows command-line behaviour is pinned by golden tests of
the strings the launcher builds and of the shim's text.
"""

import json
import os
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import relaunch
from conftest import BIN, LIB, PLUGIN, posix_only

SID = "0b7e6c2a-1111-4222-8333-444455556666"


def run(fake, args, **kwargs):
    kwargs.setdefault("interactive", True)
    return relaunch.main(list(args), env=fake.env(), **kwargs)


def argvs(fake):
    return [c["argv"] for c in fake.calls()]


# --- locating claude ---------------------------------------------------------


@posix_only
def test_path_search_skips_own_bin_and_non_executables(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for d in (a, b, c):
        d.mkdir()
    (a / "claude").write_text("x")  # not executable
    own = relaunch.own_bin_dir()
    (b / "claude").write_text("x")
    (b / "claude").chmod(0o755)
    (c / "claude").write_text("x")
    (c / "claude").chmod(0o755)
    path = os.pathsep.join(["", str(a), own, str(b), str(c)])
    assert relaunch.find_on_path("claude", path, windows=False, pathext="", skip_dirs=[own]) == str(b / "claude")
    assert relaunch.find_on_path("claude", str(a), windows=False, pathext="", skip_dirs=[]) is None
    assert relaunch.find_on_path("claude", str(b), windows=False, pathext="", skip_dirs=[str(b)]) is None


def test_windows_path_search_follows_path_then_pathext_order(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "claude.cmd").write_text("x")
    (second / "claude.exe").write_text("x")
    both = tmp_path / "both"
    both.mkdir()
    (both / "claude.cmd").write_text("x")
    (both / "claude.exe").write_text("x")
    # Windows is case-insensitive; this host is not, so compare the lowered path.
    find = lambda path: relaunch.find_on_path(  # noqa: E731
        "claude", path, windows=True, pathext=".COM;.EXE;.BAT;.CMD", skip_dirs=[]).lower()
    assert find(";".join([str(first), str(second)])) == str(first / "claude.cmd").lower()
    assert find(str(both)) == str(both / "claude.exe").lower()  # same directory: PATHEXT order
    assert find(";".join([str(tmp_path / "none"), str(second)])) == str(second / "claude.exe").lower()


def test_pinned_target_is_used_verbatim_and_must_exist(tmp_path):
    exe = tmp_path / "pinned"
    exe.write_text("x")
    assert relaunch.resolve_target({relaunch.TARGET_ENV: str(exe), "PATH": ""}, windows=False) == str(exe)
    with pytest.raises(relaunch.LaunchError, match="absolute path of an existing file"):
        relaunch.resolve_target({relaunch.TARGET_ENV: str(tmp_path / "missing")}, windows=False)
    with pytest.raises(relaunch.LaunchError, match="absolute path of an existing file"):
        relaunch.resolve_target({relaunch.TARGET_ENV: "claude"}, windows=False)
    with pytest.raises(relaunch.LaunchError, match="not found on PATH"):
        relaunch.resolve_target({"PATH": str(tmp_path)}, windows=False)


@pytest.mark.parametrize("name,kind", [("claude.exe", "native"), ("CLAUDE.COM", "native"),
                                       ("claude.cmd", "batch"), ("claude.BAT", "batch")])
def test_windows_target_kind_is_decided_by_extension(name, kind):
    assert relaunch.classify_target("C:\\x\\" + name, windows=True) == kind


def test_windows_target_with_another_extension_is_refused():
    with pytest.raises(relaunch.LaunchError, match="only .exe, .com, .cmd and .bat"):
        relaunch.classify_target("C:\\x\\claude.ps1", windows=True)
    assert relaunch.classify_target("/usr/bin/claude", windows=False) == "native"


# --- Windows command lines (golden) ------------------------------------------


def test_native_command_line_golden():
    line = relaunch.native_command_line(
        r"C:\Program Files\Claude\claude.exe", ["a b", 'say "hi"', "x&y", "", r"C:\dir\ "])
    assert line == r'"C:\Program Files\Claude\claude.exe" "a b" "say \"hi\"" x&y "" "C:\dir\ "'


def test_batch_command_line_golden():
    line = relaunch.batch_command_line(
        r"C:\Windows\system32\cmd.exe",
        r"C:\Users\Me\AppData\Roaming\npm\claude.cmd",
        ["-p", "a b&c", "x|y", "(z)", "^c", "!x!", "<in>", "C:\\dir\\", "", "it's", "a;b,c=d"],
    )
    assert line == (
        r'C:\Windows\system32\cmd.exe /d /v:off /s /c "'
        r'"C:\Users\Me\AppData\Roaming\npm\claude.cmd" "-p" "a b&c" "x|y" "(z)" "^c" "!x!" "<in>" '
        r'"C:\dir\\" "" "it' "'" r's" "a;b,c=d"'
        '"'
    )


def test_batch_command_line_quotes_a_comspec_with_spaces():
    line = relaunch.batch_command_line(r"C:\Program Files\cmd.exe", r"C:\c.cmd", [])
    assert line == r'"C:\Program Files\cmd.exe" /d /v:off /s /c ""C:\c.cmd""'


@pytest.mark.parametrize("arg,label", [
    ('say "hi"', "a double quote"),
    ("100%", "a percent sign"),
    ("%PATH%", "a percent sign"),
    ("a\rb", "a carriage return"),
    ("a\nb", "a line feed"),
    ("a\x00b", "a NUL"),
])
def test_batch_command_line_refuses_what_cmd_would_alter(arg, label):
    with pytest.raises(relaunch.UnsafeArgument) as info:
        relaunch.batch_command_line(r"C:\cmd.exe", r"C:\c.cmd", ["ok", arg])
    assert label in str(info.value)
    assert "argument 2" in str(info.value)


def test_batch_command_line_refuses_an_unsafe_claude_path():
    with pytest.raises(relaunch.UnsafeArgument, match="claude path"):
        relaunch.batch_command_line(r"C:\cmd.exe", r"C:\100%\c.cmd", [])


# --- the cmd.exe shim boundary (pure) ---------------------------------------

PREFIX = '"C:\\py\\python.exe" "C:\\p\\lib\\relaunch.py"'


def test_cmd_shim_arguments_reads_bare_and_quoted_tokens():
    assert relaunch.cmd_shim_arguments(PREFIX) == []
    assert relaunch.cmd_shim_arguments(PREFIX + ' --model opus "fix the bug" ""  x') == [
        "--model", "opus", "fix the bug", "", "x"]
    assert relaunch.cmd_shim_arguments(PREFIX + ' "a&b" "(x)" "C:\\dir\\sub"') == ["a&b", "(x)", "C:\\dir\\sub"]


@pytest.mark.parametrize("tail,why", [
    ('a"b c"', "a double quote inside an argument"),
    ('"a"b', "a double quote inside an argument"),
    ('"unterminated', "an unterminated double quote"),
    ('"C:\\dir\\"', "a backslash before the closing quote"),
    ('"C:\\dir\\\\"', "a backslash before the closing quote"),
    ('{"a":1}', "a double quote inside an argument"),
])
def test_cmd_shim_arguments_refuses_what_cmd_and_the_program_would_read_differently(tail, why):
    with pytest.raises(relaunch.UnsafeArgument) as info:
        relaunch.cmd_shim_arguments(PREFIX + " " + tail)
    assert why in str(info.value)
    assert "clauder.cmd cannot forward" in str(info.value)


def test_cmd_shim_arguments_needs_the_two_quoted_leading_tokens():
    with pytest.raises(relaunch.LaunchError, match="unexpected command line"):
        relaunch.cmd_shim_arguments("python.exe launcher.py a")


def test_main_refuses_when_the_cmd_shim_text_and_argv_disagree(fake_claude, capsys):
    status = relaunch.main(
        ["x"], env={**fake_claude.env(), relaunch.SHIM_ENV: "cmd"}, interactive=True, windows=True,
        raw_command_line=lambda: PREFIX + " y")
    assert status == 2
    assert "differ from the command line cmd.exe built" in capsys.readouterr().err
    status = relaunch.main(
        ["a\"b"], env={**fake_claude.env(), relaunch.SHIM_ENV: "cmd"}, interactive=True, windows=True,
        raw_command_line=lambda: PREFIX + ' a"b')
    assert status == 2
    assert "cannot forward" in capsys.readouterr().err
    assert fake_claude.calls() == []


# --- the shim text (golden) ---------------------------------------------------


def _cmd_lines():
    return (BIN / "clauder.cmd").read_text(encoding="ascii").splitlines()


def test_cmd_shim_forwards_arguments_once_verbatim_with_delayed_expansion_off():
    lines = _cmd_lines()
    code = [l for l in lines if l.strip() and not l.lower().startswith("rem")]
    assert code[0] == "@echo off"
    assert code[1] == "setlocal DisableDelayedExpansion"
    assert code[-2:] == ['"%RK_PY%" "%RK_LAUNCHER%" %*', "exit /b %ERRORLEVEL%"]
    assert lines[-1] == "exit /b %ERRORLEVEL%"
    assert [l for l in lines if "%*" in l] == ['"%RK_PY%" "%RK_LAUNCHER%" %*']  # not even in a comment
    text = "\n".join(lines).lower()
    assert "enabledelayedexpansion" not in text
    for forbidden in ("shift", "%1", "%~1", "!"):
        assert forbidden not in "\n".join(l for l in lines if not l.lower().startswith("rem")), forbidden
    assert code[-3] == 'set "RELAUNCH_KIT_SHIM=cmd"'
    assert not any(l.lstrip().startswith(":") for l in lines), "no labels: the file is stored with LF endings"


def test_cmd_shim_states_its_boundary():
    text = (BIN / "clauder.cmd").read_text(encoding="ascii")
    for needle in ("ARGUMENT FORWARDING BOUNDARY", "RELAUNCH_KIT_SHIM=cmd", "native shim", "bare one is consumed by cmd"):
        assert needle in text


def test_shims_and_libs_are_ascii():
    for path in list(BIN.iterdir()) + list(LIB.glob("*.py")):
        path.read_bytes().decode("ascii")


# --- token file validation ----------------------------------------------------


@pytest.mark.parametrize("override,fragment", [
    ({"schema": 2}, "schema is 2"),
    ({"schema": True}, "schema is True"),
    ({"token": "other"}, "not this launch's token"),
    ({"action": "explode"}, "action 'explode'"),
    ({"session_id": "bad id"}, "session_id 'bad id'"),
    ({"session_id": "<drop>"}, "missing: session_id"),
    ({"session_id": "-abc"}, "starts with a dash"),
    ({"extra": 1}, "unexpected: extra"),
    ({"cwd": "/no/such/directory/here"}, "not an existing absolute directory"),
    ({"cwd": "relative"}, "not an existing absolute directory"),
    ({"reason": "because"}, "reason 'because'"),
    ({"written_at": "yesterday"}, "written_at 'yesterday'"),
    ({"written_at": "2026-13-45T00:00:00Z"}, "not a valid time"),
    ({"action": "prompt"}, "requires a prompt"),
    ({"action": "prompt", "prompt": "two\nlines"}, "one line"),
    ({"action": "prompt", "prompt": "x" * 5000}, "longer than 4096"),
    ({"action": "prompt", "prompt": "mcp"}, "subcommand name"),
    ({"action": "prompt", "prompt": "   "}, "must not be empty"),
    ({"prompt": 5}, "string or null"),
    ({"writer": ""}, "writer"),
    ({"raw": "not json"}, "cannot read the file as UTF-8 JSON"),
    ({"raw": "[]"}, "does not hold a JSON object"),
])
def test_invalid_token_file_is_renamed_and_nothing_relaunches(fake_claude, capsys, override, fragment):
    fake_claude.script([{"request": override}, {"exit": 0}])
    status = run(fake_claude, [])
    err = capsys.readouterr().err
    assert status == 1  # claude exited 0, a rejected request must not look like success
    assert len(fake_claude.calls()) == 1
    assert "not relaunching" in err and fragment in err
    rejected = list(fake_claude.rendezvous().glob("*.rejected.json"))
    assert len(rejected) == 1 and str(rejected[0]) in err
    assert list(fake_claude.rendezvous().glob("*.json")) == rejected


def test_rejected_request_keeps_a_nonzero_claude_status(fake_claude):
    fake_claude.script([{"request": {"schema": 9}, "exit": 4}])
    assert run(fake_claude, []) == 4


# --- the loop ----------------------------------------------------------------


def test_no_request_returns_claudes_status_and_exports_the_token(fake_claude):
    fake_claude.script([{"exit": 7}])
    assert run(fake_claude, ["--model", "m", "hello"]) == 7
    (call,) = fake_claude.calls()
    assert call["argv"] == ["--model", "m", "hello"]
    assert call["dir"] == str(fake_claude.rendezvous())
    assert len(call["token"]) == 32 and call["shim"] is None
    assert list(fake_claude.rendezvous().iterdir()) == []


def test_resume_relaunches_the_session_in_the_requested_directory(fake_claude, tmp_path, capsys):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake_claude.script([{"request": {"cwd": str(elsewhere)}}, {"exit": 3}])
    assert run(fake_claude, ["--model", "m", "-c", "first prompt"]) == 3
    first, second = fake_claude.calls()
    assert first["argv"] == ["--model", "m", "-c", "first prompt"]  # the first launch is verbatim
    assert second["argv"] == ["--model", "m", "--resume", SID]  # selector and prompt dropped
    assert os.path.realpath(second["cwd"]) == os.path.realpath(elsewhere)
    assert first["token"] != second["token"]
    assert "[clauder] relaunching (restart): resume %s" % SID in capsys.readouterr().err
    assert list(fake_claude.rendezvous().iterdir()) == []


def test_prompt_action_starts_a_new_session_with_the_prompt(fake_claude, capsys):
    fake_claude.script([
        {"request": {"action": "prompt", "prompt": "/task work dev/tasks/x", "reason": "hand-off"}},
        {"exit": 0},
    ])
    assert run(fake_claude, ["--add-dir", "a", "--resume", SID, "-n", "my name"]) == 0
    assert argvs(fake_claude)[1] == ["--add-dir", "a", "-n", "my name", "--", "/task work dev/tasks/x"]
    assert "[clauder] relaunching (hand-off): /task work dev/tasks/x" in capsys.readouterr().err


def test_resume_with_a_prompt_is_the_relaunch_check_hop(fake_claude):
    fake_claude.script([{"request": {"prompt": "/relaunch-check 1 run", "reason": "check"}}, {}])
    assert run(fake_claude, []) == 0
    assert argvs(fake_claude)[1] == ["--resume", SID, "--", "/relaunch-check 1 run"]


def test_prompt_starting_with_a_dash_survives_the_relaunch(fake_claude):
    fake_claude.script([{"request": {"action": "prompt", "prompt": "-p is not a flag here"}}, {}])
    assert run(fake_claude, ["--tools", "Bash"]) == 0
    assert argvs(fake_claude)[1] == ["--tools", "Bash", "--", "-p is not a flag here"]


def test_three_hop_chain_stays_under_the_loop_guard(fake_claude):
    fake_claude.script([
        {"request": {"prompt": "/relaunch-check 1 r", "reason": "check"}},
        {"request": {"action": "prompt", "prompt": "/relaunch-check 0 r", "reason": "check"}},
        {"exit": 0},
    ])
    assert run(fake_claude, ["/relaunch-check 2 r"]) == 0
    assert len(fake_claude.calls()) == 3
    assert len({c["token"] for c in fake_claude.calls()}) == 3


@pytest.mark.parametrize("age", [0, 100000])
def test_tombstone_is_honoured_at_any_age_and_deleted(fake_claude, capsys, age):
    fake_claude.script([{"request": {"action": "cancelled", "age_s": age}, "exit": 5}, {"exit": 0}])
    assert run(fake_claude, []) == 5
    assert len(fake_claude.calls()) == 1
    assert list(fake_claude.rendezvous().iterdir()) == []
    assert capsys.readouterr().err == ""


def test_actionable_file_older_than_120_seconds_is_not_acted_on(fake_claude, capsys):
    fake_claude.script([{"request": {"age_s": 200}, "exit": 6}, {"exit": 0}])
    assert run(fake_claude, []) == 6
    assert len(fake_claude.calls()) == 1
    assert list(fake_claude.rendezvous().iterdir()) == []
    assert "written 200 s before claude exited" in capsys.readouterr().err


def test_actionable_file_just_inside_120_seconds_is_acted_on(fake_claude):
    fake_claude.script([{"request": {"age_s": 110}}, {"exit": 0}])
    run(fake_claude, [])
    assert len(fake_claude.calls()) == 2


def test_loop_guard_stops_the_fourth_fast_request(fake_claude, capsys):
    fake_claude.script([{"request": {}} for _ in range(6)] + [{"exit": 0}])
    status = run(fake_claude, [])
    err = capsys.readouterr().err
    assert len(fake_claude.calls()) == 4  # three relaunches; the 4th request is refused
    assert status == 1
    assert "stopping: 4 restart requests in a row" in err
    kept = list(fake_claude.rendezvous().glob("*.rejected.json"))
    assert len(kept) == 1 and str(kept[0]) in err
    assert list(fake_claude.rendezvous().glob("*.json")) == kept


def test_a_slow_run_resets_the_loop_guard(fake_claude, monkeypatch):
    monkeypatch.setattr(relaunch, "FAST_RUN_S", 2.0)
    fake_claude.script([{"request": {}}, {"request": {}}, {"request": {}, "sleep": 2.3},
                        {"request": {}}, {"request": {}}, {"exit": 0}])
    assert run(fake_claude, []) == 0
    assert len(fake_claude.calls()) == 6


def test_sweep_removes_only_stale_token_files(fake_claude):
    directory = fake_claude.rendezvous()
    directory.mkdir(parents=True)
    now = time.time()
    files = {
        "old.json": 25 * 3600, "fresh.json": 3600,
        "old.rejected.json": 8 * 24 * 3600, "recent.rejected.json": 3 * 24 * 3600,
        "check-run.log": 30 * 24 * 3600,
    }
    for name, age in files.items():
        (directory / name).write_text("{}")
        os.utime(directory / name, (now - age, now - age))
    fake_claude.script([{"exit": 0}])
    run(fake_claude, [])
    assert sorted(p.name for p in directory.iterdir()) == ["check-run.log", "fresh.json", "recent.rejected.json"]


def test_default_rendezvous_directory_follows_the_data_root_rule(tmp_path):
    with_root = relaunch.rendezvous_dir({"CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path)})
    assert with_root == str(tmp_path / "plugins-kit" / "relaunch-kit" / "relaunch")
    home = relaunch.rendezvous_dir({})
    assert home == os.path.join(os.path.expanduser("~"), ".claude", "plugins", "data",
                                "plugins-kit", "relaunch-kit", "relaunch")


def test_a_token_from_an_outer_session_is_replaced_not_inherited(fake_claude):
    fake_claude.script([{"exit": 0}])
    env = fake_claude.env()
    env[relaunch.TOKEN_ENV] = "outer"
    env[relaunch.DIR_ENV] = "/outer"
    relaunch.main([], env=env, interactive=True)
    (call,) = fake_claude.calls()
    assert call["token"] not in ("outer", None) and call["dir"] == str(fake_claude.rendezvous())


# --- batch targets in the loop -------------------------------------------------


def test_relaunch_refuses_an_unsafe_prompt_for_a_cmd_target(tmp_path, monkeypatch, capsys):
    """The prompt arrives after claude exited, so the refusal keeps the file and says so."""
    directory = tmp_path / "data" / "plugins-kit" / "relaunch-kit" / "relaunch"
    launches = []

    def fake_spawn(target, kind, args, *, cwd, env, windows):
        launches.append(list(args))
        if len(launches) == 1:
            token = env[relaunch.TOKEN_ENV]
            body = {"schema": 1, "token": token, "action": "prompt", "session_id": SID,
                    "cwd": str(tmp_path), "prompt": "echo 100% done", "reason": "restart",
                    "written_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "writer": "t"}
            Path(env[relaunch.DIR_ENV], token + ".json").write_text(json.dumps(body))
        return 0

    monkeypatch.setattr(relaunch, "spawn", fake_spawn)
    env = {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"), "COMSPEC": r"C:\Windows\system32\cmd.exe"}
    import claude_args
    status = relaunch.run_loop("C:\\npm\\claude.cmd", "batch", [], claude_args.parse([]), env, windows=True)
    assert status == 2
    assert len(launches) == 1
    err = capsys.readouterr().err
    assert "a percent sign" in err and "not relaunching" in err
    assert len(list(directory.glob("*.rejected.json"))) == 1


# --- pass-through ---------------------------------------------------------------


def passthrough(fake, args, **kwargs):
    seen = []
    status = relaunch.main(list(args), env=fake.env(), exec_fn=lambda t, a, e: seen.append((t, a, dict(e))), **kwargs)
    return status, seen


@pytest.mark.parametrize("args", [
    ["-p", "hi"], ["--print", "hi"], ["--bg"], ["--background"], ["--desktop"], ["--cloud"], ["--cloud", "task"],
    ["-h"], ["--help"], ["-v"], ["--version"], ["--environment", "ccpool_1"], ["mcp", "list"],
    ["--model", "m", "update"], ["-cp", "hi"], ["--", "doctor"],
])
def test_modes_that_never_return_to_an_interactive_loop_run_claude_once(fake_claude, args):
    status, seen = passthrough(fake_claude, args, interactive=True)
    assert status == 0
    (target, argv, env), = seen
    assert target == str(fake_claude.exe) and argv == [target] + args
    assert relaunch.TOKEN_ENV not in env and relaunch.DIR_ENV not in env
    assert fake_claude.calls() == [] and not fake_claude.rendezvous().exists()


def test_without_a_terminal_claude_runs_once_unparsed_and_without_a_token(fake_claude):
    env = fake_claude.env()
    env[relaunch.TOKEN_ENV] = "outer"
    env[relaunch.DIR_ENV] = "/outer"
    seen = []
    relaunch.main(["--not-a-claude-flag", "x"], env=env, interactive=False,
                  exec_fn=lambda t, a, e: seen.append((a, dict(e))))
    (argv, child), = seen
    assert argv[1:] == ["--not-a-claude-flag", "x"]
    assert relaunch.TOKEN_ENV not in child and relaunch.DIR_ENV not in child


@pytest.mark.parametrize("args,fragment", [
    (["--not-a-claude-flag"], "unknown option '--not-a-claude-flag'"),
    (["--model"], "'--model' argument missing"),
])
def test_unknown_flag_is_refused_before_anything_launches(fake_claude, capsys, args, fragment):
    status, seen = passthrough(fake_claude, args, interactive=True)
    assert status == 2 and seen == [] and fake_claude.calls() == []
    err = capsys.readouterr().err
    assert err.startswith("clauder: ") and fragment in err


def test_missing_claude_is_refused(tmp_path, capsys):
    status = relaunch.main([], env={"PATH": str(tmp_path)}, interactive=True)
    assert status == 2 and "not found on PATH" in capsys.readouterr().err


# --- signals and exit status ------------------------------------------------------


def test_exit_status_of_a_killed_child_is_128_plus_the_signal():
    assert relaunch._exit_status(-9) == 137
    assert relaunch._exit_status(0) == 0
    assert relaunch._exit_status(42) == 42


@posix_only
def test_interrupt_signals_do_not_kill_the_launcher_while_claude_runs(monkeypatch):
    before = signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGQUIT)
    with relaunch.interrupts_deferred():
        os.kill(os.getpid(), signal.SIGINT)
        os.kill(os.getpid(), signal.SIGQUIT)
        time.sleep(0.05)
        child = subprocess.run(
            [sys.executable, "-c", "import signal;print(signal.getsignal(signal.SIGINT) is signal.SIG_IGN,"
             "signal.getsignal(signal.SIGQUIT) is signal.SIG_IGN)"],
            capture_output=True, text=True)
    assert child.stdout.split() == ["False", "False"]  # a no-op handler, never SIG_IGN: the child does not inherit it
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGQUIT)) == before


# --- bin/clauder under real shells -----------------------------------------------

SHELLS = sorted({s for s in ("/bin/sh", "/bin/bash", shutil.which("bash"), shutil.which("zsh")) if s and os.path.exists(s)})
_TOOLS = ("dirname", "uname", "tr", "readlink")


class Sandbox:
    """The sh shim copied beside a stub lib, with a PATH holding only what the shim may call."""

    def __init__(self, tmp_path, subdir="plugin"):
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.root = tmp_path / subdir
        (self.root / "bin").mkdir(parents=True)
        (self.root / "lib").mkdir()
        shutil.copy2(BIN / "clauder", self.root / "bin" / "clauder")
        (self.root / "lib" / "relaunch.py").write_text("# stub\n")
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        for tool in _TOOLS:
            os.symlink(shutil.which(tool), self.tools / tool)
        self.standalone = self.home / ".local" / "share" / "python-standalone"

    @staticmethod
    def interpreter(path, marker):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('#!/bin/sh\necho "%s"\nfor a in "$@"; do echo "ARG:$a"; done\n' % marker)
        path.chmod(0o755)
        return path

    def run(self, shell, args=(), path_python=None, **env_extra):
        env = {"HOME": str(self.home), "PATH": str(self.tools)}
        if path_python:
            self.interpreter(self.tools / path_python, "PATH-" + path_python)
        env.update({k: str(v) for k, v in env_extra.items()})
        return subprocess.run([shell, str(self.root / "bin" / "clauder"), *args], env=env,
                              capture_output=True, text=True, timeout=60)


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_sh_shim_prefers_the_standalone_python_and_forwards_arguments_exactly(tmp_path, shell):
    box = Sandbox(tmp_path)
    box.interpreter(box.standalone / "python" / "bin" / "python3", "STANDALONE")
    result = box.run(shell, ["a b", "", "$HOME", "--x=y", "it's"], path_python="python3")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "STANDALONE", "ARG:%s" % (box.root / "lib" / "relaunch.py"),
        "ARG:a b", "ARG:", "ARG:$HOME", "ARG:--x=y", "ARG:it's"]


@posix_only
def test_sh_shim_accepts_bootstrap_python_only_inside_the_standalone_dir(tmp_path):
    box = Sandbox(tmp_path)
    inside = box.interpreter(box.standalone / "python" / "bin" / "other", "INSIDE")
    stranger = box.interpreter(tmp_path / "elsewhere" / "python", "STRANGER")
    assert "INSIDE" in box.run("/bin/sh", path_python="python3", BOOTSTRAP_PYTHON=inside).stdout
    assert "PATH-python3" in box.run("/bin/sh", path_python="python3", BOOTSTRAP_PYTHON=stranger).stdout
    link = box.standalone / "python" / "bin" / "escape"
    os.symlink(stranger, link)
    assert "PATH-python3" in box.run("/bin/sh", path_python="python3", BOOTSTRAP_PYTHON=link).stdout


@posix_only
def test_sh_shim_without_any_python_exits_1(tmp_path):
    result = Sandbox(tmp_path).run("/bin/sh")
    assert result.returncode == 1 and "no Python interpreter found" in result.stderr


@posix_only
def test_sh_shim_reports_a_missing_launcher(tmp_path):
    box = Sandbox(tmp_path)
    (box.root / "lib" / "relaunch.py").unlink()
    result = box.run("/bin/sh", path_python="python3")
    assert result.returncode == 2 and "launcher not found" in result.stderr


@posix_only
@pytest.mark.parametrize("subdir,marketplace_env,refused", [
    ("home/.claude/plugins/cache/plugins-kit/relaunch-kit/1.0.0", "plugins-kit", True),
    ("home/.claude/plugins/cache/plugins-kit/relaunch-kit/1.0.0", "Plugins-Kit", True),
    ("home/.claude/plugins/cache/plugins-kit/relaunch-kit/1.0.0", None, True),
    ("home/.claude/plugins/cache/other-market/relaunch-kit/1.0.0", "plugins-kit", False),
    ("home/.claude/plugins/data/plugins-kit/relaunch-kit", "plugins-kit", True),
    ("home/.claude/plugins/data/plugins-kit/relaunch-kit", None, True),
    ("working/relaunch-kit", "plugins-kit", False),
])
def test_sh_shim_refuses_installed_copies_under_claudx(tmp_path, subdir, marketplace_env, refused):
    box = Sandbox(tmp_path, subdir=subdir)
    extra = {"CLAUDE_PLUGIN_TEST": "1"}
    if marketplace_env:
        extra["CLAUDE_PLUGIN_TEST_MARKETPLACE"] = marketplace_env
    result = box.run("/bin/sh", path_python="python3", **extra)
    if refused:
        assert result.returncode == 2
        assert "refusing to run the cached copy" in result.stderr and "CLAUDE_PLUGIN_TEST" in result.stderr
        assert "PATH-python3" not in result.stdout
    else:
        assert result.returncode == 0 and "PATH-python3" in result.stdout
    outside = box.run("/bin/sh", path_python="python3")  # no claudx: nothing is refused
    assert outside.returncode == 0


# --- the whole chain under a pty -----------------------------------------------------


def run_in_pty(command, env, timeout=90):
    master, slave = pty.openpty()
    proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
                            start_new_session=True)
    os.close(slave)
    output = b""
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            elif proc.poll() is not None:
                break
        status = proc.wait(timeout=max(1, deadline - time.time()))
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return status, output.decode("utf-8", "replace")


def _chain_env(fake):
    env = fake.env()
    env["PATH"] = os.pathsep.join([os.path.dirname(sys.executable), "/usr/bin", "/bin"])
    return env


@posix_only
def test_clauder_relaunches_in_the_same_terminal_through_the_real_shim(fake_claude):
    fake_claude.script([
        {"request": {"action": "prompt", "prompt": "/task work dev/tasks/x", "reason": "hand-off"}},
        {"request": {"prompt": "/relaunch-check 0 r", "reason": "check"}},
        {"exit": 3},
    ])
    status, output = run_in_pty([str(BIN / "clauder"), "--model", "m", "start here"], _chain_env(fake_claude))
    assert status == 3, output
    assert argvs(fake_claude) == [
        ["--model", "m", "start here"],
        ["--model", "m", "--", "/task work dev/tasks/x"],
        ["--model", "m", "--resume", SID, "--", "/relaunch-check 0 r"],
    ]
    assert "[clauder] relaunching (hand-off): /task work dev/tasks/x" in output
    assert len({c["token"] for c in fake_claude.calls()}) == 3
    assert list(fake_claude.rendezvous().iterdir()) == []


@posix_only
def test_clauder_without_a_terminal_is_a_plain_exec_of_claude(fake_claude):
    fake_claude.script([{"exit": 5}])
    result = subprocess.run([str(BIN / "clauder"), "--not-a-claude-flag", "a b"], env=_chain_env(fake_claude),
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert result.returncode == 5, result.stderr
    (call,) = fake_claude.calls()
    assert call["argv"] == ["--not-a-claude-flag", "a b"]
    assert call["token"] is None and call["dir"] is None
