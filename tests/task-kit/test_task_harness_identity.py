"""Who is calling: the harness identity, the machine, the exact process stamp, the lock, the data directory.

These modules sit in ``task_system`` so ``task.py`` can use them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
import yaml
from dashboard_support import SCRIPTS_DIR
from fake_claude import FakeClaude, bash_available

from task_system import datadir, harness, machine, oslock, procstamp

needs_bash = pytest.mark.skipif(not bash_available(), reason="the Bash tool process shape needs bash")

PROBE = (
    "import json, os, psutil; from task_system import harness, procstamp;"
    "ident = harness.resolve_identity();"
    "near = next(p for p in psutil.Process().parents() if 'claude' in ' '.join(p.cmdline()[:3]).lower());"
    "print(json.dumps({'session': ident.session_id, 'pid': ident.pid, 'stamp': ident.stamp.to_dict(),"
    " 'nearest_claude_looking_ancestor': near.pid, 'claude_pid_env': os.environ['CLAUDE_PID']}))"
)


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def probe_env() -> dict:
    return dict(os.environ, PYTHONPATH=str(SCRIPTS_DIR))


# -- the harness process, from inside a Bash tool command's process tree ---------------------------------


@needs_bash
def test_identity_inside_a_tool_shell_is_the_claude_process_not_the_shell():
    """The shell's command line mentions claude (the snapshot path) and the old walk stopped there."""
    command = f"{sys.executable} -c \"{PROBE}\""
    with FakeClaude("sess-0001", command, env=probe_env()) as fake:
        assert fake.result["returncode"] == 0, fake.result["stderr"]
        seen = json.loads(fake.result["stdout"])
        assert seen["pid"] == fake.pid == int(seen["claude_pid_env"])
        assert seen["session"] == "sess-0001"
        assert seen["stamp"] == procstamp.stamp_of(fake.pid).to_dict()
        # the walk this replaces would have stopped at the transient shell, which is a different process
        assert seen["nearest_claude_looking_ancestor"] != fake.pid


def test_a_claude_pid_naming_a_live_non_ancestor_is_refused(sleeper):
    env = {"CLAUDE_PID": str(sleeper.pid), "CLAUDE_CODE_SESSION_ID": "s"}
    with pytest.raises(harness.IdentityError, match="not an ancestor"):
        harness.resolve_identity(env)


def test_claude_pid_unset_with_a_session_id_set_is_refused():
    with pytest.raises(harness.IdentityError, match="CLAUDE_PID is not set"):
        harness.resolve_identity({"CLAUDE_CODE_SESSION_ID": "s"})


def test_no_session_id_is_not_a_claude_subprocess():
    assert harness.session_id({}) is None
    assert harness.session_id({"CLAUDE_CODE_SESSION_ID": "  "}) is None
    with pytest.raises(harness.IdentityError, match="CLAUDE_CODE_SESSION_ID is not set"):
        harness.resolve_identity({"CLAUDE_PID": str(os.getppid())})


@pytest.mark.parametrize("value", ["abc", "-4", "0", "12 34"])
def test_a_claude_pid_that_is_not_a_process_id_is_refused(value):
    with pytest.raises(harness.IdentityError, match="not a process id"):
        harness.claude_process({"CLAUDE_PID": value})


def test_a_claude_pid_that_is_not_running_is_refused(sleeper):
    sleeper.kill()
    sleeper.wait()
    with pytest.raises(harness.IdentityError, match="not running"):
        harness.claude_process({"CLAUDE_PID": str(sleeper.pid)})


def test_a_live_ancestor_is_accepted_and_the_process_itself_is_not():
    parent = os.getppid()
    assert harness.claude_process({"CLAUDE_PID": str(parent)}).pid == parent
    with pytest.raises(harness.IdentityError, match="not an ancestor"):
        harness.claude_process({"CLAUDE_PID": str(os.getpid())})


def test_a_dashboard_run_is_recognised_by_its_marker():
    assert harness.in_dashboard_run({"TASK_KIT_AGENT_DEPTH": "1"})
    assert not harness.in_dashboard_run({})


# -- the transcript -------------------------------------------------------------------------------------


def make_transcript(home: Path, session: str, *projects: str, config: str = ".claude") -> list[Path]:
    made = []
    for project in projects:
        path = home / config / "projects" / project / f"{session}.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        made.append(path)
    return made


