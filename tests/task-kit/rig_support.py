"""A dashboard rig for the cancellation and recovery tests.

The scheduler thread is frozen so a test drives ``tick()`` itself and can stop
the world at an exact point, and the "server dies" tests stop the server while
the detached runner process lives on, then start a fresh server over the same
data directory.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from pathlib import Path

import psutil
from dashboard_support import FakeEdge, Running, dashboard_world, runner_environment

from task_system.dashboard import mutation, proctree, runtime, scheduler
from task_system.dashboard.inventory import project_id_for

TASK = "dev/tasks/alpha"


class ServerDied(BaseException):
    """Raised inside the scheduler to stand for the server process dying on the spot.

    It is a BaseException so no ``except Exception`` in the scheduler turns it
    into an ordinary failure: the run stays exactly as the dying server left it.
    """


def wait_for(predicate, timeout_s: float = 20.0, step_s: float = 0.05):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step_s)
    return None


def alive(pid: int) -> bool:
    ident = proctree.ident_of(pid)
    return ident is not None and proctree.live(ident) is not None


def _cmdline(proc: psutil.Process) -> list[str]:
    try:
        return proc.cmdline() or []
    except (psutil.Error, SystemError, PermissionError, OSError):  # macOS refuses other users' processes in odd ways
        return []


def processes_running(marker: str) -> list[psutil.Process]:
    """Live processes whose command line mentions ``marker`` (a script name or a run directory)."""
    found = []
    for proc in psutil.process_iter():
        try:
            if any(marker in part for part in _cmdline(proc)) and proc.status() != psutil.STATUS_ZOMBIE:
                found.append(proc)
        except psutil.Error:
            continue
    return found


def runners_of(run_dir: Path) -> list[psutil.Process]:
    return [p for p in processes_running(str(run_dir)) if any("runner_cli.py" in a for a in _cmdline(p))]


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch, *, behavior: str = "hang", sleep_s: float = 3.0,
                 slack_s: float = 1.0, grace_s: float = 2.0, items=None, sections=None) -> None:
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", slack_s)
        monkeypatch.setattr(scheduler, "RUNNER_GRACE_S", grace_s)
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.devroot, self.project, self.folder = dashboard_world(tmp_path, items=items, sections=sections)
        self.edge = FakeEdge(behavior=behavior, sleep_s=sleep_s)
        self.config = tmp_path / "none.yaml"
        self.data = tmp_path / "data"
        self.pid = project_id_for(self.project)
        self.servers: list[Running] = []
        self.srv = self._start()
        self.freeze()  # the test queues, then drives tick() itself; restart() hands the new server its own thread

    def _start(self) -> Running:
        srv = Running(self.data, (self.devroot,), edge=self.edge, config_path=self.config)
        self.servers.append(srv)
        return srv

    # -- the server's life ---------------------------------------------------

    @property
    def db(self):
        return self.srv.scheduler.db

    def freeze(self) -> None:
        """Stop the scheduler thread; the test drives ``tick()`` from here on."""
        sched = self.srv.scheduler
        assert wait_for(lambda: sched._thread is not None and sched._thread.is_alive(), 10), "the scheduler thread never started"
        sched._stop.set()
        sched._wake.set()
        sched._thread.join(timeout=10)

    def restart(self) -> Running:
        """The server goes away (the detached runner does not) and a new one starts on the same data."""
        self.srv.stop()
        self.srv = self._start()
        return self.srv

    def close(self) -> None:
        for srv in self.servers:
            try:
                srv.stop()
            except AssertionError:
                pass
        for proc in processes_running(str(self.tmp_path)):  # nothing a test started outlives it
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass

    # -- actions ---------------------------------------------------------------

    def item_path(self, item: str, action: str, task: str = TASK) -> str:
        return f"/api/v1/items/{self.pid}/{urllib.parse.quote(task, safe='')}/{item}/{action}"

    def revision(self) -> str:
        return mutation.folder_revision(self.folder)

    def queue(self, item: str = "do-it", options: dict | None = None) -> str:
        status, _h, body = self.srv.post(self.item_path(item, "queue"), {
            "expected_revision": self.revision(), "options": options if options is not None else {"timeout_s": 60}})
        assert status == 202, body
        return body["run"]["id"]

    def cancel(self, run_id: str, body: dict | None = None):
        return self.srv.post(f"/api/v1/runs/{run_id}/cancel", body if body is not None else {})

    def run(self, run_id: str) -> runtime.Run:
        run = self.db.get(run_id)
        assert run is not None
        return run

    def tick_until(self, run_id: str, states, timeout_s: float = 60.0) -> runtime.Run:
        wanted = tuple(states)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.srv.scheduler.tick()
            run = self.run(run_id)
            if run.state in wanted:
                return run
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {wanted}: {self.run(run_id)}")

    def run_dir(self, run_id: str) -> Path:
        return self.srv.scheduler.run_dir_for(self.pid, run_id)

    def read_json(self, run_id: str, name: str):
        return json.loads((self.run_dir(run_id) / name).read_text(encoding="utf-8"))

    def ledger(self, run_id: str) -> dict:
        return self.read_json(run_id, "fake-ledger.json")

    def cancel_calls(self, run_id: str) -> list[dict]:
        path = self.run_dir(run_id) / "cancel-calls.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def plan_item_ids(self) -> list[str]:
        return [i["id"] for i in mutation.raw_items((self.folder / "plan.md").read_text(encoding="utf-8"))]

    def task_status(self) -> str:
        return mutation.stored_status((self.folder / "task.yaml").read_text(encoding="utf-8"))


_REAL_AWAIT = scheduler.Scheduler.__dict__["_await_runner"]


def restore_await(monkeypatch) -> None:
    monkeypatch.setattr(scheduler.Scheduler, "_await_runner", _REAL_AWAIT)


def die_in_spawn(rig: "Rig") -> None:
    """The next dispatch creates its run directory, then the 'server' dies before spawning the runner."""

    def spawn(argv, run_dir):
        raise ServerDied()

    rig.srv.scheduler._spawn = spawn


def die_in_await(monkeypatch, *, until_result: bool = False) -> None:
    """The next dispatch spawns its runner, then the 'server' dies before recording it."""

    def await_runner(run_dir, timeout_s: float = 20.0):
        if until_result:
            assert wait_for(lambda: (Path(run_dir) / "result.json").is_file(), 60), "the runner never finished"
        raise ServerDied()

    monkeypatch.setattr(scheduler.Scheduler, "_await_runner", staticmethod(await_runner))
