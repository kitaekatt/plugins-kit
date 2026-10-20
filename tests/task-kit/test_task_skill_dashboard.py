"""Authoring seen from the task skill: the harness is never in the dashboard's way, and the dashboard never in the harness's.

Two red checks run the real `/task work` handshake (`task_hold.py`) and the real `task.py` verbs against a live
dashboard server doing authoring work beside them:

1. A session working a task wins: its hold refuses authoring writes on that task and keeps queued runs queued.
2. Work on task B proceeds untouched while a dashboard run works task A, and authoring on B is not blocked by A.

The user's ruling is the reason: the task system keeps working, uninterrupted, while the dashboard runs beside it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from authoring_support import rig  # noqa: F401 -- rig is a fixture
from dashboard_support import PLUGIN_ROOT, SCRIPTS_DIR, TASK_CLI, init_repo, run_task_cli, tree_digest

from task_system.dashboard import runtime

TASK_HOLD = SCRIPTS_DIR / "task_hold.py"


def cli_env():
    return dict(os.environ, PYTHONPATH=os.pathsep.join([str(PLUGIN_ROOT.parent / "skills-kit"), str(SCRIPTS_DIR)]),
                _BOOTSTRAP_GUARD_SKIP_REEXEC="1", CLAUDE_SESSION_ID="test-session")


def skill_hold(rig, task: str, session_pid: int, session: str = "skill-session") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TASK_HOLD), f"dev/tasks/{task}", "--root", str(rig.project), "--data-dir", str(rig.srv.data_dir),
         "--session", session, "--session-pid", str(session_pid)],
        capture_output=True, env=cli_env(), cwd=rig.project, timeout=60)


def task_work(rig, task: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TASK_CLI), "work", f"dev/tasks/{task}", "--root", str(rig.project)],
                          capture_output=True, env=cli_env(), cwd=rig.project, timeout=60)


def test_a_session_working_a_task_wins_over_authoring_and_queued_runs(rig):
    """RED CHECK 1: make the start loop ignore holds and Queue all's runs start under the session."""
    rig.freeze_scheduler()
    session = rig.session()
    hold = skill_hold(rig, "alpha", session.pid)
    assert (hold.returncode, hold.stdout, hold.stderr) == (0, b"", b"")
    before = tree_digest(rig.project / "dev")
    status, _h, body = rig.add(title="Mid session", details="d", queue=True)
    assert status == 409 and body["error"]["code"] == "held", body
    assert tree_digest(rig.project / "dev") == before  # nothing was written under the session

    status, _h, queued = rig.queue_all(options={"timeout_s": 30})
    assert status == 202, queued
    for _ in range(4):
        rig.srv.scheduler.tick()
        time.sleep(0.1)
    assert [r.state for r in rig.srv.scheduler.db.runs()] == [runtime.QUEUED, runtime.QUEUED]
    assert not (rig.srv.server.root / "projects").exists()  # not even translated into a run directory

    rig.end_session(session)
    first = queued["runs"][0]["id"]
    assert rig.srv.settle(first).state == runtime.ACCEPTED  # the head starts once the session is gone


def test_work_on_one_task_proceeds_while_a_run_works_another_and_authoring_is_not_blocked(rig):
    """RED CHECK 2: make authoring refuse whenever the checkout holds a lease and B's authoring answers 409."""
    baseline = subprocess.run(
        [sys.executable, str(TASK_CLI), "work", "dev/tasks/beta", "--root", str(rig.project)],
        capture_output=True, env=cli_env(), cwd=rig.project, timeout=60)
    assert baseline.returncode == 0, baseline.stderr

    rig.edge.behavior = "hang"
    status, _h, queued = rig.queue("do-it", options={"timeout_s": 120})
    live = rig.srv.settle(queued["run"]["id"], states=(runtime.RUNNING,))
    alpha_before = tree_digest(rig.alpha)

    session = rig.session()
    hold = skill_hold(rig, "beta", session.pid)
    work = task_work(rig, "beta")
    assert (hold.returncode, hold.stdout, hold.stderr) == (0, b"", b"")  # no notice: the live run is on another task
    assert (work.returncode, work.stdout, work.stderr) == (baseline.returncode, baseline.stdout, baseline.stderr)
    rig.end_session(session)  # the session ends, so B is free to author on again

    assert rig.add("beta", title="Beta extra", details="d")[0] == 201
    assert rig.defer("b-item", "deferred", "beta")[0] == 200
    assert rig.new_task(title="Another one")[0] == 201
    assert rig.srv.scheduler.db.get(live.id).state == runtime.RUNNING
    assert tree_digest(rig.alpha) == alpha_before  # A's folder was never touched


