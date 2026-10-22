"""``task-kit`` after the dashboard's retirement: bare ``task-kit`` hands off to databench, ``stop`` ends a legacy server.

The entrypoint runs as a real subprocess (the file ``bin/task-kit`` execs) with a PATH the test builds, so
"databench is on PATH" and "it is not" are both observed, never assumed. The legacy ``stop`` is driven in-process with
every effect injected, plus once through the real entrypoint against a dead pid.
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
from dashboard_support import ENTRYPOINT, REPO_ROOT, clean_env

from task_system import legacy_stop

# The guard's opt-out, so the entrypoint does not exec into a plugin venv this test does not have.
SKIP_REEXEC = "_BOOTSTRAP_GUARD_SKIP_REEXEC"


def run_entrypoint(args: list[str], path_dirs: list[Path], **extra_env) -> subprocess.CompletedProcess:
    env = clean_env({"PATH": os.pathsep.join(str(d) for d in path_dirs), SKIP_REEXEC: "1", **extra_env})
    return subprocess.run([sys.executable, str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, timeout=60)


def make_fake_databench(directory: Path, record: Path, exit_code: int = 0) -> Path:
    """A ``databench`` executable that writes its argv, one per line, to ``record`` and exits ``exit_code``."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / "databench.cmd"
        path.write_text(f'@echo off\r\n(for %%a in (%*) do echo %%a) > "{record}"\r\nexit /b {exit_code}\r\n')
    else:
        path = directory / "databench"
        path.write_text(f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "{record}"\nexit {exit_code}\n')
        path.chmod(0o755)
    return path


# -- bare task-kit ---------------------------------------------------------------------------------------


def test_bare_task_kit_runs_databench_view_tasks(tmp_path):
    record = tmp_path / "argv.txt"
    make_fake_databench(tmp_path / "bin", record)
    done = run_entrypoint([], [tmp_path / "bin"])
    assert done.returncode == 0, done.stderr
    assert record.read_text().split() == ["--view", "tasks"]


def test_bare_task_kit_returns_databench_exit_status(tmp_path):
    make_fake_databench(tmp_path / "bin", tmp_path / "argv.txt", exit_code=7)
    assert run_entrypoint([], [tmp_path / "bin"]).returncode == 7


def test_bare_task_kit_without_databench_exits_one_and_says_who_provides_the_view(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    done = run_entrypoint([], [empty])
    assert done.returncode == 1
    assert "databench provides the task view" in done.stderr
    assert "not on PATH" in done.stderr and "https://github.com/kitaekatt/databench" in done.stderr
    assert done.stdout == ""


def test_a_retired_command_exits_two_and_names_what_remains(tmp_path):
    for command in ("status", "restart", "serve", "characterize"):
        done = run_entrypoint([command], [tmp_path])
        assert done.returncode == 2, command
        assert "retired" in done.stderr and "export" in done.stderr and "stop" in done.stderr


def test_export_is_still_dispatched_without_databench(tmp_path):
    """``task-kit export`` is unchanged: it answers with the protocol document whatever is on PATH."""
    done = run_entrypoint(["export", "protocols", "--format", "json"], [tmp_path],
                          PYTHONPATH=str(REPO_ROOT / "plugins" / "skills-kit"))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["payload"] == {"protocols": [1]}


# -- task-kit stop, against the legacy server.json --------------------------------------------------------


def write_record(data_dir: Path, *, pid: int, create_time: float, port: int = 9, token: str = "tok") -> Path:
    root = data_dir / "dashboard"
    root.mkdir(parents=True)
    path = root / "server.json"
    path.write_text(json.dumps({
        "schema": 1, "pid": pid, "pid_create_time": create_time, "nonce": "n", "port": port, "token": token,
        "code_hash": "a" * 64, "started_at": "2026-10-17T00:00:00+00:00", "log_path": str(root / "server.log"),
    }))
    return path


def dead_process() -> tuple[int, float]:
    """The pid and creation time of a process that has exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    created = psutil.Process(proc.pid).create_time()
    proc.wait()
    return proc.pid, created


class Effects:
    """Injected effects that record every attempt to signal or contact a server."""

    def __init__(self, *, health=None, alive_after_shutdown=False):
        self.health = health
        self.calls: list[tuple] = []
        self.alive_after_shutdown = alive_after_shutdown
        self._shut = False

    def probe(self, port):
        self.calls.append(("probe", port))
        if self.health is None:
            raise legacy_stop.ProbeFailed("refused")
        return self.health

    def shutdown(self, record, force):
        self.calls.append(("shutdown", record.pid, force))
        self._shut = True

    def kill(self, record):
        self.calls.append(("kill", record.pid))

    def signals(self):
        return [c for c in self.calls if c[0] in ("shutdown", "kill")]


def stop_with(data_dir, effects, *, create_time, force=False, out=None):
    return legacy_stop.stop(
        data_dir, force=force, out=out, probe=effects.probe, create_time=create_time,
        shutdown=effects.shutdown, kill=effects.kill, sleep=lambda _s: None, stop_timeout=0.0,
    )


def test_no_record_means_nothing_to_stop(tmp_path, capsys):
    effects = Effects()
    assert stop_with(tmp_path, effects, create_time=lambda _pid: None) == 0
    assert "nothing to stop" in capsys.readouterr().out
    assert effects.calls == []


def test_a_record_naming_a_dead_pid_is_reported_stale_and_signals_nothing(tmp_path, capsys):
    pid, created = dead_process()
    record = write_record(tmp_path, pid=pid, create_time=created)
    effects = Effects()
    assert stop_with(tmp_path, effects, create_time=legacy_stop.process_create_time) == 0
    out = capsys.readouterr().out
    assert "stale record" in out and f"pid {pid} is gone" in out and "nothing was signalled" in out
    assert effects.signals() == []
    assert record.exists()  # nothing is deleted


def test_the_real_entrypoint_reports_a_dead_pid_stale_and_signals_nothing(tmp_path):
    pid, created = dead_process()
    record = write_record(tmp_path, pid=pid, create_time=created)
    done = run_entrypoint(["stop", "--data-dir", str(tmp_path)], [tmp_path])
    assert done.returncode == 0, done.stderr
    assert "stale record" in done.stdout and "nothing was signalled" in done.stdout
    assert record.exists()


def test_a_reused_pid_is_not_the_recorded_server_and_is_never_signalled(tmp_path, capsys):
    """A live process under the recorded pid whose creation time differs is someone else's."""
    record = write_record(tmp_path, pid=os.getpid(), create_time=psutil.Process().create_time() - 3600)
    effects = Effects()
    assert stop_with(tmp_path, effects, create_time=legacy_stop.process_create_time, force=True) == 0
    assert effects.signals() == []
    assert "stale record" in capsys.readouterr().out
    assert record.exists()


def test_a_stranger_answering_on_the_recorded_port_is_never_signalled(tmp_path):
    write_record(tmp_path, pid=os.getpid(), create_time=psutil.Process().create_time() - 3600)
    effects = Effects(health={"busy": 0})
    with pytest.raises(legacy_stop.LegacyStopError, match="not ours to stop"):
        stop_with(tmp_path, effects, create_time=legacy_stop.process_create_time, force=True)
    assert effects.signals() == []


def test_stop_is_refused_while_jobs_run_unless_forced(tmp_path):
    write_record(tmp_path, pid=os.getpid(), create_time=psutil.Process().create_time())
    effects = Effects(health={"busy": 2})
    with pytest.raises(legacy_stop.BusyError, match="2 job"):
        stop_with(tmp_path, effects, create_time=legacy_stop.process_create_time)
    assert effects.signals() == []

    forced = Effects(health={"busy": 2})
    gone = iter([psutil.Process().create_time(), None])  # alive at the identity check, gone after the shutdown
    assert stop_with(tmp_path, forced, create_time=lambda _pid: next(gone), force=True) == 0
    assert forced.signals() == [("shutdown", os.getpid(), True)]


def test_a_corrupt_record_stops_the_command_and_is_left_on_disk(tmp_path):
    root = tmp_path / "dashboard"
    root.mkdir()
    (root / "server.json").write_text("{not json")
    effects = Effects()
    with pytest.raises(legacy_stop.LegacyStopError, match="unusable"):
        stop_with(tmp_path, effects, create_time=lambda _pid: None)
    assert effects.calls == [] and (root / "server.json").read_text() == "{not json"


def test_stop_refuses_inside_a_dashboard_run(tmp_path):
    """A dashboard run's command carries ``TASK_KIT_AGENT_DEPTH``; such a process may not stop its dashboard."""
    pid, created = dead_process()
    record = write_record(tmp_path, pid=pid, create_time=created)
    refused = run_entrypoint(["stop", "--data-dir", str(tmp_path)], [tmp_path], TASK_KIT_AGENT_DEPTH="1")
    assert refused.returncode == 6 and "refused inside a dashboard run" in refused.stderr
    assert record.exists()