def test_the_transcript_is_the_single_match_recorded_home_relative(tmp_path):
    make_transcript(tmp_path, "abc-123", "-Users-me-proj")
    found = harness.find_transcript("abc-123", {}, tmp_path)
    assert found.path == "~/.claude/projects/-Users-me-proj/abc-123.jsonl" and found.note is None


def test_zero_or_several_transcripts_are_not_recorded_and_say_why(tmp_path):
    none = harness.find_transcript("abc-123", {}, tmp_path)
    assert none.path is None and "no transcript" in none.note
    make_transcript(tmp_path, "abc-123", "p1", "p2")
    several = harness.find_transcript("abc-123", {}, tmp_path)
    assert several.path is None and "2 transcripts" in several.note


def test_claude_config_dir_moves_the_search_and_outside_home_is_not_recorded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    elsewhere = tmp_path / "elsewhere"
    make_transcript(elsewhere, "abc-123", "p1", config="cfg")
    found = harness.find_transcript("abc-123", {"CLAUDE_CONFIG_DIR": str(elsewhere / "cfg")}, home)
    assert found.path is None and "not under the home directory" in found.note
    inside = home / "cfgdir"
    make_transcript(home, "abc-123", "p1", config="cfgdir")
    assert harness.find_transcript("abc-123", {"CLAUDE_CONFIG_DIR": str(inside)}, home).path \
        == "~/cfgdir/projects/p1/abc-123.jsonl"


@pytest.mark.parametrize("session", ["../x", "a/b", "*", "", "a b"])
def test_a_session_id_that_cannot_name_a_file_finds_nothing(tmp_path, session):
    assert harness.find_transcript(session, {}, tmp_path).path is None


# -- the machine ----------------------------------------------------------------------------------------


def test_host_id_is_the_lowercase_short_name_and_wsl_appends_once():
    assert machine.host_id(hostname="M5Pro.local", instance="macos") == "m5pro"
    assert machine.host_id(hostname="Desk", instance="linux-wsl") == "desk-wsl"
    assert machine.host_id(hostname="devwsl", instance="linux-wsl") == "devwsl"
    assert machine.host_id(hostname="Desk", instance="linux") == "desk"


def test_the_sessions_host_override_wins_and_must_be_a_file_name():
    assert machine.host_id(override="Studio-2", hostname="m5pro", instance="linux-wsl") == "studio-2"
    for bad in ("a/b", "-x", "has space", ".hidden", "x" * 80):
        with pytest.raises(machine.MachineError, match="sessions.host"):
            machine.host_id(override=bad, hostname="m5pro", instance="macos")
    with pytest.raises(machine.MachineError, match="the host name"):
        machine.host_id(hostname="bad name", instance="macos")


@pytest.mark.parametrize("platform,release,expected", [
    ("darwin", "25.0", "macos"), ("win32", "10", "windows"), ("linux", "6.5.0-generic", "linux"),
    ("linux", "5.15.90.1-microsoft-standard-WSL2", "linux-wsl"), ("linux", "4.4.0-19041-Microsoft", "linux-wsl"),
])
def test_os_instance_names_the_process_table(platform, release, expected):
    assert machine.os_instance(platform=platform, release=release) == expected


def test_an_unsupported_platform_is_refused():
    with pytest.raises(machine.MachineError, match="do not support"):
        machine.os_instance(platform="plan9")


def test_the_fingerprint_is_a_salted_sixteen_digit_hash_of_the_machine_id():
    import hashlib

    fingerprint = machine.fingerprint("ABC-123")
    assert fingerprint == hashlib.sha256(b"task-kit-machine-v1:ABC-123").hexdigest()[:16]
    assert len(fingerprint) == 16 and "ABC" not in fingerprint
    assert machine.fingerprint("ABC-124") != fingerprint


def test_an_unreadable_machine_id_is_an_error_not_a_guess(tmp_path):
    with pytest.raises(machine.MachineError, match="cannot read the machine id"):
        machine.machine_id(platform="linux", linux_paths=(tmp_path / "absent-1", tmp_path / "absent-2"))
    empty = tmp_path / "machine-id"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(machine.MachineError, match="empty"):
        machine.machine_id(platform="linux", linux_paths=(empty,))
    with pytest.raises(machine.MachineError, match="empty"):
        machine.machine_id(reader=lambda: "  ")
    with pytest.raises(machine.MachineError):
        machine.current(id_reader=lambda: "")
    good = tmp_path / "good-id"
    good.write_text("0123abcd\n", encoding="utf-8")
    assert machine.machine_id(platform="linux", linux_paths=(empty, good)) == "0123abcd"