def normalized(result: subprocess.CompletedProcess, root: Path) -> tuple:
    return (result.returncode, result.stdout.replace(str(root).encode(), b"<root>"), result.stderr.replace(str(root).encode(), b"<root>"))


def test_the_task_system_treats_a_dashboard_authored_folder_like_one_made_with_the_cli(rig, tmp_path):
    status, _h, body = rig.new_task(title="Made on the dashboard", priority="P1", first_item={"title": "Start here", "details": "Begin."})
    assert status == 201 and "followup_error" not in body
    made = rig.project / body["path"]

    twin_root = tmp_path / "cli-twin"
    init_repo(twin_root)
    created = run_task_cli(["init", "Made on the dashboard", "--dest", "dev/tasks", "--root", str(twin_root)], twin_root)
    assert created.returncode == 0
    run_task_cli(["update", body["path"], "--priority", "P1", "--root", str(twin_root)], twin_root)
    twin = twin_root / body["path"]
    for name in ("plan.md", "log.md"):  # the two files the dashboard authored beyond init
        (twin / name).write_bytes((made / name).read_bytes())

    for args in (["items", body["path"]], ["show", body["path"]], ["validate", body["path"]], ["status", body["path"]],
                 ["list", "--scope", "project"]):
        ours = run_task_cli(args + ["--root", str(rig.project)], rig.project)
        theirs = run_task_cli(args + ["--root", str(twin_root)], twin_root)
        if args[0] == "list":  # the project also holds alpha and beta
            assert body["path"].split("/")[-1].encode() in ours.stdout
            continue
        assert normalized(ours, rig.project) == normalized(theirs, twin_root), args
    work_ours = subprocess.run([sys.executable, str(TASK_CLI), "work", body["path"], "--root", str(rig.project)],
                               capture_output=True, env=cli_env(), cwd=rig.project, timeout=60)
    work_twin = subprocess.run([sys.executable, str(TASK_CLI), "work", body["path"], "--root", str(twin_root)],
                               capture_output=True, env=cli_env(), cwd=twin_root, timeout=60)
    assert normalized(work_ours, rig.project) == normalized(work_twin, twin_root)


def test_existing_verbs_stay_byte_identical_while_authoring_hits_other_tasks(rig):
    verbs = (["items", "dev/tasks/alpha"], ["show", "dev/tasks/alpha"], ["validate", "dev/tasks/alpha"], ["status", "dev/tasks/alpha"])
    without = [run_task_cli(args + ["--root", str(rig.project)], rig.project) for args in verbs]
    assert all(r.returncode == 0 for r in without)
    stop = threading.Event()
    wrote = []

    def author():
        n = 0
        while not stop.is_set() and n < 6:
            n += 1
            wrote.append(rig.add("beta", title=f"Beta item {n}", details="d")[0])
            wrote.append(rig.defer("b-item", "deferred" if n % 2 else "available", "beta")[0])

    worker = threading.Thread(target=author)
    worker.start()
    try:
        during = [run_task_cli(args + ["--root", str(rig.project)], rig.project) for args in verbs]
    finally:
        stop.set()
        worker.join(60)
    assert wrote and all(status in (200, 201) for status in wrote)
    for args, a, b in zip(verbs, without, during):
        assert (a.returncode, a.stdout, a.stderr) == (b.returncode, b.stdout, b.stderr), args
