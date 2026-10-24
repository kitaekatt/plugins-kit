"""Machine-interface tests: full input, actual CAS races and semantic refusals."""
from __future__ import annotations

from io import StringIO
import json
import multiprocessing
from pathlib import Path

import pytest

from task_export_support import MACHINE, make_world
from task_system import answer_store, manage, mutation, questions, session_publish
from task_system.answers_types import Answerer
from task_system.discovery import read_task_block
from task_system.manage_cli import document

TASK = "dev/tasks/build-export"
ITEM = "write-schema"


def _mine():
    return MACHINE


@pytest.fixture
def world(tmp_path):
    parent = make_world(tmp_path)
    return parent / "alpha", tmp_path / "local"


def _input(root, item=ITEM):
    return manage.input_payload(root, TASK, item, resolve_machine=_mine)


def _request(revision, action="task-update", fields=None, task=TASK):
    return {"task": task, "expected_revision": revision, "action": action,
            "fields": {"description": "Updated from manager"} if fields is None else fields}


def _mutate(world, request):
    root, local = world
    return manage.mutate_payload(root, request, data_dir=local, resolve_machine=_mine)


def test_protocols_are_additive_and_require_negotiation():
    code, result = document(["protocols", "--format", "json"], version="test")
    assert code == 0
    assert set(result["payload"]["actions"]) == set(manage.ACTIONS)
    assert result["payload"]["revision_files"] == list(mutation.FILES)
    assert document(["input", "--format", "json", "--project", "/"], version="test")[0] == 2
    assert document(["input", "--format", "json", "--protocol", "99"], version="test")[0] == 4


def test_execution_input_keeps_long_instructions_and_acceptance_argv(world):
    root, _ = world
    plan = root / TASK / "plan.md"
    text = plan.read_text()
    text = text.replace("    title: Write the schema", "    accept: [[echo, checked]]\n    title: Write the schema")
    text += "\n### write-schema -- Full instructions\n\n" + "long instructions " * 800
    # The fixture already has a section: replace it, rather than creating duplicate instructions.
    text = text.replace("### write-schema -- Write the schema", "### old-section -- Write the schema")
    plan.write_text(text)
    result = _input(root)
    assert len(result["instructions"]) > 8000
    assert result["acceptance_argv"] == [["echo", "checked"]]
    assert result["item"]["accept"] == [["echo", "checked"]]
    assert result["task"]["revision"] == mutation.folder_revision(root / TASK)
    assert result["repository"] and result["checkout"] and result["git"]["head"]


def test_context_edits_change_the_machine_revision(world):
    root, _ = world
    revision = _input(root)["task"]["revision"]
    with (root / TASK / "CLAUDE.md").open("a") as handle:
        handle.write("\nAn additional execution constraint.\n")
    with pytest.raises(mutation.StaleRevision):
        _mutate(world, _request(revision))


@pytest.mark.parametrize("body", ["", "\n### write-schema -- Duplicate\n\nAnother instruction.\n"])
def test_missing_or_duplicate_instructions_refuse_without_side_effects(world, body):
    root, local = world
    plan = root / TASK / "plan.md"
    if body:
        plan.write_text(plan.read_text() + body)
    else:
        plan.write_text(plan.read_text().replace("write-schema --", "other-section --"))
    with pytest.raises(manage.ManageError, match="instructions"):
        _input(root)
    assert not local.exists()


def test_oversized_input_refuses_instead_of_truncating(world, monkeypatch):
    root, _ = world
    monkeypatch.setattr(manage, "INPUT_LIMIT", 1)
    with pytest.raises(manage.ManageError) as caught:
        _input(root)
    assert caught.value.code == "too_large"


def test_stale_revision_has_no_writes(world):
    root, _ = world
    revision = _input(root)["task"]["revision"]
    with (root / TASK / "log.md").open("a") as handle:
        handle.write("\nA concurrent harness edit.\n")
    before = mutation.read_files(root / TASK)
    with pytest.raises(mutation.StaleRevision):
        _mutate(world, _request(revision))
    assert mutation.read_files(root / TASK) == before


def test_candidate_validation_refuses_before_writes(world):
    root, _ = world
    before = mutation.read_files(root / TASK)
    with pytest.raises(manage.ManageError):
        _mutate(world, _request(mutation.revision_of(before), "item-update", {"item": ITEM, "state": "done"}))
    assert mutation.read_files(root / TASK) == before


