"""Runtime state: the project lease, the global bound, FIFO order and server restarts."""

from __future__ import annotations

import sqlite3
import urllib.parse

import psutil
import pytest
from dashboard_support import FakeEdge, Running, dashboard_world, runner_environment

from task_system.dashboard import runtime, scheduler
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.mutation import folder_revision, TaskMutator
from task_system.dashboard.runtime import RuntimeDB


@pytest.fixture
def db(tmp_path):
    database = RuntimeDB(tmp_path / "dashboard.sqlite3")
    yield database
    database.close()


def enqueue(db, run_id, project="p1", item="i"):
    return db.enqueue(run_id=run_id, project_id=project, project_root="/x", task_ref="tmp/t", item_id=item,
                      item_title=item, options={}, source_revision="r")


def test_runs_queue_in_fifo_order(db):
    for n in range(4):
        enqueue(db, f"r{n}")
    assert [r.id for r in db.runs((runtime.QUEUED,))] == ["r0", "r1", "r2", "r3"]


def test_one_project_holds_at_most_one_lease(db):
    enqueue(db, "a", "p1")
    enqueue(db, "b", "p1")
    assert db.try_lease("a", "p1", max_projects=2) is True
    assert db.try_lease("b", "p1", max_projects=2) is False  # same project: refused whatever the global bound
    assert db.get("b").state == runtime.QUEUED
    db.set_state("a", runtime.ACCEPTED)
    assert db.try_lease("b", "p1", max_projects=2) is True  # the lease is free again


def test_the_global_bound_counts_projects_not_runs(db):
    for run_id, project in (("a", "p1"), ("b", "p2"), ("c", "p3")):
        enqueue(db, run_id, project)
    assert db.try_lease("a", "p1", max_projects=2) and db.try_lease("b", "p2", max_projects=2)
    assert db.try_lease("c", "p3", max_projects=2) is False  # two projects are busy
    db.set_state("a", runtime.FAILED)
    assert db.try_lease("c", "p3", max_projects=2) is True


def test_a_lease_can_only_be_taken_from_the_queued_state(db):
    enqueue(db, "a")
    assert db.try_lease("a", "p1", max_projects=5)
    db.set_state("a", runtime.ACCEPTED)
    assert db.try_lease("a", "p1", max_projects=5) is False  # a settled run is never restarted


def test_idle_projects_use_no_slot(db):
    enqueue(db, "a", "p1")
    db.try_lease("a", "p1", max_projects=1)
    db.set_state("a", runtime.ACCEPTED)
    enqueue(db, "b", "p2")
    assert db.try_lease("b", "p2", max_projects=1) is True


def test_a_schema_it_does_not_know_is_left_alone_loudly(tmp_path):
    path = tmp_path / "dashboard.sqlite3"
    RuntimeDB(path).close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema 99"):
        RuntimeDB(path)


def test_runs_and_events_survive_reopening_the_database(tmp_path):
    path = tmp_path / "dashboard.sqlite3"
    first = RuntimeDB(path)
    enqueue(first, "a")
    first.set_state("a", runtime.FAILED, code="x", detail="y")
    first.close()
    second = RuntimeDB(path)
    run = second.get("a")
    assert (run.state, run.code, run.detail) == (runtime.FAILED, "x", "y") and run.ended_at
    assert [e["kind"] for e in second.events("a")] == ["queued", "failed"]
    second.close()


def test_a_starting_run_that_never_spawned_goes_back_to_the_queue_on_adoption(tmp_path):
    db = RuntimeDB(tmp_path / "d.sqlite3")
    enqueue(db, "a")
    enqueue(db, "b", "p2")
    db.try_lease("a", "p1", max_projects=2)
    db.try_lease("b", "p2", max_projects=2)
    db.set_state("b", runtime.RUNNING, runner=psutil_ident())
    sched = scheduler.Scheduler(root=tmp_path, db=db, mutator=TaskMutator(tmp_path / "l"), edge=FakeEdge())
    sched.adopt()
    assert db.get("a").state == runtime.QUEUED  # no runner identity: nothing was dispatched
    assert db.get("b").state == runtime.RUNNING  # a recorded runner is left to be observed
    db.close()


def psutil_ident():
    from task_system.dashboard import proctree

    return proctree.ident_of(psutil.Process().pid)


