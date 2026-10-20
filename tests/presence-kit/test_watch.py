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


def run(argv, clock, tmp_path, env=None):
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
    clock = FakeClock(stop_after=1)
    _, out, _ = run(["--path", str(root), "--progress-minutes", "10000"], clock, tmp_path)
    assert stalls(out) == ["STALL: no change in watched paths for 25m"]


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


def test_progress_interval_defaults_to_the_shipped_config_value(tmp_path):
    job = make_file(tmp_path / "job.log", 0)
    clock = FakeClock(on_sleep=lambda n, c: set_mtime(job, c.t), stop_after=14)  # t up to 65m
    _, out, _ = run(["--path", str(job)], clock, tmp_path)
    assert [p.split(":")[0] for p in progresses(out)] == ["PROGRESS 20m", "PROGRESS 40m", "PROGRESS 60m"]


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
    bad.write_text(json.dumps({"values": {"progress_minutes": 0}}), encoding="utf-8")
    clock = FakeClock(stop_after=3)
    code, out, err = run(["--path", str(tmp_path / "x")], clock, tmp_path)
    assert code == 2
    assert out.text == ""
    assert err.text == f"{bad}: value 'progress_minutes' must be a positive integer; got 0\n"
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
