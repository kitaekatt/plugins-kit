"""The harness always wins: interactive holds and the `/task work` handshake.

The user keeps working tasks in Claude Code, uninterrupted, while the dashboard
runs beside it. So the relationship is one-way:

- a hold is registered by the harness, per TASK FOLDER (never per project),
  keyed by the Claude session's process identity, and it ends with the session;
- the scheduler yields to a hold and never the reverse;
- the handshake that registers a hold can never stall or fail `/task work`: with
  a corrupt record or a dead or hung server it prints one loud line and gets out
  of the way, leaving the work verb's own output byte-identical.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import psutil
import pytest
from dashboard_support import (
    PLUGIN_ROOT,
    SCRIPTS_DIR,
    FakeEdge,
    Running,
    agent_world,
    make_task,
    runner_environment,
)

from task_system.dashboard import handshake, proctree, runtime, scheduler
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.mutation import folder_revision

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]
TASK_HOLD = SCRIPTS_DIR / "task_hold.py"
TASK_CLI = SCRIPTS_DIR / "task.py"


@pytest.fixture
def session():
    """A stand-in for the Claude Code process: any long-lived process the hold can name."""
    proc = subprocess.Popen(SLEEPER)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, folder = agent_world(tmp_path)
    beta = make_task(project, "tmp", "beta", title="Beta",
                     items=[{"id": "b-item", "title": "B item", "state": "available"}],
                     sections={"b-item": "Do b."}, log="- 2026-10-01 09:00: began\n")
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")

    class Rig:
        pass

    r = Rig()
    r.srv, r.edge, r.project, r.alpha, r.beta, r.pid = srv, edge, project, folder, beta, project_id_for(project)
    r.queue = lambda task, item, options=None: srv.post(
        f"/api/v1/items/{r.pid}/{urllib.parse.quote('tmp/' + task, safe='')}/{item}/queue",
        {"expected_revision": folder_revision(project / "tmp" / task), "options": options or {"timeout_s": 30}})
    r.hold = lambda folder, proc, name="s1": srv.scheduler.db.add_hold(str(folder), name, proctree.ident_of(proc.pid))
    try:
        yield r
    finally:
        srv.stop()


# -- holds ---------------------------------------------------------------------


def test_a_hold_ends_with_its_session_and_a_recycled_pid_is_not_the_session(tmp_path, session):
    db = runtime.RuntimeDB(tmp_path / "db.sqlite3")
    folder = tmp_path / "folder"
    folder.mkdir()
    db.add_hold(str(folder), "s1", proctree.ident_of(session.pid))
    db.add_hold(str(folder), "s2", proctree.ProcIdent(session.pid, proctree.ident_of(session.pid).create_time + 3600))
    assert [h["session"] for h in db.live_holds(str(folder))] == ["s1"]  # the recycled identity expired at once
    session.kill()
    session.wait()
    assert db.live_holds(str(folder)) == []  # and the session's own hold ended with it
    db.close()


def test_the_scheduler_never_starts_a_task_the_harness_holds_then_starts_it_when_the_hold_ends(rig, session):
    """RED CHECK: remove the hold check and this run starts under the user's feet."""
    rig.hold(rig.alpha, session)
    status, _h, body = rig.queue("alpha", "do-it")
    assert status == 202  # queueing is allowed; running waits
    run_id = body["run"]["id"]
    for _ in range(4):
        rig.srv.scheduler.tick()
        time.sleep(0.2)
    assert rig.srv.scheduler.db.get(run_id).state == runtime.QUEUED
    assert not (rig.srv.server.root / "projects").exists()  # nothing was even translated into a run directory
    session.kill()
    session.wait()
    assert rig.srv.settle(run_id).state == runtime.ACCEPTED


def test_a_hold_on_one_task_does_not_hold_another_task_in_the_same_project(rig, session):
    rig.hold(rig.alpha, session)
    status, _h, body = rig.queue("beta", "b-item")
    assert status == 202
    assert rig.srv.settle(body["run"]["id"]).state in (runtime.BLOCKED, runtime.ACCEPTED, runtime.FAILED)  # it ran: not held
    rows = rig.srv.scheduler.db.get(body["run"]["id"])
    assert rows.run_dir is not None and rows.started_at is not None


