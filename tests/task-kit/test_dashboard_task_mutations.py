"""Safe dashboard authoring: New task, Add item (and add-and-queue), Defer / Undefer, Queue all,
Remove from queue and run options. Every test drives the REAL server and scheduler with a fake llm-kit.

What is pinned here is what a person relies on: a write lands exactly what was asked in the files the
task system reads, an input rule refuses BEFORE a byte is written, and the task system itself (``task.py``)
treats a dashboard-authored folder like one made by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from authoring_support import make_rig, rig, teardown_rig  # noqa: F401 -- rig is a fixture
from dashboard_support import FakeEdge, init_repo, make_task, run_task_cli, tree_digest

from task_system.dashboard import mutation, runtime, translate
from task_system.validate import validate_ref


def findings(project: Path, task: str) -> tuple:
    result = validate_ref(f"dev/tasks/{task}", project)
    return (result.classification, tuple(result.errors), tuple(result.warnings))


def item_map(plan: str) -> dict:
    return {i["id"]: i for i in mutation.raw_items(plan)}


def log_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


# -- Add item ------------------------------------------------------------------------------------


def test_add_item_writes_the_block_the_section_in_its_region_and_one_log_line(rig):
    """RED CHECK: skip the section insertion and the item has no instructions for a run."""
    plan = rig.plan() + "\n## Notes\n\nlater notes\n"
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    before_findings = findings(rig.project, "alpha")
    log_before = log_lines(rig.log())
    status, _h, body = rig.add(title="Write the docs", priority="P1", details="Explain it.\n\nDone when it reads well.")
    assert status == 201, body
    assert body["item"] == {"id": "write-the-docs", "title": "Write the docs", "state": "available", "priority": "P1"}
    assert body["revision"] == rig.rev() and body["task"]["revision"] == rig.rev()
    new_plan = rig.plan()
    assert item_map(new_plan)["write-the-docs"] == {"id": "write-the-docs", "title": "Write the docs", "state": "available", "priority": "P1"}
    # the section sits at the end of the region holding the block, before the next ## heading
    heading = new_plan.index("### write-the-docs -- Write the docs")
    assert new_plan.index("### later-one") < heading < new_plan.index("## Notes")
    assert "Explain it.\n\nDone when it reads well.\n" in new_plan
    lines = log_lines(rig.log())
    assert lines[:-1] == log_before and "dashboard: added item write-the-docs (Write the docs)" in lines[-1]
    # task.py treats the folder like any other: the item is listed, nothing new is wrong with it
    items = run_task_cli(["items", "dev/tasks/alpha", "--root", str(rig.project)], rig.project)
    assert items.returncode == 0 and b"write-the-docs" in items.stdout
    assert findings(rig.project, "alpha") == before_findings
    # and it is ready to run: the same translation a run starts from accepts it
    status, _h, queued = rig.queue("write-the-docs")
    assert status == 202, queued


def test_ids_come_from_the_title_and_never_collide(rig):
    plan = rig.plan() + "\n#### shared-id -- an unrelated heading\n\ntext\n"
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    ids = []
    for title in ("Do it", "Do it", "Shared id", "  Mixed CASE & Symbols!  "):
        status, _h, body = rig.add(title=title)
        assert status == 201, body
        ids.append(body["item"]["id"])
    assert ids == ["do-it-2", "do-it-3", "shared-id-2", "mixed-case-symbols"]
    status, _h, body = rig.add(title="x" * 100)
    assert status == 201 and body["item"]["id"] == "x" * 48


@pytest.mark.parametrize("body,code", [
    ({"title": ""}, "bad-title"),
    ({"title": "   "}, "bad-title"),
    ({"title": "two\nlines"}, "bad-title"),
    ({"title": "!!!"}, "bad-title"),
    ({"title": 7}, "bad-title"),
    ({"title": "ok", "priority": "P9"}, "bad-priority"),
    ({"title": "ok", "priority": "high"}, "bad-priority"),
    ({"title": "ok", "details": "# top heading"}, "bad-details"),
    ({"title": "ok", "details": "text\n## second level"}, "bad-details"),
    ({"title": "ok", "details": "### third level"}, "bad-details"),
    ({"title": "ok", "details": "```python\nnever closed"}, "bad-details"),
    ({"title": "ok", "queue": True}, "details-required"),
])
def test_every_input_refusal_writes_nothing(rig, body, code):
    digest = tree_digest(rig.project / "dev")
    status, _h, answer = rig.add(**body)
    assert status == 422 and answer["error"]["code"] == code, answer
    assert tree_digest(rig.project / "dev") == digest


def test_headings_inside_a_fence_and_deeper_headings_are_allowed(rig):
    details = "Steps:\n\n```md\n# not a heading here\n```\n\n#### Deeper\n\nfine"
    status, _h, body = rig.add(title="Fenced", details=details)
    assert status == 201, body
    section = rig.plan().split("### fenced -- Fenced")[1]
    assert "# not a heading here" in section and "#### Deeper" in section
    assert findings(rig.project, "alpha")[1] == ()


def test_unknown_fields_a_closed_task_and_a_missing_block_are_refused_with_nothing_written(rig):
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.srv.send("POST", rig.task_path("alpha", "items"),
                                    {"expected_revision": rig.rev(), "title": "ok", "dest": "tmp"})
    assert status == 422 and body["error"]["code"] == "unknown_field"
    (rig.alpha / "task.yaml").write_text((rig.alpha / "task.yaml").read_text().replace("status: active", "status: closed"), encoding="utf-8")
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.add(title="ok")
    assert status == 422 and body["error"]["code"] == "task-not-open"
    (rig.alpha / "task.yaml").write_text((rig.alpha / "task.yaml").read_text().replace("status: closed", "status: active"), encoding="utf-8")
    (rig.alpha / "plan.md").write_text("# Plan\n\nno block here\n", encoding="utf-8")
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.add(title="ok")
    assert status == 422 and body["error"]["code"] == "no-items-block"
    assert tree_digest(rig.project / "dev") == digest


def test_the_writer_and_the_reader_agree_on_which_fence_is_the_block(rig):
    """RED CHECK: give the writer back its private fence pattern and it edits a block the reader does not see."""
    plan = rig.plan().replace("```yaml\n", "```yaml title=hidden\n", 1)
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    from task_system.task_items import read_task_items
    from task_system.types import HAND_OFF

    assert read_task_items(rig.alpha, HAND_OFF).block_found is False  # the reader does not see this fence
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.add(title="Sneaky")
    assert status == 422 and body["error"]["code"] == "no-items-block", body
    assert tree_digest(rig.project / "dev") == digest


def test_a_long_title_stays_on_one_line_in_the_block_and_task_yaml_matches_the_task_systems_bytes(rig, tmp_path):
    """RED CHECK: drop the width from the block dump (the block wraps), or give task.yaml the wide dump (its bytes differ)."""
    long_title = " ".join(["word"] * 50)  # 249 characters, plenty of break points
    status, _h, body = rig.add(title=long_title)
    assert status == 201, body
    assert f"\n    title: {long_title}\n" in rig.plan()
    # a block recorded for the same item keeps both lines whole
    from task_system.dashboard import blocker

    blocker.record_block(mutation.TaskMutator(rig.tmp_path / "x.lock"), rig.alpha, item_id=body["item"]["id"],
                         note="n " * 300, log_detail="blocked for the test")
    block = rig.plan().splitlines()
    assert f"    title: {long_title}" in block
    note_lines = [line for line in block if line.startswith("    note: ")]
    assert len(note_lines) == 1 and note_lines[0].endswith("...") and len(note_lines[0]) > 300
    # task.yaml: the dashboard's helpers and the task system's own `update` write the same bytes
    twin = tmp_path / "twin"
    init_repo(twin)
    make_task(twin, "dev/tasks", "alpha", title=long_title, priority=None)
    mine = tmp_path / "mine"
    init_repo(mine)
    make_task(mine, "dev/tasks", "alpha", title=long_title, priority=None)
    cli = run_task_cli(["update", "dev/tasks/alpha", "--status", "blocked", "--priority", "P1", "--root", str(twin)], twin)
    assert cli.returncode == 0, cli.stderr
    mutation.TaskMutator(tmp_path / "y.lock").mutate(
        mine / "dev/tasks/alpha",
        lambda t: {mutation.TASK_YAML: mutation.set_task_fields(mutation.set_task_status(t.task_yaml, "blocked"), priority="P1")})
    assert (mine / "dev/tasks/alpha/task.yaml").read_bytes() == (twin / "dev/tasks/alpha/task.yaml").read_bytes()


# -- add and queue ---------------------------------------------------------------------------------


def test_add_and_queue_enqueues_the_new_item(rig):
    status, _h, body = rig.add(title="Ship it", details="Ship the thing.", queue=True, options={"timeout_s": 30})
    assert status == 202, body
    assert body["run"]["item"] == "ship-it" and body["run"]["state"] == "queued" and body["run"]["timeout_s"] == 30
    assert "queue_error" not in body
    assert rig.srv.settle(body["run"]["id"]).state == runtime.ACCEPTED


def test_add_and_queue_without_llm_kit_is_503_and_writes_nothing(tmp_path, monkeypatch):
    built = make_rig(tmp_path, monkeypatch, edge=FakeEdge(state="absent"))
    try:
        digest = tree_digest(built.project / "dev")
        status, _h, body = built.add(title="Ship it", details="Ship the thing.", queue=True)
        assert status == 503 and body["error"]["code"] == "llm-kit-unavailable"
        assert tree_digest(built.project / "dev") == digest
        status, _h, body = built.add(title="Ship it", details="Ship the thing.")  # adding alone needs no llm-kit
        assert status == 201
    finally:
        teardown_rig(built)


def test_a_refused_queue_step_leaves_the_added_item_and_says_so(rig):
    (rig.alpha / "task.yaml").write_text((rig.alpha / "task.yaml").read_text().replace("status: active", "status: blocked"), encoding="utf-8")
    status, _h, body = rig.add(title="Ship it", details="Ship the thing.", queue=True)
    assert status == 201, body
    assert body["queue_error"]["code"] == "not-ready" and "run" not in body
    assert "ship-it" in item_map(rig.plan())


def test_bad_options_on_add_and_queue_refuse_before_anything_is_written(rig):
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.add(title="Ship it", details="Ship the thing.", queue=True, options={"worker": "codex"})
    assert status == 422 and body["error"]["code"] == "bad-options"
    assert tree_digest(rig.project / "dev") == digest


# -- Defer / Undefer -------------------------------------------------------------------------------


def test_defer_and_undefer_change_only_the_state_and_keep_the_note(rig):
    plan = rig.plan().replace("    state: available\n    priority: P2", "    state: available\n    priority: P2\n    note: keep me", 1)
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    status, _h, body = rig.defer("later-one", "deferred")
    assert status == 200 and body["item"]["state"] == "deferred" and body["revision"] == rig.rev()
    item = item_map(rig.plan())["later-one"]
    assert item["state"] == "deferred" and item["note"] == "keep me"
    assert "dashboard: deferred item later-one" in log_lines(rig.log())[-1]
    status, _h, body = rig.defer("later-one", "available")
    assert status == 200 and item_map(rig.plan())["later-one"]["state"] == "available"
    assert "dashboard: undeferred item later-one" in log_lines(rig.log())[-1]


@pytest.mark.parametrize("item,state", [("later-one", "available"), ("later-one", "blocked-user"), ("later-one", "in-flight"), ("ghost", "deferred")])
def test_any_other_transition_is_refused_and_writes_nothing(rig, item, state):
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.defer(item, state)
    assert status == 422, body
    assert tree_digest(rig.project / "dev") == digest


# -- New task --------------------------------------------------------------------------------------


def twin_task(tmp_path, title: str, *, priority: str | None = None) -> Path:
    twin = tmp_path / "twin-repo"
    init_repo(twin)
    created = run_task_cli(["init", title, "--dest", "dev/tasks", "--root", str(twin)], twin)
    assert created.returncode == 0, created.stderr
    folder = Path(created.stdout.decode().strip().splitlines()[-1])
    if priority:
        assert run_task_cli(["update", f"dev/tasks/{folder.name}", "--priority", priority, "--root", str(twin)], twin).returncode == 0
    return folder


def test_new_task_is_byte_identical_to_task_init_and_is_visible_at_once(tmp_path, monkeypatch):
    """RED CHECK: drop the inventory invalidation and the response (and the next read) cannot see the task."""
    built = make_rig(tmp_path, monkeypatch, min_interval=3600.0)
    try:
        built.dashboard()  # prime the inventory: a stale window now hides anything not invalidated
        status, _h, body = built.new_task(title="Fix the flaky build")
        assert status == 201, body
        assert body["path"] == "dev/tasks/fix-the-flaky-build" and body["task"] is not None
        assert body["task"]["title"] == "Fix the flaky build" and body["task"]["revision"] == body["revision"]
        folder = built.project / body["path"]
        twin = twin_task(tmp_path, "Fix the flaky build")
        for name in ("task.yaml", "plan.md", "log.md", "CLAUDE.md"):
            assert (folder / name).read_bytes() == (twin / name).read_bytes(), name
        assert any(t["id"] == body["path"] for t in built.dashboard()["tasks"])
        assert run_task_cli(["show", body["path"], "--root", str(built.project)], built.project).returncode == 0
    finally:
        teardown_rig(built)


def test_new_task_with_priority_and_a_first_item_matches_the_task_systems_own_edits(rig, tmp_path):
    status, _h, body = rig.new_task(title="Second thing", priority="P1",
                                    first_item={"title": "Start here", "details": "Begin."})
    assert status == 201 and "followup_error" not in body, body
    folder = rig.project / body["path"]
    twin = twin_task(tmp_path, "Second thing", priority="P1")
    assert (folder / "task.yaml").read_bytes() == (twin / "task.yaml").read_bytes()
    assert item_map((folder / "plan.md").read_text())["start-here"]["state"] == "available"
    assert "### start-here -- Start here" in (folder / "plan.md").read_text()
    assert "dashboard: created task with priority P1, first item start-here" in log_lines((folder / "log.md").read_text())[-1]
    assert findings(rig.project, "second-thing")[1] == ()


def test_new_task_refusals(rig, tmp_path):
    status, _h, body = rig.new_task(title="Fix it")
    assert status == 201
    status, _h, body = rig.new_task(title="Fix it")
    assert status == 409 and body["error"]["code"] == "task-exists" and body["error"]["current_revision"]
    for bad, code in (({"title": ""}, "bad-title"), ({"title": "x", "priority": "P7"}, "bad-priority"),
                      ({"title": "x", "first_item": {"title": ""}}, "bad-title"),
                      ({"title": "x", "first_item": {"title": "y", "details": "# no"}}, "bad-details"),
                      ({"title": "!!!"}, "bad-title")):
        digest = tree_digest(rig.project / "dev")
        status, _h, answer = rig.new_task(**bad)
        assert status == 422 and answer["error"]["code"] == code, (bad, answer)
        assert tree_digest(rig.project / "dev") == digest
    status, _h, answer = rig.new_task(title="x", dest="tmp")
    assert status == 422 and answer["error"]["code"] == "unknown_field"
    status, _h, answer = rig.srv.send("POST", "/api/v1/projects/nope/tasks", {"title": "x"})
    assert status == 404


def test_a_concurrent_creation_of_the_same_stub_is_a_409_not_a_500(rig, monkeypatch):
    from task_system import init as task_init

    def lose_the_race(title, root, **kw):
        (root / "dev/tasks/raced").mkdir(parents=True)
        raise FileExistsError(str(root / "dev/tasks/raced"))

    monkeypatch.setattr(task_init, "init_task", lose_the_race)
    status, _h, body = rig.new_task(title="Raced")
    assert status == 409 and body["error"]["code"] == "task-exists"


def test_new_task_is_refused_where_a_session_already_holds_the_target_path(rig):
    rig.hold(rig.project / "dev/tasks/fresh-one")
    status, _h, body = rig.new_task(title="Fresh one")
    assert status == 409 and body["error"]["code"] == "held"
    assert not (rig.project / "dev/tasks/fresh-one").exists()


def test_a_failed_followup_keeps_the_created_task_and_reports_it(rig, monkeypatch):
    real = rig.srv.scheduler._authoring_precommit

    def refuse(folder, **kw):
        from task_system.dashboard.scheduler import QueueRefused

        def check():
            raise QueueRefused(409, "held", "a session appeared")
        return check

    monkeypatch.setattr(rig.srv.scheduler, "_authoring_precommit", refuse)
    status, _h, body = rig.new_task(title="Half done", priority="P1")
    assert status == 201 and body["followup_error"]["code"] == "held", body
    assert (rig.project / "dev/tasks/half-done/task.yaml").is_file()
    assert findings(rig.project, "half-done")[1] == ()
    assert real  # (the original is untouched for other tests)


# -- Queue all -----------------------------------------------------------------------------------


def add_ready(rig, n: int):
    ids = []
    for i in range(n):
        status, _h, body = rig.add(title=f"Extra {i}", priority="P3", details=f"Do extra {i}.")
        assert status == 201, body
        ids.append(body["item"]["id"])
    return ids


def test_queue_all_enqueues_every_available_item_in_task_items_order(rig):
    add_ready(rig, 1)
    status, _h, body = rig.queue_all(options={"timeout_s": 30})
    assert status == 202, body
    assert [r["item"] for r in body["runs"]] == ["do-it", "later-one", "extra-0"]  # P1, P2, P3
    queued = [r for r in rig.dashboard()["runs"] if r["state"] == "queued"]
    assert sorted((r["queue_position"], r["item"]) for r in queued) == [(1, "do-it"), (2, "later-one"), (3, "extra-0")]


def test_queue_all_skips_items_that_already_have_a_run_and_refuses_when_nothing_is_left(rig):
    status, _h, first = rig.queue("do-it")
    assert status == 202
    status, _h, body = rig.queue_all()
    assert status == 202 and [r["item"] for r in body["runs"]] == ["later-one"]
    status, _h, body = rig.queue_all()
    assert status == 422 and body["error"]["code"] == "nothing-to-queue"


def test_queue_all_with_one_bad_item_blocks_once_and_enqueues_nothing(rig):
    plan = rig.plan().replace("### later-one -- section\n\nMake the other thing.", "")
    (rig.alpha / "plan.md").write_text(plan, encoding="utf-8")
    before_lines = log_lines(rig.log())
    status, _h, body = rig.queue_all()
    assert status == 422 and body["error"]["code"] == "translation-failed" and body["error"]["blocked"] is True, body
    assert rig.srv.scheduler.db.runs() == []  # nothing was enqueued, not even the good item
    assert mutation.stored_status((rig.alpha / "task.yaml").read_text()) == "blocked"
    items = item_map(rig.plan())
    assert items["later-one"]["state"] == "blocked-user" and items["do-it"]["state"] == "available"
    assert len(log_lines(rig.log())) == len(before_lines) + 1  # ONE combined block, one log line


def test_queue_all_edge_cases(tmp_path, monkeypatch):
    built = make_rig(tmp_path, monkeypatch, edge=FakeEdge(state="absent"))
    try:
        digest = tree_digest(built.project / "dev")
        assert built.queue_all()[0] == 503
        assert tree_digest(built.project / "dev") == digest
    finally:
        teardown_rig(built)


def test_queue_all_stale_revision_is_a_409_and_changes_nothing(rig):
    stale = rig.rev()
    status, _h, body = rig.add(title="Move it on")
    assert status == 201
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.queue_all(revision=stale)
    assert status == 409 and body["error"]["code"] == "stale-revision" and body["error"]["current_revision"] == rig.rev()
    assert tree_digest(rig.project / "dev") == digest and rig.srv.scheduler.db.runs() == []


# -- Remove from queue ----------------------------------------------------------------------------


def test_remove_ends_a_queued_entry_as_removed_and_the_item_can_be_queued_again(rig):
    session = rig.hold(rig.alpha)  # keeps the entry queued
    status, _h, body = rig.queue("do-it")
    run_id = body["run"]["id"]
    status, _h, removed = rig.remove(run_id)
    assert status == 200 and removed["run"]["state"] == "removed" and removed["run"]["retryable"] is False
    payload = rig.dashboard()
    assert run_id not in [r["id"] for r in payload["recent_runs"] + payload["runs"]]
    row = next(t for t in payload["tasks"] if t["id"] == "dev/tasks/alpha")
    assert next(i for i in row["items"] if i["id"] == "do-it")["run"] is None
    assert rig.queue("do-it")[0] == 202
    assert rig.srv.send("POST", f"/api/v1/runs/{run_id}/retry", {"expected_revision": rig.rev()})[0] == 409  # not retryable
    rig.end_session(session)


def test_remove_refuses_an_entry_that_has_started_and_leaves_it_alone(rig):
    rig.edge.behavior = "hang"
    status, _h, body = rig.queue("do-it", options={"timeout_s": 60})
    live = rig.srv.settle(body["run"]["id"], states=(runtime.RUNNING,))
    status, _h, answer = rig.remove(live.id)
    assert status == 409 and answer["error"]["code"] == "not-queued"
    assert rig.srv.scheduler.db.get(live.id).state == runtime.RUNNING
    assert rig.remove("nope")[0] == 404


# -- run options -----------------------------------------------------------------------------------


@pytest.mark.parametrize("options,code", [
    ({"worker": "codex"}, "bad-options"),
    ({"entry": "no-such-entry"}, "bad-options"),
    ({"effort": "ludicrous"}, "bad-options"),
    ({"timeout_s": -5}, "bad-options"),
    ({"worker": "gemini"}, "bad-options"),
    ({"surprise": 1}, "bad-options"),
])
def test_bad_run_options_are_refused_as_a_request_and_never_block_the_task(rig, options, code):
    """RED CHECK: validate through the old translation path and the task is blocked by a bad option."""
    digest = tree_digest(rig.project / "dev")
    status, _h, body = rig.queue("do-it", options=options)
    assert status == 422 and body["error"]["code"] == code, body
    assert tree_digest(rig.project / "dev") == digest  # byte-identical: the task was not blocked
    assert rig.srv.scheduler.db.runs() == []


def test_an_option_problem_found_at_start_fails_the_run_and_leaves_the_task_alone(rig):
    session = rig.hold(rig.alpha)
    status, _h, body = rig.queue("do-it", options={"effort": "high", "timeout_s": 30})
    run_id = body["run"]["id"]
    rig.edge.EFFORTS = ("low",)  # the menu changed since the request was accepted
    digest = tree_digest(rig.project / "dev")
    rig.end_session(session)
    done = rig.srv.settle(run_id)
    assert (done.state, done.code) == (runtime.FAILED, "bad-options")
    assert tree_digest(rig.project / "dev") == digest


def test_options_chosen_on_a_queued_entry_are_what_the_started_job_uses(rig):
    session = rig.hold(rig.alpha)
    status, _h, body = rig.queue("do-it", options={"timeout_s": 600})
    run_id = body["run"]["id"]
    status, _h, saved = rig.run_options("do-it", {"effort": "low", "timeout_s": 45})
    assert status == 200 and saved["run"]["effort"] == "low" and saved["run"]["timeout_s"] == 45
    assert rig.srv.scheduler.db.get(run_id).options == {"effort": "low", "timeout_s": 45.0}
    rig.end_session(session)
    done = rig.srv.settle(run_id)
    assert done.state == runtime.ACCEPTED
    job = yaml.safe_load(rig.edge.job_files[-1].read_text())["jobs"][0]
    assert job["options"]["effort"] == "low"
    spec = json.loads((Path(done.run_dir) / "spec.json").read_text())
    assert spec["timeout_s"] == 45.0
    status, _h, late = rig.run_options("do-it", {"effort": "high"})
    assert status == 409 and late["error"]["code"] == "not-queued"


def test_saving_bad_run_options_is_refused_and_keeps_the_old_ones(rig):
    session = rig.hold(rig.alpha)
    status, _h, body = rig.queue("do-it", options={"timeout_s": 600})
    run_id = body["run"]["id"]
    for options in ({"worker": "codex"}, {"effort": "nope"}, {"entry": "ghost"}):
        status, _h, answer = rig.run_options("do-it", options)
        assert status == 422 and answer["error"]["code"] == "bad-options"
    assert rig.srv.scheduler.db.get(run_id).options == {"timeout_s": 600.0}
    assert rig.run_options("later-one", {"effort": "low"})[0] == 409  # no queued entry for that item
    rig.end_session(session)


def test_capabilities_describe_the_run_options(rig):
    options = rig.dashboard()["capabilities"]["run_options"]
    assert options["defaults"]["worker"] == "claude" and options["defaults"]["timeout_s"] == 7200.0
    assert options["workers"]["claude"]["available"] is True and "high" in options["workers"]["claude"]["efforts"]
    assert options["workers"]["codex"]["available"] is False and "Codex" in options["workers"]["codex"]["reason"]


# -- the start loop ---------------------------------------------------------------------------------


def test_queued_runs_behind_a_held_lease_are_not_translated(rig, monkeypatch):
    """RED CHECK: translate before checking the lease and every tick runs git for every queued entry."""
    add_ready(rig, 5)
    rig.edge.behavior = "hang"
    status, _h, body = rig.queue("do-it", options={"timeout_s": 60})
    rig.srv.settle(body["run"]["id"], states=(runtime.RUNNING,))
    status, _h, queued = rig.queue_all(options={"timeout_s": 60})
    assert status == 202 and len(queued["runs"]) == 6  # later-one + five extras
    calls = []
    real = translate.validate_ref
    monkeypatch.setattr(translate, "validate_ref", lambda *a, **k: calls.append(a) or real(*a, **k))
    for _ in range(3):
        rig.srv.scheduler.tick()
    assert calls == []
    assert [r.state for r in rig.srv.scheduler.db.runs((runtime.QUEUED,))] == ["queued"] * 6


def test_a_run_behind_a_held_task_lets_a_later_entry_of_another_task_pass(rig):
    session = rig.hold(rig.alpha)
    first = rig.queue("do-it", options={"timeout_s": 30})[2]["run"]["id"]
    second = rig.queue("b-item", task="beta", options={"timeout_s": 30})[2]["run"]["id"]
    assert rig.srv.settle(second).state == runtime.ACCEPTED
    assert rig.srv.scheduler.db.get(first).state == runtime.QUEUED
    rig.end_session(session)
    assert rig.srv.settle(first).state == runtime.ACCEPTED
