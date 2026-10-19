"""The run-owning process owns its process tree.

A model session is a tree: the CLI it launched, the tools that CLI ran, and
whatever those started. A timed-out or finished run must leave none of it
behind, a recycled PID must never be signalled, and on Windows even a killed
runner must take its descendants with it (a kill-on-close Job Object).
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
from dashboard_support import (
    SCRIPTS_DIR,
    FakeEdge,
    Running,
    dashboard_world,
    runner_environment,
)

from task_system.dashboard import proctree, runner, runtime, scheduler
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.mutation import folder_revision

SLEEPER = [sys.executable, "-c", "import time; time.sleep(600)"]


def alive(pid: int) -> bool:
    ident = proctree.ident_of(pid)
    return ident is not None and proctree.live(ident) is not None


def wait_for(predicate, timeout_s=20.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


# -- identity ----------------------------------------------------------------


def test_a_pid_with_a_different_creation_time_is_not_the_process():
    child = subprocess.Popen(SLEEPER)
    try:
        real = proctree.ident_of(child.pid)
        assert proctree.live(real) is not None
        reused = proctree.ProcIdent(child.pid, real.create_time + 3600)  # same PID, another process "lifetime"
        assert proctree.live(reused) is None
        assert proctree.stop_idents([reused], grace_s=0.5) == []
        assert child.poll() is None  # the unrelated process was NOT signalled
    finally:
        child.kill()
        child.wait()


def test_stopping_a_tree_ends_every_descendant_and_reports_no_survivors():
    parent = subprocess.Popen([sys.executable, "-c",
                               "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); time.sleep(600)"])
    try:
        assert wait_for(lambda: proctree.descendants(parent.pid))
        grandchild = proctree.descendants(parent.pid)[0]
        survivors = proctree.stop_tree(proctree.ident_of(parent.pid), grace_s=3.0)
        assert survivors == []
        assert proctree.live(grandchild) is None and parent.wait(timeout=10) is not None
    finally:
        if parent.poll() is None:
            parent.kill()


# -- the runner, through the real scheduler -----------------------------------


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    monkeypatch.setattr(scheduler, "RUNNER_GRACE_S", 2.0)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")
    pid = project_id_for(project)

    class Rig:
        pass

    r = Rig()
    r.srv, r.edge, r.project, r.folder, r.pid = srv, edge, project, folder, pid
    r.queue = lambda options=None: srv.post(
        f"/api/v1/items/{pid}/dev%2Ftasks%2Falpha/do-it/queue",
        {"expected_revision": folder_revision(folder), "options": options or {"timeout_s": 2}})
    try:
        yield r
    finally:
        srv.stop()


def grandchild_pid(run) -> int:
    return int((Path(run.run_dir) / "grandchild.pid").read_text(encoding="utf-8"))


def test_a_timed_out_run_has_its_whole_tree_stopped_including_the_grandchild(rig):
    """RED CHECK: the runner owns the tree. A hung agent with a grandchild is stopped at the deadline."""
    rig.edge.behavior = "hang"
    status, _h, body = rig.queue()
    assert status == 202
    run = rig.srv.settle(body["run"]["id"])
    assert (run.state, run.code) == (runtime.TIMEOUT, "timeout")
    result = json.loads((Path(run.run_dir) / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "timeout" and result["survivors"] == []
    gc = grandchild_pid(run)
    assert wait_for(lambda: not alive(gc)), "the grandchild of the timed-out agent is still running"
    runner_ident = proctree.ProcIdent(run.runner_pid, run.runner_ctime)
    assert wait_for(lambda: proctree.live(runner_ident) is None)  # and the runner itself has exited
    # A timeout is not a block, and nothing retries it: the task is untouched until Retry.
    assert rig.srv.scheduler.db.runs((runtime.QUEUED, runtime.RUNNING)) == []


def test_a_finished_run_leaves_no_lingering_descendant(rig):
    rig.edge.behavior = "lingering"
    status, _h, body = rig.queue({"timeout_s": 60})
    run = rig.srv.settle(body["run"]["id"])
    assert run.state == runtime.ACCEPTED
    gc = grandchild_pid(run)
    assert wait_for(lambda: not alive(gc)), "the agent exited but its grandchild was left running"


def test_a_runner_that_vanishes_without_a_result_blocks_the_task_for_inspection(rig):
    rig.edge.behavior = "hang"
    status, _h, body = rig.queue({"timeout_s": 60})
    run_id = body["run"]["id"]
    live_run = rig.srv.settle(run_id, states=(runtime.RUNNING,))
    assert wait_for(lambda: (Path(live_run.run_dir) / "grandchild.pid").is_file())
    psutil.Process(live_run.runner_pid).kill()
    crashed = rig.srv.settle(run_id)
    assert (crashed.state, crashed.code) == (runtime.CRASHED, "runner-crashed")
    text = (rig.folder / "task.yaml").read_text(encoding="utf-8")
    assert "status: blocked" in text and "process vanished" in (rig.folder / "log.md").read_text(encoding="utf-8")
    assert "do-it" in (rig.folder / "plan.md").read_text(encoding="utf-8")  # nothing was reported as complete
    if os.name == "nt":  # the kill-on-close job took the descendants down with the killed runner
        gc = grandchild_pid(live_run)
        assert wait_for(lambda: not alive(gc))
    else:
        proctree.stop_idents(proctree.descendants(os.getpid()))  # best effort cleanup of a test-owned orphan


def test_the_runner_refuses_to_dispatch_when_it_cannot_own_the_tree(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    marker = tmp_path / "ran.txt"
    spec = {"project_root": str(tmp_path), "runner": {
        "argv": [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')"], "deadline_s": 30}}
    (run_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")

    def refuse():
        raise proctree.ProcTreeError("no job object")

    monkeypatch.setattr(proctree, "assign_kill_on_close_job", refuse)
    assert runner.run(run_dir) == runner.EXIT_REFUSED
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "runner-refused" and "no job object" in result["detail"]
    assert not marker.exists()  # nothing was dispatched


@pytest.mark.skipif(os.name != "nt", reason="the kill-on-close Job Object is a Windows primitive")
def test_a_killed_runner_takes_its_descendants_with_it_on_windows(tmp_path):
    script = tmp_path / "job_runner.py"
    pidfile = tmp_path / "gc.pid"
    script.write_text(
        "import subprocess, sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
        "from task_system.dashboard import proctree\n"
        "job = proctree.assign_kill_on_close_job()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(600)\n", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(SCRIPTS_DIR.parents[3] / "skills-kit")]), _BOOTSTRAP_GUARD_SKIP_REEXEC="1")
    owner = subprocess.Popen([sys.executable, str(script)], env=env)
    try:
        assert wait_for(pidfile.is_file)
        gc = int(pidfile.read_text(encoding="utf-8"))
        assert alive(gc)
        owner.kill()
        owner.wait(timeout=10)
        assert wait_for(lambda: not alive(gc)), "the descendant outlived its job's last handle"
    finally:
        if owner.poll() is None:
            owner.kill()
