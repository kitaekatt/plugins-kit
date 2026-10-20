"""Parallel checkouts, fair start order, one start per tick and the reason a queued entry waits.

Every test drives the REAL dashboard server, scheduler and runner process with the fake agent
(tests/task-kit/fake_llm.py behind a FakeEdge); the scheduler thread is frozen so a test calls ``tick()`` itself.
The red checks named in each docstring were seen red against the defect they prevent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest
from dashboard_support import FakeEdge, Running, make_checkouts, runner_environment
from rig_support import processes_running, wait_for

from task_system.dashboard import mutation, proctree, runtime, scheduler
from task_system.dashboard.inventory import project_id_for
from task_system.dashboard.scheduler import QueueRefused

TASK = "dev/tasks/alpha"


class World:
    """Several checkouts, each holding the same-shaped task, under one dashboard with a frozen scheduler thread."""

    def __init__(self, tmp_path: Path, monkeypatch, names=("proj-a", "proj-b"), *, behavior="done", sleep_s=3.0, max_parallel=2,
                 items=None, sections=None, edge_kw=None) -> None:
        runner_environment(monkeypatch)
        monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
        monkeypatch.setattr(scheduler, "RUNNER_GRACE_S", 2.0)
        self.tmp_path = tmp_path
        self.devroot, self.checkouts = make_checkouts(tmp_path, names, items=items, sections=sections)
        self.config = tmp_path / "config.yaml"
        self.set_parallel(max_parallel)
        self.edge = FakeEdge(behavior=behavior, sleep_s=sleep_s, **(edge_kw or {}))
        self.srv = Running(tmp_path / "data", (self.devroot,), edge=self.edge, config_path=self.config)
        sched = self.srv.scheduler
        assert wait_for(lambda: sched._thread is not None and sched._thread.is_alive(), 10)
        sched._stop.set()
        sched._wake.set()
        sched._thread.join(timeout=10)

    @property
    def sched(self):
        return self.srv.scheduler

    def set_parallel(self, n: int) -> None:
        self.config.write_text(f"dashboard:\n  max_parallel_projects: {n}\n", encoding="utf-8")

    def project(self, name: str) -> Path:
        return self.checkouts[name][0]

    def folder(self, name: str) -> Path:
        return self.checkouts[name][1]

    def queue(self, name: str, item: str = "do-it") -> runtime.Run:
        return self.sched.queue(project_root=str(self.project(name)), task_ref=TASK, item_id=item, body={"timeout_s": 60},
                                expected_revision=None)

    def states(self) -> dict[str, str]:
        return {r.id: r.state for r in self.sched.db.runs()}

    def tick(self) -> None:
        self.sched.tick()

    def tick_until(self, run_id: str, states, timeout_s: float = 60.0) -> runtime.Run:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.tick()
            run = self.sched.db.get(run_id)
            if run.state in tuple(states):
                return run
            time.sleep(0.1)
        raise AssertionError(f"run {run_id} never reached {states}: {self.sched.db.get(run_id)}")

    def dashboard(self) -> dict:
        status, _h, body = self.srv.json("GET", "/api/v1/dashboard")
        assert status == 200
        return body

    def queued_rows(self) -> dict[str, dict]:
        return {r["id"]: r for r in self.dashboard()["runs"] if r["state"] == "queued"}

    def close(self) -> None:
        try:
            self.srv.stop()
        except AssertionError:
            pass
        for proc in processes_running(str(self.tmp_path)):
            try:
                proc.kill()
            except psutil.NoSuchProcess:
                pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = []

    def build(**kw) -> World:
        w = World(tmp_path, monkeypatch, **kw)
        made.append(w)
        return w

    yield build
    for w in made:
        w.close()


def test_the_checkout_that_started_least_recently_goes_first_and_the_page_shows_that_order(world):
    """R9. RED: order the walk by ``seq`` and A's second item starts before B's only item."""
    w = world(behavior="slow-done", sleep_s=2.0, max_parallel=1)
    a1, a2, a3 = (w.queue("proj-a", item) for item in ("do-it", "later-one", "third"))
    b1 = w.queue("proj-b")
    assert [r.id for r in w.sched.start_order(w.sched.db.runs((runtime.QUEUED,)))] == [a1.id, a2.id, a3.id, b1.id]

    w.tick()  # nothing has started anywhere: first come, first served
    assert w.states()[a1.id] == runtime.RUNNING
    rows = w.queued_rows()
    # A just started a run, B never did: B is next, then A's remaining items in their own order.
    assert [(rows[r.id]["queue_position"]) for r in (b1, a2, a3)] == [1, 2, 3]
    assert rows[b1.id]["waiting"]["code"] == "no-slot"
    assert rows[a2.id]["waiting"]["code"] == "checkout-busy"

    w.tick_until(a1.id, (runtime.ACCEPTED,))
    for _ in range(40):
        if w.states()[b1.id] != runtime.QUEUED:
            break
        w.tick()
        time.sleep(0.05)
    assert w.states()[b1.id] in (runtime.STARTING, runtime.RUNNING, runtime.ACCEPTED)
    assert w.states()[a2.id] == runtime.QUEUED  # B went before A's second item


def test_a_tick_starts_one_run_then_asks_for_another_tick(world, monkeypatch):
    """R10. RED: let the loop go on after a start (start every startable entry in one tick) and both dispatch at once."""
    w = world(behavior="hang", max_parallel=2)
    calls = []
    real = w.sched._dispatch

    def spy(run, spec, run_dir, revision, stage):
        calls.append(run.id)
        return real(run, spec, run_dir, revision, stage)

    monkeypatch.setattr(w.sched, "_dispatch", spy)
    a = w.queue("proj-a")
    b = w.queue("proj-b")
    w.sched._wake.clear()
    w.tick()
    assert calls == [a.id]
    assert w.states()[b.id] == runtime.QUEUED
    assert w.sched._wake.is_set(), "another startable entry remains, so the loop must be woken"
    w.sched._wake.clear()
    w.tick()
    assert calls == [a.id, b.id]
    assert not w.sched._wake.is_set(), "nothing startable remains"


def test_two_checkouts_run_together_and_every_waiting_entry_says_why(world):
    """R11: two runs live at once; a third checkout waits for a slot; a held one says held; lowering the bound starts nothing."""
    w = world(names=("proj-a", "proj-b", "proj-c", "proj-d"), behavior="hang", max_parallel=2)
    a1, a2 = w.queue("proj-a", "do-it"), w.queue("proj-a", "later-one")
    b1 = w.queue("proj-b")
    c1 = w.queue("proj-c")
    d1 = w.queue("proj-d")
    w.sched.register_hold(str(w.folder("proj-d")), "session-1", proctree.ident_of(os.getpid()))
    w.tick()
    w.tick()
    assert w.states()[a1.id] == runtime.RUNNING and w.states()[b1.id] == runtime.RUNNING  # two checkouts at once
    view = w.sched.queue_view()
    assert view[c1.id][1]["code"] == "no-slot" and "2 of 2" in view[c1.id][1]["detail"]
    assert view[a2.id][1]["code"] == "checkout-busy"
    assert view[d1.id][1]["code"] == "held"
    w.tick()
    assert w.states()[c1.id] == runtime.QUEUED  # no third run starts
    w.set_parallel(1)  # lowering the bound never stops a live run and starts nothing new
    w.tick()
    assert w.states()[a1.id] == runtime.RUNNING and w.states()[b1.id] == runtime.RUNNING
    assert w.states()[c1.id] == runtime.QUEUED
    assert w.sched.queue_view()[c1.id][1]["code"] == "no-slot"
    assert "2 of 1" in w.sched.queue_view()[c1.id][1]["detail"]


def test_an_entry_behind_an_earlier_one_of_its_checkout_says_queued_behind(world):
    w = world(behavior="hang", max_parallel=2)
    first, second = w.queue("proj-a", "do-it"), w.queue("proj-a", "later-one")
    view = w.sched.queue_view()
    assert view[first.id] == (1, None)
    assert view[second.id][0] == 2 and view[second.id][1]["code"] == "queued-behind"


def test_a_stuck_head_does_not_starve_the_other_checkouts(world, monkeypatch):
    """One start per tick must not turn into one STUCK entry per tick: an attempt that leaves its entry queued moves on."""
    w = world(behavior="hang", max_parallel=2)
    a = w.queue("proj-a")
    b = w.queue("proj-b")
    real = w.sched._try_start

    def try_start(run, config):
        if run.id == a.id:
            return None  # stays queued, no state change (a non-blocking reason)
        return real(run, config)

    monkeypatch.setattr(w.sched, "_try_start", try_start)
    w.tick()
    assert w.states()[a.id] == runtime.QUEUED and w.states()[b.id] == runtime.RUNNING


def test_queue_refuses_to_block_a_task_while_a_dashboard_run_works_it(world):
    """R8. RED: restore the held-only check in ``_block_on_queue`` and the task is blocked under the live run."""
    items = [{"id": "do-it", "title": "Do it", "state": "available", "priority": "P1"},
             {"id": "no-section", "title": "Has no plan section", "state": "available", "priority": "P2"}]
    w = world(names=("proj-a",), behavior="slow-done", sleep_s=3.0, items=items, sections={"do-it": "Make it.\n\nDone."})
    first = w.queue("proj-a", "do-it")
    w.tick()
    assert w.states()[first.id] == runtime.RUNNING
    before = (w.folder("proj-a") / "plan.md").read_text(encoding="utf-8")
    with pytest.raises(QueueRefused) as refused:
        w.queue("proj-a", "no-section")
    exc = refused.value
    assert (exc.status, exc.code, exc.blocked) == (422, "translation-failed", False)
    assert "dashboard run is working it" in str(exc) and "section-" in str(exc)
    assert mutation.stored_status((w.folder("proj-a") / "task.yaml").read_text(encoding="utf-8")) == "active"
    assert (w.folder("proj-a") / "plan.md").read_text(encoding="utf-8") == before  # the item is still available, untouched
    assert {i["id"]: i["state"] for i in mutation.raw_items(before)}["no-section"] == "available"
    assert not [r for r in w.sched.db.runs() if r.item_id == "no-section"]  # and nothing was queued for it


def test_a_start_that_cannot_translate_stays_queued_while_another_run_works_the_folder(world):
    """The start-time twin of R8: the same folder is live under another checkout's run, so no block is written.

    RED: restore the held-only check in ``_try_start`` and the entry is blocked under that run.
    """
    items = [{"id": "no-section", "title": "Has no plan section", "state": "available", "priority": "P2"}]
    w = world(names=("proj-a",), behavior="hang", items=items, sections={})
    project, folder = w.project("proj-a"), w.folder("proj-a")
    # a run of ANOTHER checkout (another lease) that works this very folder, as two checkouts do through one linked tasks repo
    other = w.sched.db.enqueue(run_id="other", project_id="other-checkout", project_root=str(project), task_ref=TASK,
                               item_id="elsewhere", item_title="Elsewhere", options={}, source_revision="x")
    w.sched.db.set_state(other.id, runtime.RUNNING)
    mine = w.sched.db.enqueue(run_id="mine", project_id=project_id_for(project), project_root=str(project), task_ref=TASK,
                              item_id="no-section", item_title="Has no plan section", options={"timeout_s": 60},
                              source_revision=mutation.folder_revision(folder))
    w.tick()
    assert w.states()[mine.id] == runtime.QUEUED
    assert mutation.stored_status((folder / "task.yaml").read_text(encoding="utf-8")) == "active"
    assert "blocked" not in (folder / "plan.md").read_text(encoding="utf-8")


def test_a_job_file_the_job_runner_refuses_blocks_the_task_for_the_run_that_owns_it(world):
    """Round-3 regression. After ``try_lease`` the run is STARTING, so its own run-active state must not refuse its block.

    RED: call ``_refuse_if_live(folder)`` without excluding the run itself and the task stays active.
    """
    w = world(names=("proj-a",), behavior="done", edge_kw={"invalid_job_file": "the job runner refused this job"})
    run = w.queue("proj-a")
    final = w.tick_until(run.id, runtime.TERMINAL_STATES)
    assert final.state == runtime.BLOCKED and final.code == "job-file-invalid"
    folder = w.folder("proj-a")
    assert mutation.stored_status((folder / "task.yaml").read_text(encoding="utf-8")) == "blocked"
    assert {i["id"]: i["state"] for i in mutation.raw_items((folder / "plan.md").read_text(encoding="utf-8"))}["do-it"] == "blocked-user"


def test_a_job_file_block_is_still_refused_when_ANOTHER_run_works_the_folder(world):
    w = world(names=("proj-a",), behavior="done", edge_kw={"invalid_job_file": "the job runner refused this job"})
    project, folder = w.project("proj-a"), w.folder("proj-a")
    other = w.sched.db.enqueue(run_id="other", project_id="other-checkout", project_root=str(project), task_ref=TASK,
                               item_id="elsewhere", item_title="Elsewhere", options={}, source_revision="x")
    w.sched.db.set_state(other.id, runtime.RUNNING)
    run = w.queue("proj-a")
    final = w.tick_until(run.id, runtime.TERMINAL_STATES)
    assert final.state == runtime.FAILED and final.code == "job-file-invalid" and "The task was not blocked" in final.detail
    assert mutation.stored_status((folder / "task.yaml").read_text(encoding="utf-8")) == "active"


def test_waiting_reasons_name_llm_kit_and_a_bad_configuration(world):
    w = world(names=("proj-a",), behavior="hang")
    run = w.sched.db.enqueue(run_id="q1", project_id="proj-a-id", project_root=str(w.project("proj-a")), task_ref=TASK,
                             item_id="do-it", item_title="Do it", options={}, source_revision="x")
    assert w.sched.queue_view()[run.id] == (1, None)
    w.edge.state, w.edge.detail = "absent", "llm-kit is not installed"
    w.edge._cached = None
    assert w.sched.queue_view()[run.id][1] == {"code": "llm-kit-unavailable", "detail": "llm-kit is not installed"}
    w.edge.state = "ok"
    w.config.write_text("dashboard:\n  surprise: 1\n", encoding="utf-8")
    code = w.sched.queue_view()[run.id][1]["code"]
    assert code == "config"
