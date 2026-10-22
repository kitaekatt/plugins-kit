"""The dashboard and working-session registrations.

Ruling (user, 2026-10-21): "We never prevent an action because of an unverifiable state". So exactly one kind of
registration holds a task in this dashboard: this machine's own record whose session is VERIFIED running (process id
plus the exact creation stamp). This machine's not-running records and every other machine's records are shown and
block nothing. Re-check reports registrations and never releases one. Dismissing another machine's registration is
recorded in git, in THIS machine's own session file, and published.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest
import yaml
from dashboard_support import (
    SCRIPTS_DIR,
    FakeEdge,
    Running,
    dashboard_world,
    init_repo,
    make_task,
    runner_environment,
    tree_digest,
)
from git_remote_support import git as run_git
from git_remote_support import isolate_git_environment, make_remote

from task_system import machine as machine_module
from task_system import procstamp, session_publish, sessions
from task_system.dashboard import runtime, scheduler
from task_system.dashboard.inventory import Inventory, project_id_for
from task_system.dashboard.mutation import folder_revision
from task_system.dashboard.sessions_view import SessionsReader
from task_system.harness import Identity
from task_system.machine import Machine

HOST = Machine("testhost", "abcdef0123456789", "macos")
OTHER = Machine("otherhost", "0123456789abcdef", "linux")
SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]
NODE = shutil.which("node")


class Rig:
    """A dashboard over one project (task ``alpha``, item ``do-it``) with a SessionsReader for machine HOST."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        self.tmp = tmp_path
        self.devroot, self.project, self.alpha = dashboard_world(tmp_path)
        self.pid = project_id_for(self.project)
        self.procs: list[subprocess.Popen] = []
        self.reader = SessionsReader(machine_value=HOST, config_path=tmp_path / "none.yaml")
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=FakeEdge(), config_path=tmp_path / "none.yaml",
                           sessions=self.reader)
        self.env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}

    @property
    def root(self) -> Path:
        return Path(os.path.realpath(self.project / "dev" / "tasks"))

    @property
    def sessions_dir(self) -> Path:
        return self.root / ".sessions"

    def proc(self) -> subprocess.Popen:
        proc = subprocess.Popen(SLEEPER)
        self.procs.append(proc)
        return proc

    def kill(self, proc: subprocess.Popen) -> None:
        proc.kill()
        proc.wait()

    def register(self, session_id: str, *, machine: Machine = HOST, ref: str = "alpha", proc=None):
        proc = proc or self.proc()
        ident = Identity(session_id, proc.pid, procstamp.stamp_of(proc.pid))
        sessions.register(ident, machine, self.project, ref, data_dir=self.srv.data_dir, env=self.env)
        return proc

    def dashboard(self) -> dict:
        status, _h, body = self.srv.json("GET", "/api/v1/dashboard")
        assert status == 200
        return body

    def row(self, stub: str = "alpha") -> dict:
        return next(t for t in self.dashboard()["tasks"] if t["stub"] == stub)

    def queue(self, item: str = "do-it"):
        return self.srv.post(
            f"/api/v1/items/{self.pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/{item}/queue",
            {"expected_revision": folder_revision(self.alpha), "options": {"timeout_s": 30}})

    def add_item(self, title: str = "A new item"):
        return self.srv.post(f"/api/v1/tasks/{self.pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/items",
                             {"expected_revision": folder_revision(self.alpha), "title": title})

    def recheck(self):
        return self.srv.post(f"/api/v1/items/{self.pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/do-it/recheck",
                             {"expected_revision": folder_revision(self.alpha)})

    def close(self) -> None:
        self.srv.stop()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    built = Rig(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def records_of(rig: Rig, machine: Machine = HOST):
    return sessions.read_root(rig.root, machine).records


# -- what holds ----------------------------------------------------------------------------------------------


def test_a_registration_verified_running_here_holds_the_task_and_stops_holding_when_the_process_ends(rig):
    """RED CHECK: make ``SessionsReader.holders`` return nothing and the run starts under the working session."""
    proc = rig.register("sess-running-1")
    status, _h, body = rig.queue()
    assert status == 202  # queueing is allowed; running waits
    run_id = body["run"]["id"]
    for _ in range(4):
        rig.srv.scheduler.tick()
        time.sleep(0.15)
    assert rig.srv.scheduler.db.get(run_id).state == runtime.QUEUED
    assert not (rig.srv.server.root / "projects").exists()  # nothing was even translated into a run directory
    waiting = rig.srv.scheduler.queue_view()[run_id][1]
    assert waiting["code"] == "held" and "sess-run" in waiting["detail"] and "testhost" in waiting["detail"]
    row = rig.row()
    assert row["held"] is True
    assert [(s["state"], s["mine"], s["holds"]) for s in row["sessions"]] == [("running", True, True)]
    status, _h, refused = rig.add_item()  # authoring is refused, naming the session
    assert status == 409 and refused["error"]["code"] == "held" and "sess-run" in refused["error"]["message"]
    rig.kill(proc)
    assert rig.srv.settle(run_id).state == runtime.ACCEPTED  # the registration did not hold once its process was gone
    assert [r.session_id for r in records_of(rig)] == ["sess-running-1"]  # and nothing released it


@pytest.mark.parametrize("kind", ["local-not-running", "remote"])
def test_a_registration_that_is_not_verified_running_here_is_shown_and_blocks_nothing(rig, kind):
    """RED CHECK: let a not-running local record, or another machine's record, hold and the run never starts."""
    if kind == "local-not-running":
        rig.kill(rig.register("sess-dead-0001"))
        expected = ("not running", True)
    else:
        rig.register("sess-far-00001", machine=OTHER)
        expected = ("unverified", False)
    before = tree_digest(rig.sessions_dir)
    row = rig.row()
    assert row["held"] is False
    assert [(s["state"], s["mine"], s["holds"]) for s in row["sessions"]] == [(*expected, False)]
    status, _h, added = rig.add_item()  # authoring works
    assert status == 201, added
    status, _h, body = rig.queue()
    assert status == 202
    assert rig.srv.settle(body["run"]["id"]).state == runtime.ACCEPTED  # the run starts and finishes
    assert tree_digest(rig.sessions_dir) == before  # no registration was touched


def test_the_snapshot_revision_moves_when_a_session_process_dies(rig):
    proc = rig.register("sess-running-1")
    first = rig.dashboard()
    assert first["tasks"][0]["sessions"][0]["state"] == "running"
    rig.kill(proc)
    second = rig.dashboard()
    assert second["tasks"][0]["sessions"][0]["state"] == "not running"
    assert second["revision"] != first["revision"]


def test_an_unidentifiable_machine_is_reported_and_holds_nothing(tmp_path, monkeypatch):
    """The registrations cannot be matched to this machine: that is shown as an error, never as a hold."""
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)

    def no_machine(**_kw):
        raise machine_module.MachineError("no machine id")

    monkeypatch.setattr(machine_module, "current", no_machine)
    devroot, project, folder = dashboard_world(tmp_path)
    srv = Running(tmp_path / "data", (devroot,), edge=FakeEdge(), config_path=tmp_path / "none.yaml",
                  sessions=SessionsReader(config_path=tmp_path / "none.yaml"))
    try:
        status, _h, body = srv.json("GET", "/api/v1/dashboard")
        assert status == 200 and any(e["code"] == "sessions" for e in body["errors"])
        pid = project_id_for(project)
        status, _h, queued = srv.post(
            f"/api/v1/items/{pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/do-it/queue",
            {"expected_revision": folder_revision(folder), "options": {"timeout_s": 30}})
        assert status == 202
        assert srv.settle(queued["run"]["id"]).state == runtime.ACCEPTED
    finally:
        srv.stop()


