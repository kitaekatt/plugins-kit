"""A rig for the dashboard's task-authoring tests: a real server, a git project with two tasks, a fake llm-kit.

``rig`` builds ``proj`` (a git repository whose ``dev/tasks`` is ignored) holding ``alpha`` (items ``do-it`` and
``later-one``, each with a section) and ``beta`` (item ``b-item``), and a server over it with the fake edge.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.parse

import psutil
import pytest
from dashboard_support import FakeEdge, Running, dashboard_world, make_task, runner_environment
from rig_support import processes_running, wait_for

from task_system.dashboard import proctree, runtime, scheduler
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.mutation import folder_revision

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


class AuthoringRig:
    def __init__(self, srv, edge, project, alpha, beta, tmp_path):
        self.srv, self.edge, self.project, self.alpha, self.beta, self.tmp_path = srv, edge, project, alpha, beta, tmp_path
        self.checkout = project_id_for(project)
        self.sessions: list[subprocess.Popen] = []

    # -- addressing -----------------------------------------------------------
    def task_path(self, task: str = "alpha", tail: str = "") -> str:
        return f"/api/v1/tasks/{self.checkout}/{urllib.parse.quote('dev/tasks/' + task, safe='')}/{tail}".rstrip("/")

    def item_path(self, item: str, tail: str = "", task: str = "alpha") -> str:
        return f"/api/v1/items/{self.checkout}/{urllib.parse.quote('dev/tasks/' + task, safe='')}/{item}" + (f"/{tail}" if tail else "")

    def folder(self, task: str):
        return self.project / "dev" / "tasks" / task

    def rev(self, task: str = "alpha") -> str:
        return folder_revision(self.folder(task))

    @property
    def project_id(self) -> str:
        status, _h, body = self.srv.json("GET", "/api/v1/dashboard")
        assert status == 200
        return body["projects"][0]["id"]

    # -- calls ----------------------------------------------------------------
    def _with_revision(self, task: str, revision, body: dict) -> dict:
        """``revision`` None = the folder's current one, False = leave it out, a string = that one."""
        if revision is not False:
            body["expected_revision"] = revision or self.rev(task)
        return body

    def add(self, task: str = "alpha", *, revision=None, **body):
        body.setdefault("title", "A new item")
        return self.srv.send("POST", self.task_path(task, "items"), self._with_revision(task, revision, body))

    def queue_all(self, task: str = "alpha", *, revision=None, **body):
        return self.srv.send("POST", self.task_path(task, "queue"), self._with_revision(task, revision, body))

    def defer(self, item: str, state: str, task: str = "alpha", *, revision=None):
        return self.srv.send("PATCH", self.item_path(item, task=task), self._with_revision(task, revision, {"state": state}))

    def queue(self, item: str = "do-it", task: str = "alpha", options: dict | None = None, *, revision=None):
        body = {} if options is None else {"options": options}
        return self.srv.post(self.item_path(item, "queue", task), self._with_revision(task, revision, body))

    def recheck(self, task: str = "alpha", *, revision=None):
        return self.srv.post(self.item_path("do-it", "recheck", task), self._with_revision(task, revision, {}))

    def run_options(self, item: str, options: dict, task: str = "alpha"):
        return self.srv.send("PUT", self.item_path(item, "run-options", task), {"options": options})

    def remove(self, run_id: str):
        return self.srv.send("DELETE", f"/api/v1/queue/{run_id}", {})

    def new_task(self, **body):
        return self.srv.send("POST", f"/api/v1/projects/{self.project_id}/tasks", body)

    def dashboard(self):
        return self.srv.json("GET", "/api/v1/dashboard")[2]

    # -- holds ---------------------------------------------------------------
    def session(self) -> subprocess.Popen:
        proc = subprocess.Popen(SLEEPER)
        self.sessions.append(proc)
        return proc

    def hold(self, folder, proc=None, name: str = "s1") -> subprocess.Popen:
        proc = proc or self.session()
        self.srv.scheduler.register_hold(str(folder), name, proctree.ident_of(proc.pid))
        return proc

    def freeze_scheduler(self) -> None:
        """Stop the background tick loop: from here a run starts only when the test calls ``tick()``.

        The server thread starts the scheduler thread, so a test that reaches here straight after building the rig
        may find ``Scheduler.start`` has only assigned ``_thread``. ``join`` on an unstarted thread raises, so wait
        until the thread is alive, as ``rig_support.freeze`` does.
        """
        sched = self.srv.scheduler
        assert wait_for(lambda: sched._thread is not None and sched._thread.is_alive(), 10), "the scheduler thread never started"
        sched.stop()

    def end_session(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()

    def plan(self, task: str = "alpha") -> str:
        return (self.folder(task) / "plan.md").read_text(encoding="utf-8")

    def log(self, task: str = "alpha") -> str:
        return (self.folder(task) / "log.md").read_text(encoding="utf-8")


def make_rig(tmp_path, monkeypatch, *, edge: FakeEdge | None = None, min_interval: float = 0.0):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, alpha = dashboard_world(tmp_path)
    beta = make_task(project, "dev/tasks", "beta", title="Beta",
                     items=[{"id": "b-item", "title": "B item", "state": "available"}],
                     sections={"b-item": "Do b."}, log="- 2026-10-01 09:00: began\n")
    edge = edge or FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml", min_interval=min_interval)
    return AuthoringRig(srv, edge, project, alpha, beta, tmp_path)


def teardown_rig(rig: AuthoringRig) -> None:
    for run in rig.srv.scheduler.db.runs(runtime.ACTIVE_STATES):
        if run.runner_pid:
            try:
                psutil.Process(run.runner_pid).kill()
            except psutil.NoSuchProcess:
                pass
    for proc in rig.sessions:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    rig.srv.stop()
    # Killing the runner leaves the job command it started (a stand-in model, say) running as an orphan:
    # nothing a test started outlives it, as rig_support.Rig.close does.
    for proc in processes_running(str(rig.tmp_path)):
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    built = make_rig(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        teardown_rig(built)