def test_recheck_releases_the_task_hold(rig, session):
    rig.hold(rig.alpha, session)
    rig.hold(rig.beta, session)
    assert rig.srv.scheduler.hold_for(str(rig.alpha))
    status, _h, body = rig.srv.post(
        f"/api/v1/items/{rig.pid}/tmp%2Falpha/do-it/recheck", {"expected_revision": folder_revision(rig.alpha)})
    assert status == 200 and body["holds_released"] == 1
    assert rig.srv.scheduler.hold_for(str(rig.alpha)) is None and rig.srv.scheduler.hold_for(str(rig.beta))


def test_the_handshake_registers_a_hold_keyed_by_session_and_a_live_run_gets_one_notice(rig, session):
    rig.edge.behavior = "hang"
    status, _h, body = rig.queue("alpha", "do-it", {"timeout_s": 60})
    live = rig.srv.settle(body["run"]["id"], states=(runtime.RUNNING,))
    err = io.StringIO()
    assert handshake.run("tmp/alpha", rig.project, session="sess-123", data_dir=rig.srv.data_dir, session_pid=session.pid, err=err) == 0
    holds = rig.srv.scheduler.db.live_holds(str(rig.alpha))
    assert [h["session"] for h in holds] == ["sess-123"] and holds[0]["pid"] == session.pid
    lines = err.getvalue().splitlines()
    assert len(lines) == 1 and live.id in lines[0] and "keeps running" in lines[0] and "/task work proceeds unchanged" in lines[0]
    # The run was not touched: the harness proceeds and the dashboard yields next time.
    assert rig.srv.scheduler.db.get(live.id).state == runtime.RUNNING
    psutil.Process(live.runner_pid).kill()


def test_the_handshake_is_silent_when_nothing_runs_on_the_task(rig, session):
    err = io.StringIO()
    handshake.run("tmp/alpha", rig.project, session="s", data_dir=rig.srv.data_dir, session_pid=session.pid, err=err)
    assert err.getvalue() == "" and len(rig.srv.scheduler.db.live_holds(str(rig.alpha))) == 1


def test_a_hold_request_needs_the_token_and_a_complete_body(rig, session):
    ident = proctree.ident_of(session.pid)
    body = {"folder": str(rig.alpha), "session": "s", "pid": ident.pid, "create_time": ident.create_time}
    # (no body: the server rejects before reading one, and a client still writing would race the close)
    assert rig.srv.request("POST", "/api/v1/holds", token=False,
                           headers={"Origin": f"http://127.0.0.1:{rig.srv.port}", "Content-Type": "application/json"})[0] == 401
    status, _h, error = rig.srv.post("/api/v1/holds", {"folder": str(rig.alpha)})
    assert status == 422 and error["error"]["code"] == "bad_hold"


# -- the handshake can never get in the harness's way ------------------------------------


def write_record(data_dir: Path, text: str) -> Path:
    root = data_dir / "dashboard"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "server.json"
    path.write_text(text, encoding="utf-8")
    return path


def valid_record(port: int) -> str:
    me = psutil.Process()
    return json.dumps({"schema": 1, "pid": me.pid, "pid_create_time": me.create_time(), "nonce": "n", "port": port,
                       "token": "t", "code_hash": "h", "started_at": "2026-10-01T00:00:00+00:00", "log_path": "x"})


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class HungServer:
    """Accepts connections and never answers."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.held = []
        self.stop = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()
        return self

    def _accept(self):
        self.sock.settimeout(0.2)
        while not self.stop.is_set():
            try:
                self.held.append(self.sock.accept()[0])
            except (socket.timeout, OSError):
                continue

    def __exit__(self, *exc):
        self.stop.set()
        for conn in self.held:
            conn.close()
        self.sock.close()


def cli_env():
    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(PLUGIN_ROOT.parent / "skills-kit"), str(SCRIPTS_DIR)]),
                _BOOTSTRAP_GUARD_SKIP_REEXEC="1", CLAUDE_SESSION_ID="test-session")


def skill_flow(project: Path, data_dir: Path, session_pid: int):
    """What the skill does: the handshake, then the unchanged work verb. Each step's raw outputs."""
    env = cli_env()
    hold = subprocess.run([sys.executable, str(TASK_HOLD), "tmp/alpha", "--root", str(project), "--data-dir", str(data_dir),
                           "--session-pid", str(session_pid)], capture_output=True, env=env, cwd=project, timeout=60)
    work = subprocess.run([sys.executable, str(TASK_CLI), "work", "tmp/alpha", "--root", str(project)],
                          capture_output=True, env=env, cwd=project, timeout=60)
    return hold, work