# -- Re-check ------------------------------------------------------------------------------------------------


def test_recheck_reports_registrations_and_releases_none_of_them(rig):
    """RED CHECK: have Re-check call ``sessions.release(..., all_local=True)`` and the digest changes."""
    rig.register("sess-running-1")
    rig.kill(rig.register("sess-dead-0001"))
    rig.register("sess-far-00001", machine=OTHER)
    before = tree_digest(rig.sessions_dir)
    status, _h, body = rig.recheck()
    assert status == 200 and body["holds_released"] == 0
    assert sorted(s["state"] for s in body["sessions"]) == ["not running", "running", "unverified"]
    assert tree_digest(rig.sessions_dir) == before
    assert len(records_of(rig)) == 3


# -- one read per physical root ----------------------------------------------------------------------------------


def test_the_inventory_reads_each_physical_root_once_however_many_checkouts_reach_it(tmp_path):
    from dashboard_support import link_dir

    shared = tmp_path / "shared-tasks"
    make_task(shared.parent, "shared-tasks", "shared-task", title="Shared")
    devroot = tmp_path / "devroot"
    for name in ("proj-a", "proj-b", "proj-c"):
        (devroot / name / "dev").mkdir(parents=True)
        link_dir(devroot / name / "dev" / "tasks", shared)
    calls: list[Path] = []

    class Counting(SessionsReader):
        def read(self, root):
            calls.append(Path(root))
            return super().read(root)

    reader = Counting(machine_value=HOST, config_path=tmp_path / "none.yaml")
    ident = Identity("sess-one-0001", os.getpid(), procstamp.stamp_of(os.getpid()))
    sessions.register(ident, HOST, devroot / "proj-b", "shared-task", data_dir=tmp_path / "d",
                      env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    snap = Inventory(parents=(devroot,), min_interval=0.0, sessions=reader).snapshot()
    assert len(calls) == 1 and calls[0] == Path(os.path.realpath(shared))
    (task,) = [t for p in snap.projects for t in p.tasks]
    assert [(v.session_id, v.state) for v in task.sessions] == [("sess-one-0001", "running")]
    assert len(snap.session_roots) == 1


# -- the background pass ---------------------------------------------------------------------------------------


def test_the_background_pass_drops_only_this_machines_records_on_closed_archived_or_missing_tasks(rig):
    """RED CHECK: let the pass also drop a record whose process is not running and the open-task dead record goes."""
    for stub in ("closed-one", "archived-one", "gone-one"):
        make_task(rig.project, "dev/tasks", stub, title=stub)
    open_running = rig.register("sess-open-run1")
    rig.kill(rig.register("sess-open-dead"))
    rig.register("sess-open-far1", machine=OTHER)
    rig.register("sess-closed-r1", ref="closed-one")
    rig.kill(rig.register("sess-arch-dead", ref="archived-one"))
    rig.register("sess-gone-run1", ref="gone-one")
    rig.register("sess-closed-far", machine=OTHER, ref="closed-one")
    for stub, status in (("closed-one", "closed"), ("archived-one", "archived")):
        path = rig.root / stub / "task.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("status: active", f"status: {status}"), encoding="utf-8")
    shutil.rmtree(rig.root / "gone-one")

    outcome = rig.srv.server.maintainer.once()

    assert sorted(outcome["reconciled"]) == sorted(f"{rig.root}:{t}" for t in ("archived-one", "closed-one", "gone-one"))
    mine = sorted((r.task, r.session_id, r.state) for r in records_of(rig))
    assert mine == [("alpha", "sess-open-dead", "not running"), ("alpha", "sess-open-far1", "unverified"),
                    ("alpha", "sess-open-run1", "running"), ("closed-one", "sess-closed-far", "unverified")]
    assert open_running.poll() is None


# -- publication in the background and Dismiss, over a real tasks repository ---------------------------------------


class GitRig:
    """``proj/dev/tasks`` is a clone of a bare remote; machine HOST is this dashboard, OTHER writes its own file."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        isolate_git_environment(monkeypatch, tmp_path)
        runner_environment(monkeypatch)
        self.tmp = tmp_path
        self.remote = make_remote(tmp_path)
        self.devroot = tmp_path / "devroot"
        self.project = self.devroot / "proj"
        init_repo(self.project)
        (self.project / "dev").mkdir(parents=True, exist_ok=True)
        run_git(self.project / "dev", "clone", "-q", str(self.remote), "tasks")
        self.tasks = self.project / "dev" / "tasks"
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid"), ("commit.gpgsign", "false")):
            run_git(self.tasks, "config", key, value)
        self.alpha = make_task(self.project, "dev/tasks", "alpha", title="Alpha", items=[
            {"id": "do-it", "title": "Do it", "state": "available", "priority": "P1"}],
            sections={"do-it": "Make the thing.\n\nDone when it exists."})
        self.procs: list[subprocess.Popen] = []
        self.data = tmp_path / "data"
        self.reader = SessionsReader(machine_value=HOST, config_path=tmp_path / "none.yaml")
        self.srv = Running(self.data, (self.devroot,), edge=FakeEdge(), config_path=tmp_path / "none.yaml",
                           sessions=self.reader)
        self.env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}

    def register(self, session_id: str, machine: Machine):
        proc = subprocess.Popen(SLEEPER)
        self.procs.append(proc)
        ident = Identity(session_id, proc.pid, procstamp.stamp_of(proc.pid))
        sessions.register(ident, machine, self.project, "alpha", data_dir=self.data, env=self.env)

    def other_publishes(self) -> None:
        """What machine OTHER's own publish does: commit and push its file."""
        run_git(self.tasks, "add", "--", ".sessions/otherhost.yaml")
        run_git(self.tasks, "commit", "-q", "-m", "otherhost sessions", "--", ".sessions/otherhost.yaml")
        run_git(self.tasks, "push", "-q", "origin", "HEAD:main")

    def on_remote(self, path: str) -> str | None:
        done = run_git(self.remote, "show", f"main:{path}", check=False)
        return done.stdout if done.returncode == 0 else None

    def remote_head(self) -> str:
        return run_git(self.remote, "rev-parse", "main").stdout.strip()

    def row(self) -> dict:
        status, _h, body = self.srv.json("GET", "/api/v1/dashboard")
        assert status == 200
        return next(t for t in body["tasks"] if t["stub"] == "alpha")

    def dismiss(self, session_id: str, registered_at: str, host: str = "otherhost"):
        row = self.row()
        return self.srv.post("/api/v1/sessions/dismiss", {
            "checkout": row["checkout"], "task": "dev/tasks/alpha", "host": host, "session_id": session_id,
            "registered_at": registered_at})

    def close(self) -> None:
        self.srv.stop()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.fixture
def git_rig(tmp_path, monkeypatch):
    built = GitRig(tmp_path, monkeypatch)
    try:
        yield built
    finally:
        built.close()


def test_the_background_pass_publishes_this_machines_pending_file_and_only_that_file(git_rig):
    """RED CHECK: skip the publish in ``SessionsMaintainer.once`` and the remote never gets the file."""
    git_rig.register("far-session-1", OTHER)
    git_rig.other_publishes()
    other_before = git_rig.on_remote(".sessions/otherhost.yaml")
    head_before = git_rig.remote_head()
    git_rig.register("sess-running-1", HOST)
    assert session_publish.pending(git_rig.tasks, HOST) == (".sessions/testhost.yaml",)
    assert git_rig.on_remote(".sessions/testhost.yaml") is None

    outcome = git_rig.srv.server.maintainer.once()

    assert [p["status"] for p in outcome["published"]] == ["published"]
    assert "sess-running-1" in git_rig.on_remote(".sessions/testhost.yaml")
    assert git_rig.on_remote(".sessions/otherhost.yaml") == other_before  # another machine's file is never pushed over
    changed = run_git(git_rig.remote, "diff", "--name-only", head_before, "main").stdout.split()
    assert changed == [".sessions/testhost.yaml"]
    assert session_publish.pending(git_rig.tasks, HOST) == ()
    assert git_rig.srv.server.maintainer.once()["published"] == []  # nothing pending: nothing published


def test_dismiss_hides_only_that_record_in_git_and_never_edits_another_machines_file(git_rig):
    """RED CHECK: have the route skip ``sessions.dismiss`` (or ``read_root`` ignore dismissals) and the record stays."""
    git_rig.register("far-session-1", OTHER)
    git_rig.register("far-session-2", OTHER)
    git_rig.other_publishes()
    rows = {s["session_id"]: s for s in git_rig.row()["sessions"]}
    assert set(rows) == {"far-session-1", "far-session-2"} and all(s["dismissable"] for s in rows.values())
    other_before = (git_rig.tasks / ".sessions" / "otherhost.yaml").read_bytes()
    other_remote = git_rig.on_remote(".sessions/otherhost.yaml")
    folder_before = tree_digest(git_rig.alpha)

    first = rows["far-session-1"]
    status, _h, body = git_rig.dismiss(first["session_id"], first["registered_at"])

    assert status == 200 and body["publish"]["published"] is True, body
    assert [s["session_id"] for s in git_rig.row()["sessions"]] == ["far-session-2"]  # only that record is hidden
    assert (git_rig.tasks / ".sessions" / "otherhost.yaml").read_bytes() == other_before  # one writer per file
    assert git_rig.on_remote(".sessions/otherhost.yaml") == other_remote
    assert tree_digest(git_rig.alpha) == folder_before
    entry = yaml.safe_load(git_rig.on_remote(".sessions/testhost.yaml"))["dismissed"]
    assert [(e["host"], e["session_id"], e["registered_at"]) for e in entry] == [
        ("otherhost", "far-session-1", first["registered_at"])]
    assert entry[0]["dismissed_at"]  # who (the file's host) and when


def test_dismiss_refuses_what_is_not_another_machines_registration(git_rig):
    git_rig.register("sess-mine-001", HOST)
    git_rig.register("far-session-1", OTHER)
    mine = next(s for s in git_rig.row()["sessions"] if s["mine"])
    status, _h, body = git_rig.dismiss(mine["session_id"], mine["registered_at"], host="testhost")
    assert status == 422 and body["error"]["code"] == "not_dismissable"
    status, _h, body = git_rig.dismiss("nobody-here", "2026-10-20T10:00:00Z")
    assert status == 404 and body["error"]["code"] == "unknown_session"
    assert "dismissed" not in (git_rig.tasks / ".sessions" / "testhost.yaml").read_text(encoding="utf-8")


def test_a_dismissed_session_that_registers_again_shows_again_and_the_tombstone_goes(git_rig, monkeypatch):
    git_rig.register("far-session-1", OTHER)
    git_rig.other_publishes()
    old = git_rig.row()["sessions"][0]
    assert git_rig.dismiss(old["session_id"], old["registered_at"])[0] == 200
    assert git_rig.row()["sessions"] == []
    monkeypatch.setattr(sessions, "utc_now", lambda: "2031-01-01T00:00:00Z")
    git_rig.register("far-session-1", OTHER)  # the owner's next write drops the dismissed record, then registers anew
    (again,) = git_rig.row()["sessions"]
    assert again["session_id"] == "far-session-1" and again["registered_at"] == "2031-01-01T00:00:00Z"
    outcome = git_rig.srv.server.maintainer.once()  # this machine's next write drops the entry whose target is gone
    assert any("dismissal of far-sess" in item for item in outcome["reconciled"])
    assert "dismissed" not in git_rig.on_remote(".sessions/testhost.yaml")
    assert [s["registered_at"] for s in git_rig.row()["sessions"]] == ["2031-01-01T00:00:00Z"]


# -- markers --------------------------------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="Node is not installed")
def test_the_task_row_marks_each_registration_and_the_unverified_one_is_tinted_and_dismissable(rig):
    rig.register("sess-running-1")
    rig.kill(rig.register("sess-dead-0001"))
    rig.register("sess-far-00001", machine=OTHER)
    payload = rig.dashboard()
    row = next(t for t in payload["tasks"] if t["stub"] == "alpha")
    (rig.tmp / "payload.json").write_text(json.dumps(payload), encoding="utf-8")
    (rig.tmp / "recheck.json").write_text(json.dumps({"changed": False, "holds_released": 0, "revision": "x",
                                                      "sessions": row["sessions"]}), encoding="utf-8")
    done = subprocess.run([NODE, str(Path(__file__).with_name("ui_sessions_harness.js")),
                           str(SCRIPTS_DIR / "task_system" / "dashboard" / "static" / "app.js"),
                           str(rig.tmp / "payload.json"), str(rig.tmp / "recheck.json"), row["key"]],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    rows = report["rows"]
    assert "Claude Code session working (testhost, since " in rows
    assert "not running (testhost, " in rows
    assert "on otherhost, " in rows and "on otherhost, unverified" not in rows  # a tint says it, not the word
    assert 'aria-label="on otherhost, ' in rows and ", unverified\"" in rows  # but assistive technology is told
    assert "mk sess on" in rows and "mk sess off" in rows and "mk sess far" in rows
    assert report["open"].count('data-action="dismisssession"') == 1  # only the unverified one can be dismissed
    assert 'data-host="otherhost"' in report["open"]
    assert "Re-check does not release a registration" in report["notice"] and "Still registered" in report["notice"]
    dismissals = [p for p in report["posts"] if p["url"] == "/api/v1/sessions/dismiss"]
    assert len(dismissals) == 1
    sent = json.loads(dismissals[0]["body"])
    far = next(s for s in row["sessions"] if s["dismissable"])
    assert sent == {"checkout": row["checkout"], "task": row["id"], "host": "otherhost", "session_id": far["session_id"],
                    "registered_at": far["registered_at"]}
