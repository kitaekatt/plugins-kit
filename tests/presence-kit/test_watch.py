"""presence-kit watch.py: STALL, PROGRESS and DONE lines, cadence, config and bad arguments.

The clocks are fake: one counter drives both the monotonic and the wall clock, each
sleep advances it and may touch files, so every line follows from file state. File
mtimes are set explicitly against the fake wall clock.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit" / "scripts" / "watch.py"
SHIPPED = SCRIPT.parents[1] / "defaults" / "presence-kit.json"
_spec = importlib.util.spec_from_file_location("watch_under_test", SCRIPT)
watch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch)

BASE = 1_700_000_000.0  # the fake wall clock's origin
TRAILER = "[exited with code 0]"


class StopRun(Exception):
    """Raised from sleep to end a watcher that would otherwise run forever."""


class FakeClock:
    def __init__(self, on_sleep=None, stop_after=None):
        self.t = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep
        self.stop_after = stop_after

    def now(self):
        return self.t

    def wall(self):
        return BASE + self.t

    def sleep(self, seconds):
        self.sleeps += 1
        if self.stop_after is not None and self.sleeps >= self.stop_after:
            raise StopRun
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps, self)


class _Buf:
    def __init__(self):
        self.text = ""
        self.lines_flushed = 0

    def write(self, s):
        self.text += s

    def flush(self):
        self.lines_flushed = self.text.count("\n")


@pytest.fixture(autouse=True)
def _no_real_process_sampling(monkeypatch):
    """Tests that do not inject process samples see no processes at all."""
    monkeypatch.setattr(watch, "_sample_procs", lambda: {})
    monkeypatch.setattr(watch, "_sample_cwds", lambda pids: {})


def run(argv, clock, tmp_path, env=None, **kw):
    """Run to completion or until clock.stop_after sleeps; returns (code, out, err)."""
    out, err = _Buf(), _Buf()
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "CLAUDE_PROJECT_DIR": str(tmp_path / "proj")} if env is None else env
    try:
        code = watch.main(
            argv,
            now=clock.now,
            sleep=clock.sleep,
            wall=clock.wall,
            stdout=out,
            stderr=err,
            env=env,
            cwd=str(tmp_path),
            **kw,
        )
    except StopRun:
        code = None
    return code, out, err


def set_mtime(path, offset_seconds):
    stamp = BASE + offset_seconds
    os.utime(path, (stamp, stamp))


def make_file(path, offset_seconds, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    set_mtime(path, offset_seconds)
    return path


def lines(buf):
    return buf.text.splitlines()


def stalls(buf):
    return [line for line in lines(buf) if line.startswith("STALL")]


def progresses(buf):
    return [line for line in lines(buf) if line.startswith("PROGRESS")]


# --- stall -------------------------------------------------------------------------

def test_stall_is_reported_once_per_quiet_episode_and_the_watcher_keeps_running(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(stop_after=20)  # 20 sleeps of 300s = 100 quiet minutes
    code, out, _ = run(["--path", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert code is None  # still running when the test stopped it
    assert stalls(out) == ["STALL: no change in watched paths for 20m"]


def test_stall_rearms_after_a_newer_mtime(tmp_path):
    job = make_file(tmp_path / "job.log", 0)

    def touch(n, clock):
        if n == 6:  # 30 minutes in: the first stall (20m) is already out
            set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=touch, stop_after=20)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    # first episode at 20m; the touch at 30m starts a new one, reported at 50m
    assert len(stalls(out)) == 2


def test_activity_before_the_watcher_started_counts_toward_the_stall(tmp_path):
    job = make_file(tmp_path / "job.log", -25 * 60)  # quiet for 25 minutes already
    clock = FakeClock(stop_after=1)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert clock.sleeps == 1
    assert stalls(out) == ["STALL: no change in watched paths for 25m"]


def test_no_stall_while_the_file_keeps_changing(tmp_path):
    job = make_file(tmp_path / "job.log", 0)

    def touch(n, clock):
        set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=touch, stop_after=30)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert stalls(out) == []


def test_stall_minutes_flag_sets_the_threshold(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(stop_after=6)
    _, out, _ = run(
        ["--path", str(job), "--stall-minutes", "5", "--progress-minutes", "10000"], clock, tmp_path
    )
    assert stalls(out) == ["STALL: no change in watched paths for 5m"]


def test_nothing_existing_stalls_from_the_watchers_own_start(tmp_path):
    clock = FakeClock(stop_after=6)
    _, out, _ = run(
        ["--path", str(tmp_path / "never"), "--progress-minutes", "10000"], clock, tmp_path
    )
    assert stalls(out) == ["STALL: no change in watched paths for 20m"]


# --- what counts as a change -----------------------------------------------------------

def test_directory_uses_the_newest_file_and_a_missing_path_counts_for_nothing(tmp_path):
    root = tmp_path / "work"
    make_file(root / "old.txt", -3600)
    make_file(root / "sub" / "deep" / "new.txt", -60)
    clock = FakeClock(stop_after=1)
    _, out, _ = run(
        ["--path", str(root), "--path", str(tmp_path / "missing"), "--progress-minutes", "1"],
        clock,
        tmp_path,
    )
    # one poll at t=0: no progress line yet (due at 1m); nothing is stale (newest is 1m old)
    assert out.text == ""
    clock = FakeClock(stop_after=2)
    _, out, _ = run(
        ["--path", str(root), "--path", str(tmp_path / "missing"), "--progress-minutes", "1",
         "--poll-seconds", "60"],
        clock,
        tmp_path,
    )
    assert progresses(out) == [f"PROGRESS 1m: last change 2m ago in {root / 'sub' / 'deep' / 'new.txt'}"]


def test_git_directories_are_skipped(tmp_path):
    root = tmp_path / "repo"
    make_file(root / "src.txt", -25 * 60)
    make_file(root / ".git" / "index", 0)  # fresh, but inside .git
    clock = FakeClock(stop_after=2)  # a watched directory is confirmed at the second poll
    _, out, _ = run(["--path", str(root), "--progress-minutes", "10000"], clock, tmp_path)
    assert stalls(out) == ["STALL: no change in watched paths for 30m"]


def test_a_file_appearing_later_ends_the_stall(tmp_path):
    root = tmp_path / "work"
    root.mkdir()

    def create(n, clock):
        if n == 5:  # 25 minutes in: stalled from the start, then something appears
            make_file(root / "late.txt", clock.t)

    clock = FakeClock(on_sleep=create, stop_after=9)
    _, out, _ = run(["--path", str(root), "--progress-minutes", "10000"], clock, tmp_path)
    assert len(stalls(out)) == 1  # the stall at 20m; the new file re-armed it, 20m not yet passed again


# --- progress ------------------------------------------------------------------------------

def test_progress_is_reported_every_interval_whether_or_not_anything_changed(tmp_path):
    job = make_file(tmp_path / "job.log", 0)

    def touch(n, clock):
        if n % 2 == 1:  # changes only some of the time
            set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=touch, stop_after=27)  # 26 sleeps: t reaches 7800s = 130m
    _, out, _ = run(["--path", str(job), "--progress-minutes", "20"], clock, tmp_path)
    assert [p.split(":")[0] for p in progresses(out)] == [
        "PROGRESS 20m", "PROGRESS 40m", "PROGRESS 60m", "PROGRESS 80m", "PROGRESS 100m", "PROGRESS 120m",
    ]
    assert progresses(out)[0] == f"PROGRESS 20m: last change 5m ago in {job}"


def test_progress_says_when_no_watched_path_exists_yet(tmp_path):
    clock = FakeClock(stop_after=6)  # polls at 0, 5, 10, 15, 20, 25 minutes
    _, out, _ = run(
        ["--path", str(tmp_path / "never"), "--progress-minutes", "20", "--stall-minutes", "1000"],
        clock,
        tmp_path,
    )
    assert progresses(out) == ["PROGRESS 20m: no watched path exists yet"]


def test_progress_is_off_by_default_and_the_shipped_value_is_zero(tmp_path):
    assert json.loads(SHIPPED.read_text(encoding="utf-8"))["values"]["progress_minutes"] == 0
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(on_sleep=lambda n, c: set_mtime(job, c.t), stop_after=14)  # t up to 65m
    code, out, _ = run(["--path", str(job)], clock, tmp_path)
    assert code is None
    assert progresses(out) == []
    assert out.text == ""  # a busy job, no STALL, no PROGRESS: the Monitor says nothing


def test_stall_and_done_are_unchanged_with_progress_off(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(stop_after=20)  # 100 quiet minutes
    _, out, _ = run(["--path", str(job)], clock, tmp_path)
    assert stalls(out) == ["STALL: no change in watched paths for 20m"]
    assert progresses(out) == []
    done = make_file(tmp_path / "job.out", 0, "x\n" + TRAILER + "\n")
    code, out, _ = run(["--done-output", str(done)], FakeClock(), tmp_path)
    assert code == 0 and lines(out) == [f"DONE: {TRAILER}"]


def test_a_config_value_of_zero_is_off_and_an_explicit_flag_still_turns_progress_on(tmp_path):
    cfg = tmp_path / "cfg" / "config"
    cfg.mkdir(parents=True)
    (cfg / "presence-kit.json").write_text(json.dumps({"values": {"progress_minutes": 0}}), encoding="utf-8")
    job = make_file(tmp_path / "job.log", 0)
    keep_fresh = lambda n, clock: set_mtime(job, clock.t)  # noqa: E731
    _, out, _ = run(["--path", str(job)], FakeClock(on_sleep=keep_fresh, stop_after=14), tmp_path)
    assert progresses(out) == []
    _, out, _ = run(["--path", str(job), "--progress-minutes", "30"],
                    FakeClock(on_sleep=keep_fresh, stop_after=14), tmp_path)
    assert [p.split(":")[0] for p in progresses(out)] == ["PROGRESS 30m", "PROGRESS 60m"]


def test_the_flag_zero_means_off_even_over_a_positive_config_value(tmp_path):
    cfg = tmp_path / "cfg" / "config"
    cfg.mkdir(parents=True)
    (cfg / "presence-kit.json").write_text(json.dumps({"values": {"progress_minutes": 10}}), encoding="utf-8")
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(on_sleep=lambda n, c: set_mtime(job, c.t), stop_after=14)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "0"], clock, tmp_path)
    assert progresses(out) == []


def test_progress_interval_follows_the_user_config_and_the_flag_overrides_it(tmp_path):
    cfg = tmp_path / "cfg" / "config"
    cfg.mkdir(parents=True)
    (cfg / "presence-kit.json").write_text(json.dumps({"values": {"progress_minutes": 10}}), encoding="utf-8")
    job = make_file(tmp_path / "job.log", 0)

    def keep_fresh(n, clock):
        set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=keep_fresh, stop_after=8)  # t up to 35m
    _, out, _ = run(["--path", str(job)], clock, tmp_path)
    assert [p.split(":")[0] for p in progresses(out)] == ["PROGRESS 10m", "PROGRESS 20m", "PROGRESS 30m"]

    clock = FakeClock(on_sleep=keep_fresh, stop_after=8)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "30"], clock, tmp_path)
    assert [p.split(":")[0] for p in progresses(out)] == ["PROGRESS 30m"]


def test_invalid_config_fails_loudly_with_exit_2(tmp_path):
    cfg = tmp_path / "cfg" / "config"
    cfg.mkdir(parents=True)
    bad = cfg / "presence-kit.json"
    bad.write_text(json.dumps({"values": {"progress_minutes": -1}}), encoding="utf-8")
    clock = FakeClock(stop_after=3)
    code, out, err = run(["--path", str(tmp_path / "x")], clock, tmp_path)
    assert code == 2
    assert out.text == ""
    assert err.text == (
        f"{bad}: value 'progress_minutes' must be a non-negative integer (0 turns PROGRESS lines off); got -1\n"
    )
    assert clock.sleeps == 0


# --- done ------------------------------------------------------------------------------------

def test_done_output_trailer_ends_the_watcher_with_a_done_line(tmp_path):
    job = make_file(tmp_path / "job.out", 0, "running\n")

    def finish(n, clock):
        if n == 2:
            job.write_text("running\nfinished\n" + TRAILER + "\n", encoding="utf-8")
            set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=finish)
    code, out, err = run(["--done-output", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert code == 0
    assert out.text == f"DONE: {TRAILER}\n"
    assert err.text == ""
    assert clock.sleeps == 2


def test_done_is_checked_before_the_first_sleep(tmp_path):
    job = make_file(tmp_path / "job.out", 0, "work\n" + TRAILER + "\n\n")
    clock = FakeClock()
    code, out, _ = run(["--done-output", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert (code, clock.sleeps, out.text) == (0, 0, f"DONE: {TRAILER}\n")


def test_trailer_on_an_earlier_line_is_not_done(tmp_path):
    job = make_file(tmp_path / "job.out", 0, TRAILER + "\nmore output\n")
    clock = FakeClock(stop_after=3)
    code, out, _ = run(["--done-output", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert code is None
    assert "DONE" not in out.text


def test_done_file_pattern_match_prints_the_matching_line(tmp_path):
    log = make_file(tmp_path / "app.log", 0, "boot\n")

    def write_match(n, clock):
        if n == 1:
            log.write_text("boot\nready on port 8080\nERR-42 failure here\n", encoding="utf-8")

    clock = FakeClock(on_sleep=write_match)
    code, out, _ = run(
        ["--done-file", str(log), "--done-pattern", r"ERR-\d+", "--progress-minutes", "10000"],
        clock,
        tmp_path,
    )
    assert code == 0
    assert out.text == "DONE: ERR-42 failure here\n"


def test_the_done_file_is_watched_without_a_path_option(tmp_path):
    job = make_file(tmp_path / "job.out", 0, "running\n")

    def append(n, clock):
        job.write_text(job.read_text(encoding="utf-8") + "tick\n", encoding="utf-8")
        set_mtime(job, clock.t)

    clock = FakeClock(on_sleep=append, stop_after=20)
    code, out, _ = run(["--done-output", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert code is None
    assert stalls(out) == []


def test_done_wins_over_a_stall_in_the_same_poll(tmp_path):
    job = make_file(tmp_path / "job.out", -3600, TRAILER + "\n")
    code, out, _ = run(["--done-output", str(job), "--progress-minutes", "10000"], FakeClock(), tmp_path)
    assert (code, out.text) == (0, f"DONE: {TRAILER}\n")


# --- output discipline ---------------------------------------------------------------------------

def test_every_line_is_flushed_when_printed(tmp_path):
    job = make_file(tmp_path / "job.log", -25 * 60)
    clock = FakeClock(stop_after=2)
    _, out, _ = run(["--path", str(job), "--progress-minutes", "10000"], clock, tmp_path)
    assert out.lines_flushed == len(lines(out)) == 1


def test_the_watcher_writes_no_file(tmp_path):
    root = tmp_path / "watched"
    make_file(root / "a.txt", -25 * 60)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    run(["--path", str(root)], FakeClock(stop_after=6), tmp_path)
    assert sorted((p.name, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before


# --- bad arguments ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--path"],
        ["--path", "x", "--done-output", "a", "--done-file", "b", "--done-pattern", "p"],
        ["--path", "x", "--done-file", "b"],
        ["--path", "x", "--done-pattern", "p"],
        ["--path", "x", "--done-output", "a", "--done-pattern", "p"],
        ["--path", "x", "--done-file", "b", "--done-pattern", "("],
        ["--path", "x", "--stall-minutes", "0"],
        ["--path", "x", "--stall-minutes", "soon"],
        ["--path", "x", "--progress-minutes", "-1"],
        ["--path", "x", "--poll-seconds", "nan"],
        ["--path", "x", "--bogus"],
    ],
)
def test_bad_arguments_print_to_stderr_and_exit_2(tmp_path, argv):
    clock = FakeClock(stop_after=1)
    code, out, err = run(argv, clock, tmp_path)
    assert code == 2
    assert out.text == ""
    assert err.text.startswith("bad arguments: ")
    assert clock.sleeps == 0


# --- the real process ---------------------------------------------------------------------------------

def test_real_process_prints_done_and_exits_0(tmp_path):
    job = make_file(tmp_path / "job.out", 0, "work\n" + TRAILER + "\n")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(tmp_path / "cfg"), CLAUDE_PROJECT_DIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--done-output", str(job)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"DONE: {TRAILER}\n"
    assert result.stderr == ""


@pytest.mark.parametrize("name", [".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"])
def test_cache_directories_are_not_activity(tmp_path, name):
    root = tmp_path / "repo"
    make_file(root / "src.txt", -25 * 60)
    make_file(root / name / "v" / "cache" / "nodeids", 0)  # fresh, but a tool cache
    clock = FakeClock(stop_after=2)
    _, out, _ = run(["--path", str(root), "--progress-minutes", "10000"], clock, tmp_path)
    assert stalls(out) == ["STALL: no change in watched paths for 30m"]


# --- --task ------------------------------------------------------------------------
#
# Claude Code keeps a background task's output at
#   <tmp>/claude-<uid>/<project slug>/<session id>/tasks/<task id>.output
# A shell task's file is plain text ending in "[exited with code N]". An agent task's
# file is a symlink to the agent's transcript, whose records carry the agent's `cwd`.

SESSION = "sess-1234"


def task_env(tmp_path, **extra):
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "CLAUDE_PROJECT_DIR": str(tmp_path / "proj"),
            "CLAUDE_CODE_SESSION_ID": SESSION, **extra}


def task_dir(root):
    # POSIX: <tmp>/claude-<uid>/<slug>/...; Windows: <tmp>/claude/<hash>/...
    base = f"claude-{os.getuid()}" if hasattr(os, "getuid") else "claude"
    return root / base / "-proj-slug" / SESSION / "tasks"


def link_agent_output(tmp_path, tdir, name, transcript):
    """Create an agent's task output. POSIX: a symlink to the transcript. Windows: an empty
    regular file, with the transcript under <config dir>/projects/<slug>/<session>/subagents/."""
    if hasattr(os, "getuid"):
        (tdir / name).symlink_to(transcript)
    else:
        (tdir / name).write_text("", encoding="utf-8")
        sub = tmp_path / "cfg" / "projects" / "-proj-slug" / SESSION / "subagents"
        sub.mkdir(parents=True)
        os.replace(transcript, sub / f"agent-{name[:-len('.output')]}.jsonl")


def test_task_watches_the_shell_task_output_and_ends_on_its_trailer(tmp_path):
    root = tmp_path / "tmp"
    out_file = make_file(task_dir(root) / "b123.output", 0, f"working\n{TRAILER}\n")
    code, out, _ = run(["--task", "b123"], FakeClock(stop_after=3), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 0
    assert lines(out) == [f"DONE: {TRAILER}"]
    assert out_file.exists()


def test_task_progress_names_the_derived_output_path(tmp_path):
    root = tmp_path / "tmp"
    out_file = make_file(task_dir(root) / "b123.output", 0, "working\n")
    _, out, _ = run(["--task", "b123", "--progress-minutes", "5"], FakeClock(stop_after=2), tmp_path,
                    task_env(tmp_path), task_root=root)
    assert progresses(out) == [f"PROGRESS 5m: last change 5m ago in {out_file}"]


def test_task_for_an_agent_also_watches_the_agent_working_directory(tmp_path):
    root = tmp_path / "tmp"
    worktree = tmp_path / "wt"
    make_file(worktree / "src" / "a.py", 0)
    transcript = make_file(tmp_path / "agent-a1.jsonl", 0, json.dumps({"cwd": str(worktree), "type": "user"}) + "\n")
    tdir = task_dir(root)
    tdir.mkdir(parents=True)
    link_agent_output(tmp_path, tdir, "a1.output", transcript)

    def touch(n, clock):
        if n == 3:
            set_mtime(worktree / "src" / "a.py", clock.t)

    code, out, _ = run(["--task", "a1", "--progress-minutes", "20"], FakeClock(on_sleep=touch, stop_after=5),
                       tmp_path, task_env(tmp_path), task_root=root)
    assert code is None  # no hand-back in the transcript: it runs until killed
    assert stalls(out) == []  # the worktree edit at 15m kept the quiet period short


def test_task_without_a_session_id_fails_loudly(tmp_path):
    env = task_env(tmp_path)
    del env["CLAUDE_CODE_SESSION_ID"]
    code, _, err = run(["--task", "b123"], FakeClock(stop_after=1), tmp_path, env, task_root=tmp_path)
    assert code == 2 and "CLAUDE_CODE_SESSION_ID" in err.text


def test_task_with_no_output_file_fails_loudly(tmp_path):
    code, _, err = run(["--task", "nope"], FakeClock(stop_after=1), tmp_path, task_env(tmp_path),
                       task_root=tmp_path / "tmp")
    assert code == 2 and "nope" in err.text and "tasks" in err.text


def test_task_ids_are_matched_whole(tmp_path):
    root = tmp_path / "tmp"
    make_file(task_dir(root) / "b1234.output", 0, TRAILER + "\n")
    code, _, err = run(["--task", "b123"], FakeClock(stop_after=1), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 2


def test_task_and_an_explicit_done_option_both_hold(tmp_path):
    root = tmp_path / "tmp"
    make_file(task_dir(root) / "b123.output", 0, "working\n")
    log = make_file(tmp_path / "x.log", 0, "PUBLISH_EXIT\n")
    code, out, _ = run(["--task", "b123", "--done-file", str(log), "--done-pattern", "PUBLISH_EXIT"],
                       FakeClock(stop_after=3), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 0 and lines(out) == ["DONE: PUBLISH_EXIT"]


def test_task_an_id_that_could_climb_out_of_the_tasks_directory_is_refused(tmp_path):
    code, _, err = run(["--task", "../x"], FakeClock(stop_after=1), tmp_path, task_env(tmp_path), task_root=tmp_path)
    assert code == 2 and "task id" in err.text


# --- process activity ---------------------------------------------------------------
#
# A live process whose working directory is inside a watched directory (or that descends
# from a --pid) and used CPU since the previous poll is a sign of work: a long command
# changes no file. A process seen for the first time is a baseline, not activity.

Proc = watch.Proc
ME = os.getpid()


class World:
    """Injected process table; `cpu` maps pid -> cumulative CPU seconds, advanced by tests."""

    def __init__(self, rows, cwds):
        self.rows = rows  # pid -> (ppid, command)
        self.cpu = {pid: 0.0 for pid in rows}
        self.cwd = cwds
        self.cwd_calls = []

    def procs(self):
        return {pid: Proc(ppid, self.cpu[pid], cmd) for pid, (ppid, cmd) in self.rows.items()}

    def cwds(self, pids):
        self.cwd_calls.append(sorted(pids))
        return {pid: self.cwd[pid] for pid in pids if pid in self.cwd}


def burn(world, pid, per_poll=60.0, until=None):
    """on_sleep hook: the process uses `per_poll` CPU seconds each poll (until poll `until`)."""
    def hook(n, clock):
        if until is None or n <= until:
            world.cpu[pid] += per_poll
    return hook


def run_world(argv, clock, tmp_path, world, **kw):
    return run(argv + ["--progress-minutes", "10000"], clock, tmp_path, procs=world.procs, cwds=world.cwds, **kw)


def test_a_cpu_using_process_in_a_watched_directory_prevents_a_stall(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)  # the only file change is at the start
    world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=30)  # 150 minutes
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert stalls(out) == []


def test_the_same_process_without_cpu_growth_stalls(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
    clock = FakeClock(stop_after=30)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_stall_follows_once_the_process_stops_using_cpu(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)

    def episode(stop_after):
        world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
        clock = FakeClock(on_sleep=burn(world, 900, until=12), stop_after=stop_after)  # busy to 60m
        return run_world(["--path", str(watched)], clock, tmp_path, world)[1]

    assert stalls(episode(16)) == []  # 15 sleeps = 75m: 15m since the last CPU use
    # 17 sleeps = 80m: 20m since it, counted from the last poll that saw CPU, not from the file
    assert stalls(episode(18)) == ["STALL: no change in watched paths for 20m"]


def test_a_process_seen_for_the_first_time_is_a_baseline_not_activity(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
    world.cpu[900] = 5000.0  # a big cumulative figure the watcher never saw grow
    clock = FakeClock(stop_after=6)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_a_process_that_appears_between_polls_is_a_baseline_too(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest")}, {900: str(watched)})

    def appear(n, clock):
        if n == 3:
            world.rows[900] = (ME, "git push")
            world.cpu[900] = 5000.0  # first sighting: no earlier figure to subtract

    clock = FakeClock(on_sleep=appear, stop_after=6)  # 25 minutes, one stall at 20m
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_a_cpu_using_process_outside_the_watched_directories_does_not_count(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    elsewhere = tmp_path / "other"
    elsewhere.mkdir()
    world = World({ME: (1, "pytest"), 900: (1, "cc")}, {900: str(elsewhere)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=30)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_a_process_in_a_subdirectory_of_a_watched_directory_counts(tmp_path):
    watched = tmp_path / "work"
    (watched / "sub").mkdir(parents=True)
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (1, "make")}, {900: str(watched / "sub")})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=30)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert stalls(out) == []


def test_a_directory_whose_name_only_starts_like_a_watched_one_does_not_count(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    sibling = tmp_path / "work2"
    sibling.mkdir()
    world = World({ME: (1, "pytest"), 900: (1, "make")}, {900: str(sibling)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=30)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_a_descendant_of_a_given_pid_counts_wherever_its_working_directory_is(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    world = World({ME: (1, "pytest"), 700: (1, "hook"), 701: (700, "gc"), 800: (1, "other")},
                  {701: "/nowhere", 800: "/nowhere"})
    clock = FakeClock(on_sleep=burn(world, 701), stop_after=30)
    _, out, _ = run_world(["--path", str(job), "--pid", "700"], clock, tmp_path, world)
    assert stalls(out) == []


def test_a_non_descendant_of_the_given_pid_does_not_count(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    world = World({ME: (1, "pytest"), 700: (1, "hook"), 800: (1, "other")}, {800: "/nowhere"})
    clock = FakeClock(on_sleep=burn(world, 800), stop_after=30)
    _, out, _ = run_world(["--path", str(job), "--pid", "700"], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_the_given_pid_itself_counts(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    world = World({ME: (1, "pytest"), 700: (1, "hook")}, {})
    clock = FakeClock(on_sleep=burn(world, 700), stop_after=30)
    _, out, _ = run_world(["--path", str(job), "--pid", "700"], clock, tmp_path, world)
    assert stalls(out) == []


def test_the_watcher_and_its_ancestors_are_never_counted(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    # 500 is the Monitor shell above the watcher, 400 its parent: both sit in the watched directory
    world = World({ME: (500, "watch.py"), 500: (400, "sh"), 400: (1, "claude")},
                  {ME: str(watched), 500: str(watched), 400: str(watched)})
    def hook(n, clock):
        for pid in (ME, 500, 400):
            world.cpu[pid] += 60.0
    clock = FakeClock(on_sleep=hook, stop_after=30)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_a_trickle_of_cpu_is_not_activity(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (1, "idle")}, {900: str(watched)})
    clock = FakeClock(on_sleep=burn(world, 900, per_poll=0.5), stop_after=30)  # 0.2% of a core
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert len(stalls(out)) == 1


def test_working_directories_are_looked_up_only_for_processes_that_used_cpu(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (1, "busy"), 901: (1, "idle")}, {900: str(watched), 901: str(watched)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=4)
    run_world(["--path", str(watched)], clock, tmp_path, world)
    assert world.cwd_calls and all(call == [900] for call in world.cwd_calls)


def test_progress_names_the_process_that_was_last_seen_working(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (ME, "git push origin dev")}, {900: str(watched)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=5)
    _, out, _ = run(["--path", str(watched), "--progress-minutes", "10"], clock, tmp_path,
                    procs=world.procs, cwds=world.cwds)
    assert progresses(out)
    assert all("process 900 (git push origin dev) using CPU" in line for line in progresses(out))


def test_progress_still_names_a_file_when_a_file_changed_more_recently(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    job = make_file(watched / "a.txt", 0)
    world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})

    def hook(n, clock):
        if n <= 3:
            world.cpu[900] += 60.0  # last CPU use is seen at 15m
        if n == 4:
            set_mtime(job, clock.t)  # the file changes at 20m, after it

    clock = FakeClock(on_sleep=hook, stop_after=6)
    _, out, _ = run(["--path", str(watched), "--progress-minutes", "10"], clock, tmp_path,
                    procs=world.procs, cwds=world.cwds)
    assert f"in {job}" in progresses(out)[-1]


def test_a_sampler_failure_exits_1_loudly_instead_of_counting_as_no_activity(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()

    def broken():
        raise watch.ProcessSampleError("ps exited 3: boom")

    clock = FakeClock(stop_after=5)
    code, out, err = run(["--path", str(watched)], clock, tmp_path, procs=broken, cwds=lambda pids: {})
    assert code == 1
    assert "ps exited 3: boom" in err.text


def test_no_sampling_when_nothing_could_match(tmp_path):
    job = make_file(tmp_path / "job.log", 0)  # a file, no directory, no --pid
    calls = []
    clock = FakeClock(stop_after=3)
    run(["--path", str(job)], clock, tmp_path, procs=lambda: calls.append(1) or {}, cwds=lambda p: {})
    assert calls == []


@pytest.mark.parametrize("text,seconds", [
    ("0:00.05", 0.05),
    ("1:02.50", 62.5),
    ("1:02:03", 3723.0),
    ("00:00:07", 7.0),
    ("2-01:00:00", 2 * 86400 + 3600.0),
])
def test_cpu_time_formats_of_macos_and_linux_ps(text, seconds):
    assert watch._parse_cpu(text) == pytest.approx(seconds)


def test_an_unparseable_cpu_time_fails_loudly():
    with pytest.raises(watch.ProcessSampleError):
        watch._parse_cpu("garbage")


def test_the_real_samplers_see_this_process_and_its_working_directory(monkeypatch):
    monkeypatch.undo()  # the autouse stub replaced the real samplers
    table = watch._sample_procs()
    assert ME in table and table[ME].ppid == os.getppid()
    assert table[ME].cpu >= 0.0
    cwd = watch._sample_cwds([ME])
    assert os.path.realpath(cwd[ME]) == os.path.realpath(os.getcwd())


def test_the_first_poll_does_not_report_a_stall_it_cannot_yet_check(tmp_path):
    watched = tmp_path / "work"
    watched.mkdir()
    make_file(watched / "a.txt", -25 * 60)  # already quiet for 25 minutes
    world = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
    clock = FakeClock(on_sleep=burn(world, 900), stop_after=3)
    _, out, _ = run_world(["--path", str(watched)], clock, tmp_path, world)
    assert stalls(out) == []  # the process was working all along
    world2 = World({ME: (1, "pytest"), 900: (ME, "git push")}, {900: str(watched)})
    _, out2, _ = run_world(["--path", str(watched)], FakeClock(stop_after=3), tmp_path, world2)
    assert stalls(out2) == ["STALL: no change in watched paths for 30m"]  # confirmed at the second poll


# --- --task on an agent: the hand-back ends the watch ---------------------------------
#
# A finished agent's transcript ends with an assistant record whose tool_use is
# SubagentHandback, answered by a user record with toolEndsTurn true whose
# sourceToolAssistantUUID names that assistant record.


def _rec(kind, uuid, **extra):
    return {"type": kind, "uuid": uuid, **extra}


def _handback_records():
    return [
        _rec("assistant", "u1", message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "SubagentHandback", "id": "t1", "input": {"message": "report"}}]}),
        _rec("user", "u2", toolEndsTurn=True, sourceToolAssistantUUID="u1",
             message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]}),
    ]


def _agent_task(tmp_path, records):
    root = tmp_path / "tmp"
    worktree = tmp_path / "wt"
    make_file(worktree / "a.py", 0)
    head = {"cwd": str(worktree), "type": "user", "uuid": "u0"}
    transcript = make_file(tmp_path / "agent-a1.jsonl", 0, "".join(json.dumps(r) + "\n" for r in [head, *records]))
    tdir = task_dir(root)
    tdir.mkdir(parents=True)
    link_agent_output(tmp_path, tdir, "a1.output", transcript)
    return root


def _run_agent(tmp_path, records):
    root = _agent_task(tmp_path, records)
    return run(["--task", "a1"], FakeClock(stop_after=3), tmp_path, task_env(tmp_path), task_root=root)


def test_task_for_an_agent_with_an_empty_output_file_finds_the_transcript_by_pattern(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "getuid", raising=False)
    # Windows layout: the output file is an empty regular file, not a link.
    root = tmp_path / "tmp"
    worktree = tmp_path / "wt"
    make_file(worktree / "a.py", 0)
    head = {"cwd": str(worktree), "type": "user", "uuid": "u0"}
    tdir = root / "claude" / "0123456789abcdef" / SESSION / "tasks"
    tdir.mkdir(parents=True)
    (tdir / "a1.output").write_text("", encoding="utf-8")
    sub = tmp_path / "cfg" / "projects" / "D--some-proj" / SESSION / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a1.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in [head, *_handback_records()]), encoding="utf-8")
    code, out, _ = run(["--task", "a1"], FakeClock(stop_after=3), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 0
    assert lines(out) == ["DONE: agent handed back its report"]


def test_task_for_an_agent_whose_transcript_is_missing_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "getuid", raising=False)
    root = tmp_path / "tmp"
    tdir = root / "claude" / "0123456789abcdef" / SESSION / "tasks"
    tdir.mkdir(parents=True)
    (tdir / "a1.output").write_text("", encoding="utf-8")
    code, _, err = run(["--task", "a1"], FakeClock(stop_after=1), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 2 and "transcript" in err.text


def test_task_without_a_uid_uses_the_windows_layout(tmp_path, monkeypatch):
    # <tmp>/claude/<hash>/<session>/tasks/<id>.output, with no -<uid> suffix.
    monkeypatch.delattr(os, "getuid", raising=False)
    root = tmp_path / "tmp"
    tdir = root / "claude" / "74411014221794bd" / SESSION / "tasks"
    tdir.mkdir(parents=True)
    (tdir / "b123.output").write_text("hi\n[exited with code 0]\n", encoding="utf-8")
    code, out, _ = run(["--task", "b123"], FakeClock(stop_after=3), tmp_path, task_env(tmp_path), task_root=root)
    assert code == 0
    assert lines(out) == ["DONE: [exited with code 0]"]


def test_task_for_an_agent_ends_with_done_when_the_agent_hands_back(tmp_path):
    code, out, _ = _run_agent(tmp_path, _handback_records())
    assert code == 0
    assert lines(out) == ["DONE: agent handed back its report"]


def test_task_for_an_agent_mid_run_does_not_end(tmp_path):
    records = _handback_records()[:1] + [
        _rec("user", "u2", sourceToolAssistantUUID="u1",
             message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]})]
    code, out, _ = _run_agent(tmp_path, records)
    assert code is None and "DONE" not in out.text


def test_a_turn_ending_tool_other_than_the_hand_back_is_not_done(tmp_path):
    records = _handback_records()
    records[0]["message"]["content"][0]["name"] = "ExitPlanMode"
    code, out, _ = _run_agent(tmp_path, records)
    assert code is None and "DONE" not in out.text


def test_an_agent_resumed_after_its_hand_back_is_not_done(tmp_path):
    records = _handback_records() + [
        _rec("assistant", "u3", message={"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "id": "t2", "input": {}}]})]
    code, out, _ = _run_agent(tmp_path, records)
    assert code is None and "DONE" not in out.text


# --- Windows process sampling (native Python: Git Bash's ps has no -A) ---------------

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows process sampling")


@windows_only
def test_windows_sampling_never_runs_ps(monkeypatch):
    monkeypatch.undo()  # the autouse stub replaced the real samplers

    def refuse(*args, **kwargs):
        raise AssertionError(f"a Windows sampler ran an external tool: {args}")

    monkeypatch.setattr(watch.procwatch.subprocess, "run", refuse)
    table = watch._sample_procs()
    assert table[ME].ppid == os.getppid()
    assert watch._sample_cwds([ME])[ME]


@windows_only
def test_windows_cpu_time_of_a_busy_child_grows_between_samples(monkeypatch, tmp_path):
    monkeypatch.undo()
    # A venv python is a launcher that runs the real interpreter as its child, so the
    # CPU is counted over the child and everything below it.
    child = subprocess.Popen([sys.executable, "-c", "while True: pass"], cwd=tmp_path)

    def tree_cpu():
        table = watch._sample_procs()
        assert table[child.pid].ppid == ME
        return sum(table[pid].cpu for pid in watch._descendants(table, [child.pid]))

    try:
        first = tree_cpu()
        import time as real_time
        real_time.sleep(0.5)
        assert tree_cpu() - first > 0.1
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True, check=False)
        child.wait()


@windows_only
def test_windows_working_directory_of_a_child_process_is_read_back(monkeypatch, tmp_path):
    monkeypatch.undo()
    work = tmp_path / "Mixed Case dir"
    work.mkdir()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], cwd=work)
    try:
        import time as real_time
        deadline = real_time.monotonic() + 30
        cwd = None
        while cwd is None:  # the image's parameters are readable once it has started
            assert real_time.monotonic() < deadline, "the child's working directory never became readable"
            cwd = watch._sample_cwds([child.pid]).get(child.pid)
            real_time.sleep(0.05)
        assert os.path.realpath(cwd) == os.path.realpath(work)
        # a directory watch recognises it, whatever case the process reports
        roots = [os.path.realpath(tmp_path).upper()]
        assert watch._within(os.path.realpath(cwd), roots)
    finally:
        child.kill()
        child.wait()


@windows_only
def test_windows_unreadable_working_directories_are_absent_not_errors(monkeypatch):
    monkeypatch.undo()
    # pid 4 is the System process: it cannot be opened, so it is simply absent
    assert 4 not in watch._sample_cwds([4])


def test_a_file_windows_cannot_access_through_a_broken_reparse_point_counts_for_nothing(tmp_path, monkeypatch):
    # Windows reports WinError 1920 (not FileNotFoundError) for an unresolvable link, such as
    # a stale entry in a package cache under a watched directory.
    good = make_file(tmp_path / "w" / "good.txt", 0)
    bad = tmp_path / "w" / "bad-link"
    bad.write_text("x", encoding="utf-8")
    real_stat = os.stat

    def stat(path, *a, **k):
        if str(path) == str(bad):
            exc = OSError(1920, "The file cannot be accessed by the system")
            exc.winerror = 1920
            raise exc
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", stat)
    found = watch._newest([str(tmp_path / "w")])
    assert found is not None and found[1] == str(good)


def test_transcript_growth_is_activity_when_the_output_file_is_not_a_link(tmp_path, monkeypatch):
    # Windows: the empty output file never changes, so the transcript itself must be watched.
    monkeypatch.delattr(os, "getuid", raising=False)
    root = tmp_path / "tmp"
    worktree = tmp_path / "wt"
    make_file(worktree / "a.py", 0)
    head = {"cwd": str(worktree), "type": "user", "uuid": "u0"}
    tdir = root / "claude" / "0123456789abcdef" / SESSION / "tasks"
    tdir.mkdir(parents=True)
    (tdir / "a1.output").write_text("", encoding="utf-8")
    set_mtime(tdir / "a1.output", 0)
    sub = tmp_path / "cfg" / "projects" / "D--some-proj" / SESSION / "subagents"
    transcript = make_file(sub / "agent-a1.jsonl", 0, json.dumps(head) + "\n")

    def grow(n, clock):
        set_mtime(transcript, clock.t)

    code, out, _ = run(["--task", "a1", "--poll-seconds", "600", "--stall-minutes", "20"],
                       FakeClock(on_sleep=grow, stop_after=6), tmp_path, task_env(tmp_path), task_root=root)
    assert code is None
    assert stalls(out) == []