@pytest.mark.parametrize("invalid", ["not-a-mapping", None, 7, ["nested"]])
def test_nonmapping_items_refuse_without_writing_any_file(world, invalid):
    root, _ = world
    path = root / TASK / "plan.md"
    plan = path.read_text()
    match, data, entries = mutation._require_block(plan)
    entries.append(invalid)
    path.write_text(mutation._splice(plan, match, data))
    before = mutation.read_files(root / TASK)
    with pytest.raises(mutation.MutationError, match="mapping"):
        _mutate(world, _request(mutation.revision_of(before), "item-update", {"item": ITEM, "note": "An edit"}))
    assert mutation.read_files(root / TASK) == before


def test_semantic_edits_and_post_worker_completion(world):
    root, _ = world
    revision = _input(root)["task"]["revision"]
    changed = _mutate(world, _request(revision, "item-update", {"item": ITEM, "note": "Reviewed", "state": "deferred"}))
    result = _input(root)
    assert result["item"]["state"] == "deferred"
    assert result["item"]["note"] == "Reviewed"
    # Completion compares the validated post-worker revision, not an earlier queue revision.
    completed = _mutate(world, _request(changed["revision"], "item-complete", {"item": ITEM, "summary": "Acceptance passed"}))
    final = _input(root, None)
    assert final["task"]["revision"] == completed["revision"]
    assert ITEM not in [entry["id"] for entry in final["task"]["items"]]
    assert "Acceptance passed" in final["task"]["plan"]


def test_create_validates_first_item_before_creating_anything(world):
    root, _ = world
    request = _request(None, "task-create", {"title": "New task", "first_item": {"title": "Item", "details": "### Bad heading\nNo"}},
                       "dev/tasks/new-task")
    with pytest.raises(Exception):
        _mutate(world, request)
    assert not (root / "dev/tasks/new-task").exists()


def test_create_expected_absence_and_add_item(world):
    root, _ = world
    request = _request(None, "task-create", {"title": "A new task", "priority": "P2",
                       "first_item": {"title": "Implement thing", "details": "Implement exactly this."}}, "dev/tasks/new-task")
    created = _mutate(world, request)
    result = manage.input_payload(root, created["task"], "implement-thing", resolve_machine=_mine)
    assert result["task"]["title"] == "A new task"
    assert result["task"]["priority"] == "P2"
    assert result["instructions"] == "Implement exactly this."
    before = mutation.read_files(root / created["task"])
    with pytest.raises(manage.ManageError) as caught:
        _mutate(world, request)
    assert caught.value.code == "conflict"
    assert mutation.read_files(root / created["task"]) == before


@pytest.mark.parametrize("task", ["dev/tasks/../../escape", "/dev/tasks/foo", "tmp/foo", "dev/tasks/archived-tasks", "dev/tasks/.."])
def test_path_escape_refused(world, task):
    root, _ = world
    with pytest.raises(manage.ManageError):
        manage.input_payload(root, task, resolve_machine=_mine)


def test_linked_root_works_but_linked_task_files_are_refused(world, tmp_path):
    root, _ = world
    source = root / TASK / "plan.md"
    target = tmp_path / "external.md"
    target.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(target)
    with pytest.raises(manage.ManageError, match="links"):
        _input(root)


def test_verified_session_holds_but_unverified_does_not(world, monkeypatch):
    root, _ = world
    revision = _input(root)["task"]["revision"]
    monkeypatch.setattr(manage, "_registrations", lambda *_: [{"local": True, "state": "running"}])
    with pytest.raises(manage.ManageError) as caught:
        _mutate(world, _request(revision))
    assert caught.value.code == "held"
    monkeypatch.setattr(manage, "_registrations", lambda *_: [{"local": False, "state": "unverified"}])
    assert _mutate(world, _request(revision))["revision"] != revision


def test_session_dismiss_uses_exact_task_tuple_and_publishes(world, monkeypatch):
    root, _ = world
    original = _input(root)
    record = original["task"]["sessions"][0]
    fields = {key: record[key] for key in ("host", "session_id", "registered_at")}
    calls = []

    def publish(task_root, machine, **_kwargs):
        calls.append((task_root, machine))
        return session_publish.PublishResult("published", "published", True, True, 1, (), root)

    monkeypatch.setattr(manage.session_publish, "publish", publish)
    result = _mutate(world, _request(original["task"]["revision"], "session-dismiss", fields))
    assert result["publication"]["published"]
    assert calls and calls[0][1] == MACHINE
    assert _input(root)["task"]["sessions"] == []
    json.dumps(result)