def test_a_running_run_makes_the_server_busy_and_refuses_an_unforced_stop(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge(behavior="hang")
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")
    try:
        pid = project_id_for(project)
        status, _h, body = srv.post(f"/api/v1/items/{pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/do-it/queue",
                                    {"expected_revision": folder_revision(folder), "options": {"timeout_s": 60}})
        run = srv.settle(body["run"]["id"], states=(runtime.RUNNING,))
        assert srv.json("GET", "/api/v1/health")[2]["busy"] == 1
        status, _h, refused = srv.post("/api/v1/shutdown", {})
        assert status == 409 and refused["error"]["code"] == "busy"
        psutil.Process(run.runner_pid).kill()
        srv.settle(run.id)
        assert srv.json("GET", "/api/v1/health")[2]["busy"] == 0
    finally:
        srv.stop()


def test_a_run_survives_a_server_restart_and_is_observed_to_its_end(tmp_path, monkeypatch):
    """The runner is detached: restarting the server over the same data directory re-observes it."""
    runner_environment(monkeypatch)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge(behavior="done")
    cfg = tmp_path / "none.yaml"
    first = Running(tmp_path / "data", (devroot,), edge=edge, config_path=cfg)
    pid = project_id_for(project)
    status, _h, body = first.post(f"/api/v1/items/{pid}/{urllib.parse.quote('dev/tasks/alpha', safe='')}/do-it/queue",
                                  {"expected_revision": folder_revision(folder), "options": {"timeout_s": 60}})
    run_id = body["run"]["id"]
    first.settle(run_id, states=(runtime.RUNNING, runtime.ACCEPTED, runtime.BLOCKED))  # at least started
    first.stop()
    second = Running(tmp_path / "data", (devroot,), edge=edge, config_path=cfg)
    try:
        assert second.settle(run_id).state == runtime.ACCEPTED
    finally:
        second.stop()


# -- authoring: removed, options, batches, holds ------------------------------------------------------------------


def test_removed_is_a_terminal_non_retryable_state_outside_the_history(db):
    enqueue(db, "a")
    assert db.transition("a", (runtime.QUEUED,), runtime.REMOVED, code="removed", detail="gone")
    run = db.get("a")
    assert run.state == runtime.REMOVED and run.ended_at is not None
    assert runtime.REMOVED in runtime.TERMINAL_STATES and runtime.REMOVED not in runtime.HISTORY_STATES
    assert runtime.REMOVED not in runtime.RETRYABLE_STATES and runtime.REMOVED not in runtime.LIVE_STATES
    assert db.live_for("p1", "tmp/t", "i") is None  # the item is free to be queued again
    assert db.try_lease("a", "p1", max_projects=5) is False
    assert not db.transition("a", (runtime.QUEUED,), runtime.CANCELLED)  # only a queued entry can be removed


def test_options_change_only_while_the_entry_is_queued(db):
    enqueue(db, "a")
    assert db.set_options("a", {"effort": "low"}) is True and db.get("a").options == {"effort": "low"}
    assert db.try_lease("a", "p1", max_projects=5)
    assert db.set_options("a", {"effort": "high"}) is False and db.get("a").options == {"effort": "low"}
    assert db.set_options("ghost", {}) is False


def test_enqueue_many_is_all_or_nothing(db):
    entry = lambda run_id: dict(run_id=run_id, project_id="p1", project_root="/x", task_ref="tmp/t", item_id=run_id,  # noqa: E731
                                item_title=run_id, options={}, source_revision="r")
    runs = db.enqueue_many([entry("a"), entry("b")])
    assert [r.id for r in runs] == ["a", "b"] and [r.id for r in db.runs((runtime.QUEUED,))] == ["a", "b"]
    with pytest.raises(sqlite3.IntegrityError):
        db.enqueue_many([entry("c"), entry("a")])  # the second collides with an existing run id
    assert [r.id for r in db.runs()] == ["a", "b"]  # "c" was rolled back with it


def test_held_folders_lists_live_holds_once_and_drops_dead_ones(tmp_path, db):
    import subprocess
    import sys

    from task_system.dashboard import proctree

    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    try:
        mine, other = tmp_path / "mine", tmp_path / "other"
        mine.mkdir()
        other.mkdir()
        db.add_hold(str(mine), "s1", proctree.ident_of(live.pid))
        db.add_hold(str(other), "s2", proctree.ProcIdent(live.pid, proctree.ident_of(live.pid).create_time + 3600))  # recycled pid
        assert db.held_folders() == {runtime.folder_key(mine)}
        assert db.live_holds(str(other)) == []  # the dead one is gone for good
    finally:
        live.kill()
        live.wait()
    assert db.held_folders() == set()