def test_a_broken_dashboard_leaves_work_output_byte_identical_apart_from_one_notice_line(tmp_path, session):
    """RED CHECK: a corrupt server.json and a non-responding port, each."""
    _devroot, project, _folder = agent_world(tmp_path, repo=False)
    quiet_dir = tmp_path / "data-none"
    quiet_dir.mkdir()
    hold0, work0 = skill_flow(project, quiet_dir, session.pid)
    assert work0.returncode == 0 and b"== task init" in work0.stdout
    assert (hold0.returncode, hold0.stdout, hold0.stderr) == (0, b"", b"")  # no dashboard: a silent no-op

    scenarios = {}
    corrupt = tmp_path / "data-corrupt"
    scenarios["corrupt"] = (corrupt, write_record(corrupt, "{not json"))
    unreachable = tmp_path / "data-dead"
    scenarios["dead-port"] = (unreachable, write_record(unreachable, valid_record(closed_port())))
    wrong_shape = tmp_path / "data-shape"
    scenarios["wrong-shape"] = (wrong_shape, write_record(wrong_shape, json.dumps({"schema": 1})))
    for name, (data_dir, record) in scenarios.items():
        hold, work = skill_flow(project, data_dir, session.pid)
        assert hold.returncode == 0 and hold.stdout == b"", name
        lines = hold.stderr.decode("utf-8").splitlines()
        assert len(lines) == 1 and "no hold registered" in lines[0] and "server.json" in lines[0], (name, lines)
        assert (work.returncode, work.stdout, work.stderr) == (work0.returncode, work0.stdout, work0.stderr), name


def test_a_hung_server_costs_at_most_the_bounded_wait_and_never_fails_work(tmp_path, session):
    _devroot, project, _folder = agent_world(tmp_path, repo=False)
    data_dir = tmp_path / "data-hung"
    with HungServer() as hung:
        write_record(data_dir, valid_record(hung.port))
        started = time.monotonic()
        hold, work = skill_flow(project, data_dir, session.pid)
        elapsed = time.monotonic() - started
    assert hold.returncode == 0 and work.returncode == 0 and b"== task init" in work.stdout
    lines = hold.stderr.decode("utf-8").splitlines()
    assert len(lines) == 1 and "handshake failed" in lines[0] and "server.json" in lines[0]
    assert handshake.TOTAL_TIMEOUT_S == 2.0 and elapsed < 40  # (two interpreter starts dominate; the wait itself is bounded below)


def test_the_handshake_itself_gives_up_within_its_bounded_wait(tmp_path, session):
    _devroot, project, _folder = agent_world(tmp_path, repo=False)
    data_dir = tmp_path / "data-hung2"
    err = io.StringIO()
    with HungServer() as hung:
        write_record(data_dir, valid_record(hung.port))
        started = time.monotonic()
        assert handshake.run("tmp/alpha", project, session="s", data_dir=data_dir, session_pid=session.pid, err=err) == 0
        elapsed = time.monotonic() - started
    assert elapsed < handshake.TOTAL_TIMEOUT_S + 1.5 and len(err.getvalue().splitlines()) == 1


def test_a_server_that_refuses_the_token_is_one_loud_line_not_a_failure(tmp_path, rig, session):
    other = tmp_path / "data-badtoken"
    record = json.loads(valid_record(rig.srv.port))
    record["token"] = "wrong"
    write_record(other, json.dumps(record))
    err = io.StringIO()
    assert handshake.run("tmp/alpha", rig.project, session="s", data_dir=other, session_pid=session.pid, err=err) == 0
    lines = err.getvalue().splitlines()
    assert len(lines) == 1 and "HTTP 401" in lines[0] and rig.srv.scheduler.db.live_holds(str(rig.alpha)) == []


def test_an_unresolvable_ref_is_left_to_the_work_verb(tmp_path, session):
    _devroot, project, _folder = agent_world(tmp_path, repo=False)
    err = io.StringIO()
    assert handshake.run("not a ref at all", project, session="s", data_dir=tmp_path / "x", session_pid=session.pid, err=err) == 0
    assert err.getvalue() == ""
