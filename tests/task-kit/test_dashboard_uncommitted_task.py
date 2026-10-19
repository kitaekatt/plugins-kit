"""The uncommitted-dev/tasks-folder warning never gates dashboard work.

A run never commits, so the worker's own edits leave a git-tracked dev/tasks
folder dirty by design; the dashboard's block and Re-check writes do the same.
``task validate`` warns about that (advice for the harness). The dashboard must
disregard exactly that warning when it gates, and show it as information.
"""

from __future__ import annotations

import pytest
from dashboard_support import FakeEdge, Running, dashboard_world, git, runner_environment
from test_dashboard_blocked_flow import Rig

from task_system.dashboard import findings, runtime, scheduler, snapshot
from task_system.dashboard.inventory import Inventory
from task_system.validate import UNCOMMITTED_DEV_TASKS, validate_ref


def track_everything(project) -> None:
    """A git repository whose dev/tasks folders are TRACKED (committed), the case that warns when dirty."""
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "test@example.invalid")
    git(project, "config", "user.name", "Test")
    git(project, "config", "commit.gpgsign", "false")
    (project / "README.md").write_text("project\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")


@pytest.fixture
def tracked(tmp_path, monkeypatch):
    runner_environment(monkeypatch)
    monkeypatch.setattr(scheduler, "RUNNER_SLACK_S", 1.0)
    devroot, project, folder = dashboard_world(tmp_path, repo=False)
    track_everything(project)
    edge = FakeEdge()
    srv = Running(tmp_path / "data", (devroot,), edge=edge, config_path=tmp_path / "no-config.yaml")
    rig = Rig(srv, edge, devroot, project, folder, tmp_path)
    try:
        yield rig
    finally:
        srv.stop()


def uncommitted(rig) -> list[str]:
    return [w for w in validate_ref("dev/tasks/alpha", rig.project).warnings if w.startswith(UNCOMMITTED_DEV_TASKS)]


# -- identification --------------------------------------------------------------


def test_the_warning_is_identified_by_the_clause_validate_exports(tracked):
    """PIN: this fails if validate changes the warning so the dashboard's filter would silently match nothing."""
    assert uncommitted(tracked) == []  # committed and clean: no warning
    (tracked.folder / "log.md").write_text("- 2026-10-02 10:00: edited\n", encoding="utf-8")
    result = validate_ref("dev/tasks/alpha", tracked.project)
    produced = [w for w in result.warnings if "unsaved durable work" in w]
    assert len(produced) == 1 and produced[0].startswith(UNCOMMITTED_DEV_TASKS)
    assert findings.informational(result.warnings) == produced
    assert findings.gating_of(result) == [] and result.errors == []


def test_every_other_finding_still_gates():
    assert findings.gating(["a different warning", f"{UNCOMMITTED_DEV_TASKS}: x has unsaved durable work",
                            "something about an uncommitted dev/tasks folder, but not leading"]) == [
        "a different warning", "something about an uncommitted dev/tasks folder, but not leading"]


# -- a run on a git-tracked task is accepted -------------------------------------


def test_a_run_on_a_git_tracked_task_is_accepted_though_the_worker_left_it_uncommitted(tracked):
    head = git(tracked.project, "rev-parse", "HEAD")
    assert uncommitted(tracked) == []
    run = tracked.run_to_end()
    assert (run.state, run.code) == (runtime.ACCEPTED, "accepted"), (run.code, run.detail)
    assert "do-it" not in tracked.items() and "completed do-it" in tracked.log()
    assert len(uncommitted(tracked)) == 1  # the folder IS dirty: the warning is real, and was disregarded
    assert git(tracked.project, "rev-parse", "HEAD") == head  # and the run committed nothing


def test_after_a_block_and_a_recheck_the_item_is_still_queueable(tracked):
    tracked.edge.behavior = "blocked"
    first = tracked.run_to_end()
    assert (first.state, first.code) == (runtime.BLOCKED, "agent-blocked") and tracked.task_status() == "blocked"
    assert len(uncommitted(tracked)) == 1  # the dashboard's own block write dirtied the folder
    plan = tracked.plan().replace("state: blocked-user", "state: available")
    plan = "\n".join(line for line in plan.splitlines() if not line.strip().startswith("note:")) + "\n"
    (tracked.folder / "plan.md").write_text(plan, encoding="utf-8")
    status, _h, body = tracked.srv.post(tracked.item_path("do-it", "recheck"), {"expected_revision": tracked.revision()})
    assert status == 200 and body["changed"] is True and tracked.task_status() == "active", body  # Re-check is not refused for it
    assert len(uncommitted(tracked)) == 1  # and its own write left the folder dirty again
    tracked.edge.behavior = "done"
    second = tracked.run_to_end()  # Queue is not refused by the queueability gate
    assert second.state == runtime.ACCEPTED and second.id != first.id


# -- shown as information, never a blocker ---------------------------------------


def test_the_warning_is_information_in_the_payload_not_a_finding_or_a_need(tracked):
    (tracked.folder / "log.md").write_text("- 2026-10-02 10:00: edited\n", encoding="utf-8")
    inventory = Inventory(parents=(tracked.devroot,), min_interval=0).snapshot()
    payload = snapshot.dashboard_payload(inventory)
    task = next(t for t in payload["tasks"] if t["id"] == "dev/tasks/alpha")
    assert task["findings"]["warnings"] == [] and task["findings"]["errors"] == []
    assert len(task["findings"]["info"]) == 1 and task["findings"]["info"][0].startswith(UNCOMMITTED_DEV_TASKS)
    assert task["needs"] == [] and payload["totals"]["needs"] == 0