def test_this_machine_identifies_itself_with_the_real_reader():
    here = machine.current()
    assert here.fingerprint == machine.fingerprint(machine.machine_id())
    assert here.host and here.os_instance in ("macos", "linux", "linux-wsl", "windows")
    assert here.fingerprint == machine.current().fingerprint  # a second read agrees


# -- the process stamp ----------------------------------------------------------------------------------


def test_repeated_reads_of_a_live_process_stamp_are_identical(sleeper):
    """Measures the stability the exact comparison relies on: every read on this platform is bit-equal."""
    first = procstamp.stamp_of(sleeper.pid)
    reads = []
    for _ in range(40):
        reads.append(procstamp.stamp_of(sleeper.pid))
        time.sleep(0.005)
    assert all(read == first for read in reads)


def test_a_live_process_reads_as_running_and_its_stamp_survives_a_text_round_trip(sleeper):
    stamp = procstamp.stamp_of(sleeper.pid)
    assert procstamp.is_running(stamp)
    restored = procstamp.Stamp.from_dict(yaml.safe_load(yaml.safe_dump(stamp.to_dict())))
    assert restored == stamp and procstamp.is_running(restored)


def test_a_stamp_half_a_second_off_names_a_different_process(sleeper):
    """The dashboard's own 1.0 s tolerance would call this the same process."""
    stamp = procstamp.stamp_of(sleeper.pid)
    if stamp.created is None:
        pytest.skip("the Linux stamp has no creation time; its exactness is tested with the fake procfs")
    assert not procstamp.is_running(procstamp.Stamp(stamp.pid, stamp.created + 0.5))
    assert not procstamp.is_running(procstamp.Stamp(stamp.pid, stamp.created - 0.5))
    assert procstamp.is_running(procstamp.Stamp(stamp.pid, stamp.created + 0.0005))


def test_a_killed_process_and_an_unreaped_zombie_are_not_running(sleeper):
    stamp = procstamp.stamp_of(sleeper.pid)
    sleeper.kill()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and psutil.Process(sleeper.pid).status() != psutil.STATUS_ZOMBIE:
        time.sleep(0.01)
    assert not procstamp.is_running(stamp)  # a zombie: killed but not yet waited for
    sleeper.wait()
    assert not procstamp.is_running(stamp)  # gone entirely
    assert procstamp.stamp_of(sleeper.pid) is None


@pytest.mark.parametrize("data", [
    {}, {"pid": True, "created": 1.0}, {"pid": 1}, {"pid": 1, "created": "x"}, {"pid": 1, "boot_id": "b"},
    {"pid": 1, "created": 1.0, "extra": 1}, {"pid": 1, "boot_id": 4, "start_ticks": 1},
])
def test_a_malformed_stamp_is_refused_loudly(data):
    with pytest.raises(procstamp.StampError):
        procstamp.Stamp.from_dict(data)


def fake_proc(tmp_path: Path, pid: int, *, ticks: int, state: str = "S", comm: str = "python", boot: str = "boot-A") -> Path:
    root = tmp_path / "proc"
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    after_comm = [state] + ["0"] * 18 + [str(ticks)] + ["0"] * 10  # fields 3.. ; field 22 is the 20th of these
    (root / str(pid) / "stat").write_text(f"{pid} ({comm}) " + " ".join(after_comm) + "\n", encoding="utf-8")
    (root / "sys" / "kernel" / "random").mkdir(parents=True, exist_ok=True)
    (root / "sys" / "kernel" / "random" / "boot_id").write_text(boot + "\n", encoding="utf-8")
    return root


def test_the_linux_stamp_is_start_ticks_plus_boot_id_compared_exactly(tmp_path):
    root = fake_proc(tmp_path, 4242, ticks=987654, comm="we ird) (name")  # a command name with spaces and parentheses
    stamp = procstamp.stamp_of(4242, platform="linux", proc_root=root)
    assert stamp == procstamp.Stamp(4242, None, "boot-A", 987654)
    assert stamp.to_dict() == {"pid": 4242, "boot_id": "boot-A", "start_ticks": 987654}
    assert procstamp.is_running(stamp, platform="linux", proc_root=root)
    assert not procstamp.is_running(procstamp.Stamp(4242, None, "boot-A", 987655), platform="linux", proc_root=root)
    assert not procstamp.is_running(procstamp.Stamp(4242, None, "boot-B", 987654), platform="linux", proc_root=root)
    fake_proc(tmp_path, 4242, ticks=987654, state="Z")
    assert not procstamp.is_running(stamp, platform="linux", proc_root=root)  # a zombie
    assert procstamp.stamp_of(4243, platform="linux", proc_root=root) is None


