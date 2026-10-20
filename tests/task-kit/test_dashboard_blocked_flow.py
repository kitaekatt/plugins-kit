"""One autonomous vertical slice, end to end, with a fake agent.

Every test here drives the REAL dashboard server, scheduler, runner process,
acceptance contract and task-folder mutation. Only the model is fake
(tests/task-kit/fake_llm.py, behind a FakeEdge): it edits the task folder like a
diligent agent, then runs the job's real contract command with its answer on
stdin. The blocked flow is: translation or the worker blocks -> the task folder
says so -> the user resolves it in the harness -> Re-check -> a FRESH run.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest
import yaml
from dashboard_support import (
    FakeEdge,
    Running,
    dashboard_world,
    git,
    runner_environment,
)

from task_system.dashboard import mutation, runtime, scheduler
from task_system.dashboard.inventory import project_id_for

TASK = "dev/tasks/alpha"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "no-config.yaml")
    rig = Rig(srv, edge, devroot, project, folder, tmp_path)
    try:
        yield rig
    finally:
        srv.stop()


class Rig:
    def __init__(self, srv, edge, devroot, project, folder, tmp_path):
        self.srv, self.edge, self.devroot, self.project, self.folder, self.tmp_path = srv, edge, devroot, project, folder, tmp_path
        self.pid = project_id_for(project)

    def item_path(self, item, action, task=TASK):
        return f"/api/v1/items/{self.pid}/{urllib.parse.quote(task, safe='')}/{item}/{action}"

    def revision(self):
        return mutation.folder_revision(self.folder)

    def queue(self, item="do-it", options=None, revision=None):
        body = {"expected_revision": revision or self.revision()}
        if options is not None:
            body["options"] = options
        return self.srv.post(self.item_path(item, "queue"), body)

    def run_to_end(self, item="do-it", **kw):
        status, _h, body = self.queue(item, **kw)
        assert status == 202, body
        return self.srv.settle(body["run"]["id"])

    def plan(self):
        return (self.folder / "plan.md").read_text(encoding="utf-8")

    def task_status(self):
        return mutation.stored_status((self.folder / "task.yaml").read_text(encoding="utf-8"))

    def items(self):
        return {i["id"]: i for i in mutation.raw_items(self.plan())}

    def log(self):
        return (self.folder / "log.md").read_text(encoding="utf-8")

    def receipt(self, run):
        return json.loads((self.run_dir(run) / "receipt.json").read_text(encoding="utf-8"))

    def run_dir(self, run):
        from pathlib import Path

        return Path(run.run_dir)


# -- the happy path ----------------------------------------------------------


def test_a_done_run_is_accepted_without_the_dashboard_touching_git_or_the_siblings(rig):
    head = git(rig.project, "rev-parse", "HEAD")
    run = rig.run_to_end()
    assert (run.state, run.code) == (runtime.ACCEPTED, "accepted")
    assert "do-it" not in rig.items() and "later-one" in rig.items()  # completion is removal; siblings stay
    assert rig.task_status() == "active"
    assert git(rig.project, "rev-parse", "HEAD") == head  # the dashboard commits nothing
    assert git(rig.project, "diff", "--cached", "--name-only") == ""  # and stages nothing
    receipt = rig.receipt(run)
    assert receipt["verdict"]["outcome"] == "accepted" and receipt["start"]["head"] == head
    assert receipt["end"]["head"] == head and receipt["result"]["status"] == "done"
    events = [e["kind"] for e in rig.srv.scheduler.db.events(run.id)]
    assert events[:2] == ["queued", "starting"] and "running" in events and events[-1] == "accepted"


def test_the_run_gets_one_flat_job_on_the_machine_defaults_and_a_ledger_path(rig):
    run = rig.run_to_end()
    document = yaml.safe_load((rig.run_dir(run) / "job.yaml").read_text(encoding="utf-8"))
    (job,) = document["jobs"]
    assert document["max_parallel"] == 1 and "disallowed_tools" not in document
    assert job["models"] == ["opus"] and job["max_attempts"] == 1 and job["workspace"] == {"isolate": False}
    assert job["id"].endswith(run.id) and job["directory"] == str(rig.project)
    spec = json.loads((rig.run_dir(run) / "spec.json").read_text(encoding="utf-8"))
    assert spec["runner"]["argv"][-1] == run.id and spec["runner"]["argv"][-2].endswith("ledger.sqlite3")
    assert job["contract"]["command"][0] == spec["runner"]["argv"][0]  # the runner's own absolute interpreter


# -- the worker blocks -------------------------------------------------------


def test_a_blocked_result_blocks_the_task_and_item_and_the_ledger_rejects(rig):
    rig.edge.behavior = "blocked"
    run = rig.run_to_end()
    assert (run.state, run.code) == (runtime.BLOCKED, "agent-blocked")
    assert rig.task_status() == "blocked"
    item = rig.items()["do-it"]
    assert item["state"] == "blocked-user" and "decision" in item["note"] and "two designs fit" in item["note"]
    assert "dashboard run " + run.id in rig.log() and "worker reported blocked" in rig.log()
    receipt = rig.receipt(run)
    assert receipt["verdict"]["outcome"] == "agent-blocked" and receipt["block"]["item_mutated"] is True
    snapshot = json.loads((rig.run_dir(run) / "llm.out").read_text(encoding="utf-8").strip().splitlines()[-1])
    assert snapshot["jobs"][0]["state"] == "rejected"  # distinct nonzero exit -> the ledger rejects, never accepts
    status, _h, payload = rig.srv.json("GET", "/api/v1/dashboard")
    kinds = {(n["kind"], n["item"]) for n in payload["needs"]}
    assert ("item-blocked", "do-it") in kinds and ("task-blocked", None) in kinds


def test_resolving_in_the_harness_then_recheck_reactivates_and_the_next_run_is_fresh(rig):
    rig.edge.behavior = "blocked"
    first = rig.run_to_end()
    # Re-check is refused while the item is still blocked-user, and says why.
    status, _h, body = rig.srv.post(rig.item_path("do-it", "recheck"), {"expected_revision": rig.revision()})
    assert status == 422 and body["error"]["code"] == "recheck-failed" and rig.task_status() == "blocked"
    assert any(r["code"] == "item-blocked" for r in body["error"]["reasons"])
    # The user fixes it in the harness (a plain edit; no dashboard involved).
    plan = rig.plan().replace("state: blocked-user", "state: available")
    plan = "\n".join(line for line in plan.splitlines() if not line.strip().startswith("note:")) + "\n"
    (rig.folder / "plan.md").write_text(plan, encoding="utf-8")
    status, _h, body = rig.srv.post(rig.item_path("do-it", "recheck"), {"expected_revision": rig.revision()})
    assert status == 200 and body["changed"] is True and rig.task_status() == "active"
    assert "re-check cleared the block" in rig.log()
    # A later Queue is a NEW run with its own directory and ledger.
    rig.edge.behavior = "done"
    second = rig.run_to_end()
    assert second.state == runtime.ACCEPTED and second.id != first.id and second.run_dir != first.run_dir


# -- the worker is rejected --------------------------------------------------


@pytest.mark.parametrize("behavior,code", [
    ("no-result", "result-missing"),
    ("duplicate-result", "result-duplicate"),
    ("done-no-removal", "item-still-present"),
    ("branch-switch", "branch-changed"),
])
def test_an_unacceptable_run_is_rejected_and_blocks_the_task(rig, behavior, code):
    rig.edge.behavior = behavior
    run = rig.run_to_end()
    assert (run.state, run.code) == (runtime.BLOCKED, code)
    assert rig.task_status() == "blocked"
    assert rig.receipt(run)["verdict"]["outcome"] == "rejected"
    if behavior == "branch-switch":
        # The worker had already removed its item, so only the task can be blocked, and the log says so.
        assert "do-it" not in rig.items() and "could not be marked blocked" in rig.log()
        assert rig.receipt(run)["block"]["item_mutated"] is False
        assert git(rig.project, "rev-parse", "--abbrev-ref", "HEAD") == "other"  # the host never repairs a breach
    else:
        assert rig.items()["do-it"]["state"] == "blocked-user" and rig.items()["do-it"]["note"].startswith(code)


def test_an_intervening_commit_is_observed_not_attributed(rig):
    rig.edge.behavior = "commit"
    start = git(rig.project, "rev-parse", "HEAD")
    run = rig.run_to_end()
    assert run.state == runtime.ACCEPTED  # the start HEAD is still an ancestor of the end HEAD
    receipt = rig.receipt(run)
    assert receipt["start"]["head"] == start and receipt["end"]["head"] != start


def test_a_run_that_ends_without_a_verdict_is_failed_not_blocked_and_retry_is_fresh(rig):
    rig.edge.behavior = "exit-1"
    failed = rig.run_to_end()
    assert (failed.state, failed.code) == (runtime.FAILED, "llm-kit-exit-1")
    assert rig.task_status() == "active" and rig.items()["do-it"]["state"] == "available"
    status, _h, payload = rig.srv.json("GET", "/api/v1/dashboard")
    assert any(r["id"] == failed.id and r["retryable"] for r in payload["recent_runs"])
    # Retry only creates a new queue entry; nothing is replayed.
    rig.edge.behavior = "done"
    status, _h, body = rig.srv.post(f"/api/v1/runs/{failed.id}/retry", {"expected_revision": rig.revision()})
    assert status == 202
    fresh = rig.srv.settle(body["run"]["id"])
    assert fresh.state == runtime.ACCEPTED and fresh.retry_of == failed.id and fresh.run_dir != failed.run_dir
    # An accepted run is not retryable; neither is a blocked one.
    status, _h, body = rig.srv.post(f"/api/v1/runs/{fresh.id}/retry", {"expected_revision": rig.revision()})
    assert status == 409 and body["error"]["code"] == "not-retryable"


# -- translation failures block with no trace of a job -----------------------


def test_a_missing_plan_section_blocks_the_task_and_creates_no_job_run_or_ledger(tmp_path, monkeypatch):
    """RED CHECK: translation fails loudly -> the task is shown blocked, NO job/ledger exists."""
    runner_environment(monkeypatch)
    devroot, project, folder = dashboard_world(tmp_path, sections={})  # do-it has no section
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")
    try:
        rig = Rig(srv, edge, devroot, project, folder, tmp_path)
        status, _h, body = rig.queue()
        assert status == 422 and body["error"]["code"] == "translation-failed" and body["error"]["blocked"] is True
        assert any(r["code"] == "section-missing" for r in body["error"]["reasons"])
        assert rig.task_status() == "blocked" and rig.items()["do-it"]["state"] == "blocked-user"
        assert "section-missing" in rig.items()["do-it"]["note"]
        assert srv.scheduler.db.runs() == []  # no run row
        assert edge.job_files == []  # llm-kit never saw a job file
        assert not (srv.server.root / "projects").exists()  # no run directory, no ledger
        srv.scheduler.tick()
        assert not (srv.server.root / "projects").exists()
    finally:
        srv.stop()


def test_a_malformed_item_source_still_blocks_the_task_and_says_the_item_was_not_marked(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    devroot, project, folder = dashboard_world(tmp_path)
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "none.yaml")
    try:
        rig = Rig(srv, edge, devroot, project, folder, tmp_path)
        # A duplicated id is malformed source: the item cannot be edited safely.
        plan = rig.plan().replace("- id: later-one", "- id: do-it")
        (folder / "plan.md").write_text(plan, encoding="utf-8")
        status, _h, body = rig.queue()
        assert status == 422 and body["error"]["blocked"] is True
        assert rig.task_status() == "blocked"
        assert "could not be marked blocked because its source was invalid" in rig.log()
        assert [i["state"] for i in mutation.raw_items(rig.plan())] == ["available", "available"]  # untouched
    finally:
        srv.stop()


def test_stale_input_is_a_409_with_the_current_revision_and_changes_nothing(rig):
    old = rig.revision()
    with open(rig.folder / "log.md", "a", encoding="utf-8") as handle:
        handle.write("- 2026-10-03 08:00: the harness changed something\n")
    status, _h, body = rig.queue(revision=old)
    assert status == 409 and body["error"]["code"] == "stale-revision"
    assert body["error"]["current_revision"] == rig.revision()
    assert rig.srv.scheduler.db.runs() == []


def test_every_change_must_name_its_expected_revision(rig):
    status, _h, body = rig.srv.post(rig.item_path("do-it", "queue"), {})
    assert status == 422 and body["error"]["code"] == "missing_expected_revision"


def test_a_second_queue_of_the_same_item_is_refused(rig):
    rig.edge.behavior = "hang"  # stays live so the duplicate arrives while it is queued or running
    status, _h, body = rig.queue(options={"timeout_s": 2})
    assert status == 202
    status, _h, again = rig.queue()
    assert status == 409 and again["error"]["code"] == "already-queued"
    assert rig.srv.settle(body["run"]["id"]).state == runtime.TIMEOUT  # and nothing outlives the test


def test_an_item_that_is_not_available_is_refused_without_blocking_anything(rig):
    plan = rig.plan().replace("state: available\n    priority: P2", "state: deferred\n    priority: P2")
    assert plan != rig.plan()
    (rig.folder / "plan.md").write_text(plan, encoding="utf-8")
    status, _h, body = rig.queue("later-one")
    assert status == 422 and body["error"]["code"] == "not-ready" and "blocked" not in body["error"]
    assert rig.task_status() == "active"


def test_an_item_that_vanishes_before_the_run_starts_is_dropped_not_blocked(rig):
    rig.edge.state = "absent"  # llm-kit disappears: the entry cannot start...
    # ...so queue directly (the API would refuse) to model an entry that was queued earlier.
    rig.edge.state = "ok"
    run = rig.srv.scheduler.db.enqueue(
        run_id="gone", project_id=rig.pid, project_root=str(rig.project), task_ref=TASK, item_id="not-an-item",
        item_title="Gone", options={}, source_revision=rig.revision())
    rig.srv.scheduler.tick()
    dropped = rig.srv.scheduler.db.get(run.id)
    assert (dropped.state, dropped.code) == (runtime.SKIPPED, "task-source-changed")
    assert rig.task_status() == "active"


# -- reading runs ------------------------------------------------------------


def test_the_run_log_is_a_bounded_redacted_cursor_tail_and_needs_the_token(rig):
    run = rig.run_to_end()
    status, _h, first = rig.srv.json("GET", f"/api/v1/runs/{run.id}/log?after=0")
    assert status == 200 and "runner: starting" in first["text"] and first["cursor"] > 0
    status, _h, second = rig.srv.json("GET", f"/api/v1/runs/{run.id}/log?after={first['cursor']}")
    assert second["text"] == "" and second["cursor"] == first["cursor"]
    assert rig.srv.request("GET", f"/api/v1/runs/{run.id}/log", token=False)[0] == 401
    assert rig.srv.json("GET", f"/api/v1/runs/{run.id}/log?after=x")[0] == 422
    status, _h, detail = rig.srv.json("GET", f"/api/v1/runs/{run.id}")
    assert status == 200 and detail["run"]["state"] == "accepted" and detail["events"]


def test_the_dashboard_payload_lists_runs_and_attaches_the_latest_to_each_item(rig):
    rig.edge.behavior = "exit-1"
    failed = rig.run_to_end()
    status, _h, payload = rig.srv.json("GET", "/api/v1/dashboard")
    assert payload["capabilities"]["run"]["available"] is True
    assert payload["capabilities"]["mutations"]["actions"] == ["queue", "retry", "recheck", "cancel", "newtask", "additem", "defer", "undefer", "queueall", "remove", "runoptions"]
    assert payload["runs"] == [] and [r["id"] for r in payload["recent_runs"]] == [failed.id]
    item = next(i for t in payload["tasks"] for i in t["items"] if i["id"] == "do-it")
    assert item["run"]["id"] == failed.id and item["run"]["state"] == "failed"
    task = next(t for t in payload["tasks"] if t["id"] == TASK)
    assert task["revision"] == rig.revision()
