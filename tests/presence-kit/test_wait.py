"""presence-kit wait.py: done, stalled, slice, and bad-argument exits.

The clock is fake: each poll's sleep advances it and may change the file, so every
outcome follows from file state, not from wall-clock timing.
"""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit" / "scripts" / "wait.py"
_spec = importlib.util.spec_from_file_location("wait_under_test", SCRIPT)
wait = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait)

TRAILER = "[exited with code 0]"


class FakeClock:
    def __init__(self, on_sleep=None):
        self.t = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


class _Buf:
    def __init__(self):
        self.text = ""

    def write(self, s):
        self.text += s

    def flush(self):
        pass


def run(argv, clock=None):
    clock = clock or FakeClock()
    out, err = _Buf(), _Buf()
    code = wait.main(argv, now=clock.now, sleep=clock.sleep, stdout=out, stderr=err)
    return code, out.text, err.text, clock


def append(path, text):
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def test_last_nonempty_line_ignores_trailing_blanks_and_spaces():
    assert wait.last_nonempty_line("a\n\n  [exited with code 1]  \n\n") == "[exited with code 1]"
    assert wait.last_nonempty_line("\n  \n") is None


def test_output_trailer_as_last_line_is_done(tmp_path):
    job = tmp_path / "job.out"
    job.write_text("running\n", encoding="utf-8")

    def finish(n):
        if n == 2:
            append(job, "finished\n" + TRAILER + "\n")

    code, out, err, _ = run(
        ["--output", str(job), "--stall-minutes", "10", "--poll-seconds", "1"],
        FakeClock(finish),
    )
    assert code == 0
    assert out == TRAILER + "\n"
    assert err == ""


def test_trailer_on_earlier_line_is_not_done_slice_ends(tmp_path):
    job = tmp_path / "codex.out"
    job.write_text("design doc\n" + TRAILER + "\nmore output\n", encoding="utf-8")
    code, out, _, _ = run(
        ["--output", str(job), "--stall-minutes", "10", "--slice-seconds", "3", "--poll-seconds", "1"]
    )
    assert code == 4
    assert out == f"slice over: {job} still live\n"


def test_trailer_on_earlier_line_is_not_done_stall_ends(tmp_path):
    job = tmp_path / "codex.out"
    job.write_text(TRAILER + "\nmore output\n", encoding="utf-8")
    code, out, _, _ = run(
        ["--output", str(job), "--stall-minutes", "0.05", "--slice-seconds", "600", "--poll-seconds", "1"]
    )
    assert code == 3
    assert out == f"stalled: no change in {job} for 0.05 minutes\n"


def test_trailer_already_last_at_start_exits_without_sleeping(tmp_path):
    job = tmp_path / "job.out"
    job.write_text("work\n" + TRAILER + "\n\n", encoding="utf-8")
    code, out, _, clock = run(["--output", str(job), "--stall-minutes", "1"])
    assert code == 0
    assert out == TRAILER + "\n"
    assert clock.sleeps == 0


def test_file_pattern_match_is_done(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("boot\n", encoding="utf-8")

    def write_match(n):
        if n == 1:
            append(log, "ready on port 8080\nERR-42 failure here\n")

    code, out, err, _ = run(
        ["--file", str(log), "--pattern", r"ERR-\d+", "--stall-minutes", "10", "--poll-seconds", "1"],
        FakeClock(write_match),
    )
    assert code == 0
    assert out == "ERR-42 failure here\n"
    assert err == ""


def test_file_mode_ignores_trailer_convention(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(TRAILER + "\n", encoding="utf-8")
    code, out, _, _ = run(
        ["--file", str(log), "--pattern", r"^DONE$", "--stall-minutes", "0.05", "--poll-seconds", "1"]
    )
    assert code == 3
    assert out == f"stalled: no change in {log} for 0.05 minutes\n"


def test_stall_exits_3_when_size_and_mtime_do_not_change(tmp_path):
    job = tmp_path / "job.out"
    job.write_text("quiet\n", encoding="utf-8")
    code, out, _, clock = run(
        ["--output", str(job), "--stall-minutes", "0.05", "--slice-seconds", "600", "--poll-seconds", "1"]
    )
    assert code == 3
    assert out == f"stalled: no change in {job} for 0.05 minutes\n"
    assert clock.t >= 3.0


def test_growing_output_resets_the_stall_window(tmp_path):
    job = tmp_path / "job.out"
    job.write_text("", encoding="utf-8")

    def grow(n):
        append(job, f"line {n}\n")
        if n == 10:
            append(job, TRAILER + "\n")

    code, out, _, clock = run(
        ["--output", str(job), "--stall-minutes", "0.05", "--slice-seconds", "600", "--poll-seconds", "1"],
        FakeClock(grow),
    )
    assert code == 0
    assert out == TRAILER + "\n"
    assert clock.t > 3.0


def test_slice_over_while_live_exits_4(tmp_path):
    job = tmp_path / "job.out"
    job.write_text("still running\n", encoding="utf-8")

    def keep_alive(n):
        # Output keeps changing, so only the slice can end the wait.
        append(job, f"tick {n}\n")

    code, out, _, clock = run(
        ["--output", str(job), "--stall-minutes", "10", "--slice-seconds", "5", "--poll-seconds", "1"],
        FakeClock(keep_alive),
    )
    assert code == 4
    assert out == f"slice over: {job} still live\n"
    assert clock.t == 5.0


def test_file_never_appears_exits_2(tmp_path):
    missing = tmp_path / "never.out"
    code, out, err, _ = run(
        ["--output", str(missing), "--stall-minutes", "0.01", "--poll-seconds", "0.1"]
    )
    assert code == 2
    assert out == ""
    assert err == f"no file {missing} within 0.01 minutes\n"


def test_file_appearing_in_time_is_watched(tmp_path):
    job = tmp_path / "late.out"

    def appear(n):
        if n == 2:
            job.write_text(TRAILER + "\n", encoding="utf-8")

    code, out, _, _ = run(
        ["--output", str(job), "--stall-minutes", "1", "--poll-seconds", "1"],
        FakeClock(appear),
    )
    assert code == 0
    assert out == TRAILER + "\n"


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--stall-minutes", "1"],
        ["--output", "x.out"],
        ["--output", "x.out", "--file", "y.log", "--pattern", "a", "--stall-minutes", "1"],
        ["--file", "y.log", "--stall-minutes", "1"],
        ["--output", "x.out", "--pattern", "a", "--stall-minutes", "1"],
        ["--output", "x.out", "--stall-minutes", "0"],
        ["--output", "x.out", "--stall-minutes", "-1"],
        ["--output", "x.out", "--stall-minutes", "abc"],
        ["--output", "x.out", "--stall-minutes", "nan"],
        ["--output", "x.out", "--stall-minutes", "1", "--slice-seconds", "0"],
        ["--output", "x.out", "--stall-minutes", "1", "--poll-seconds", "-2"],
        ["--file", "y.log", "--pattern", "([", "--stall-minutes", "1"],
        ["--output", "x.out", "--stall-minutes", "1", "--bogus"],
    ],
)
def test_bad_arguments_exit_2_without_polling(argv):
    code, out, err, clock = run(argv)
    assert code == 2
    assert out == ""
    assert err.startswith("bad arguments: ")
    assert clock.sleeps == 0