def test_a_session_for_another_task_cannot_be_dismissed(world, monkeypatch):
    root, _ = world
    record = _input(root)["task"]["sessions"][0]
    other = "dev/tasks/other-task"
    _mutate(world, _request(None, "task-create", {"title": "Other task"}, other))
    revision = manage.input_payload(root, other, resolve_machine=_mine)["task"]["revision"]
    monkeypatch.setattr(manage.sessions, "dismiss", lambda *_args, **_kwargs: pytest.fail("wrong task mutated"))
    with pytest.raises(manage.ManageError) as caught:
        _mutate(world, _request(revision, "session-dismiss", {key: record[key] for key in ("host", "session_id", "registered_at")}, other))
    assert caught.value.code == "conflict"


def test_grounded_defaults_require_current_exact_answer(world, monkeypatch):
    root, _ = world
    folder = root / TASK
    catalog = questions.load_catalog(questions.default_shipped_dir())
    monkeypatch.setattr(questions, "default_fleet_dir", lambda: root / "no-fleet")
    question = catalog.get("decisions")
    choice = {"item": "pick-name", "question": "Which command name?", "default": "export", "evidence": "export reads as the verb"}
    fingerprint = answer_store.question_fingerprint(folder, read_task_block(folder), question)
    answer_store.write_answer(folder, "decisions", MACHINE, answer_store.NewAnswer(
        question_version=question.version, answer={"decisions": [choice]}, inputs_fingerprint=fingerprint,
        as_of=answer_store.AsOf(task_commit=None, code_commit=None),
        answerer=Answerer(kind="model", entry="fixture", name=None, tier="free", effort="medium", attempts=(("fixture", "completed"),)),
        generated_at="2026-10-23T12:00:00-05:00"))
    revision = _input(root)["task"]["revision"]
    with pytest.raises(manage.ManageError):
        _mutate(world, _request(revision, "accept-defaults", {"defaults": [{**choice, "default": "dump"}]}))
    _mutate(world, _request(revision, "accept-defaults", {"defaults": [choice]}))
    final = _input(root, None)
    item = next(entry for entry in final["task"]["items"] if entry["id"] == "pick-name")
    assert item["state"] == "available" and item["note"] == "Accepted default: export"


def test_context_race_during_preparation_does_not_overwrite(world, monkeypatch):
    root, _ = world
    revision = _input(root)["task"]["revision"]
    original = mutation.TaskMutator._write_temp

    def change_context(path, text):
        temp = original(path, text)
        with (root / TASK / "CLAUDE.md").open("a") as handle:
            handle.write("\nA concurrent execution constraint.\n")
        return temp

    monkeypatch.setattr(mutation.TaskMutator, "_write_temp", staticmethod(change_context))
    before = (root / TASK / "task.yaml").read_bytes()
    with pytest.raises(manage.ManageError) as caught:
        _mutate(world, _request(revision))
    assert caught.value.code == "conflict"
    assert (root / TASK / "task.yaml").read_bytes() == before
    assert not list((root / TASK).glob("*.manage.tmp"))


def test_creation_partial_error_names_created_files(world, monkeypatch):
    root, _ = world

    def fail_replace(*_args):
        raise mutation.MutationError("replace-failed", "injected replacement failure")

    monkeypatch.setattr(mutation.TaskMutator, "_replace", staticmethod(fail_replace))
    request = _request(None, "task-create", {"title": "New task"}, "dev/tasks/new-task")
    with pytest.raises(manage.ManageError) as caught:
        _mutate(world, request)
    assert caught.value.written
    assert (root / "dev/tasks/new-task/task.yaml").exists()
    assert not list((root / "dev/tasks/new-task").glob("*.manage.tmp"))


def test_strict_json_and_one_document_errors(world):
    root, local = world
    argv = ["mutate", "--format", "json", "--protocol", "1", "--project", str(root)]
    for text in ('{"task":1,"task":2}', 'NaN', '{'):
        code, result = document(argv, stdin=StringIO(text), version="test", data_dir=local, resolve_machine=_mine)
        assert code == 2
        assert result["error"]["code"] == "usage"
        json.dumps(result)


def _race(root, local, revision, outcomes):
    try:
        _mutate((Path(root), Path(local)), _request(revision))
        outcomes.put("written")
    except mutation.StaleRevision:
        outcomes.put("conflict")


def test_two_processes_with_one_revision_write_exactly_once(world):
    root, local = world
    revision = _input(root)["task"]["revision"]
    context = multiprocessing.get_context("spawn")
    outcomes = context.Queue()
    children = [context.Process(target=_race, args=(str(root), str(local), revision, outcomes)) for _ in range(2)]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        assert child.exitcode == 0
    assert sorted(outcomes.get(timeout=5) for _ in children) == ["conflict", "written"]
    outcomes.close()
    outcomes.join_thread()