def test_an_unreadable_boot_id_is_an_error_not_a_dead_process(tmp_path):
    root = fake_proc(tmp_path, 7, ticks=5)
    (root / "sys" / "kernel" / "random" / "boot_id").unlink()
    with pytest.raises(procstamp.StampError, match="boot id"):
        procstamp.stamp_of(7, platform="linux", proc_root=root)


# -- the OS lock ----------------------------------------------------------------------------------------


def test_two_handles_exclude_each_other_until_the_first_releases(tmp_path):
    path = tmp_path / "x.lock"
    with oslock.FileLock(path, 1.0):
        started = time.monotonic()
        with pytest.raises(oslock.LockTimeout) as caught:
            with oslock.FileLock(path, 0.2):
                pass
        assert time.monotonic() - started >= 0.2 and caught.value.path == path
    with oslock.FileLock(path, 0.2):
        pass


def test_a_lock_held_by_another_process_excludes_this_one(tmp_path):
    path = tmp_path / "y.lock"
    code = ("import sys, time; from task_system import oslock;"
            "l = oslock.FileLock(sys.argv[1], 5); l.__enter__(); print('held', flush=True); time.sleep(60)")
    holder = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True, env=probe_env())
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(oslock.LockTimeout):
            with oslock.FileLock(path, 0.3):
                pass
    finally:
        holder.kill()
        holder.wait()
    with oslock.FileLock(path, 5.0):  # the OS dropped it with the process
        pass


def test_try_lock_reports_a_held_lock_without_waiting(tmp_path):
    path = tmp_path / "z.lock"
    with open(path, "a+b") as first, open(path, "a+b") as second:
        assert oslock.try_lock(first)
        assert not oslock.try_lock(second)
        oslock.unlock(first)
        assert oslock.try_lock(second)
        oslock.unlock(second)


# -- Windows gate: registration is refused until process verification is measured there ---------------------


@pytest.mark.parametrize("platform", ["win32", "cygwin", "msys"])
def test_identity_is_refused_on_windows_with_a_stated_reason(platform, sleeper):
    """Revert: drop the platform check in resolve_identity -> the call proceeds past the gate (red)."""
    env = {"CLAUDE_CODE_SESSION_ID": "sid-1", "CLAUDE_PID": str(os.getpid())}
    with pytest.raises(harness.IdentityError) as caught:
        harness.resolve_identity(env, platform=platform)
    assert str(caught.value) == harness.WINDOWS_REFUSAL
    assert "not supported on Windows" in str(caught.value) and "CLAUDE_PID" in str(caught.value)


def test_the_windows_refusal_precedes_every_other_identity_check():
    """A Windows caller with no session id still gets the platform reason, never a misleading env message."""
    with pytest.raises(harness.IdentityError) as caught:
        harness.resolve_identity({}, platform="win32")
    assert str(caught.value) == harness.WINDOWS_REFUSAL


def test_other_platforms_pass_the_gate_and_reach_the_ordinary_checks():
    with pytest.raises(harness.IdentityError) as caught:
        harness.resolve_identity({"CLAUDE_CODE_SESSION_ID": "sid-1"}, platform="darwin")
    assert "CLAUDE_PID is not set" in str(caught.value)


def test_work_reports_a_windows_refusal_on_the_session_line_and_writes_nothing(monkeypatch, tmp_path):
    """Revert: drop the gate -> registration is attempted on Windows (red on both assertions)."""
    from task_system import sessions_cli

    monkeypatch.setattr(sys, "platform", "win32")
    env = {"CLAUDE_CODE_SESSION_ID": "sid-1", "CLAUDE_PID": str(os.getpid())}
    lines = sessions_cli.register_for_work(tmp_path, "alpha", env=env, config_path=tmp_path / "none.yaml",
                                           data_dir=tmp_path / "data")
    assert lines == [f"session: not registered: {harness.WINDOWS_REFUSAL}"]
    assert not (tmp_path / "data").exists() and not (tmp_path / "dev").exists()
