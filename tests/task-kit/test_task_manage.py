"""Machine-interface tests: full input, actual CAS races and semantic refusals."""
from __future__ import annotations

from io import StringIO
import json
import multiprocessing
from pathlib import Path

import pytest

from task_export_support import MACHINE, make_world
from task_system import answer_store, discovery, manage, mutation, questions, session_publish
from task_system.answers_types import Answerer
from task_system.discovery import read_task_block
from task_system.manage_cli import document

TASK = "dev/tasks/build-export"
ITEM = "write-schema"


def _mine():
    return MACHINE


@pytest.fixture(autouse=True)
def _no_user_config(tmp_path, monkeypatch):
    """The machine's own task.local.yaml must not change what these tests see."""
    monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", tmp_path / "no-task.local.yaml")


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


# -- commits: a semantic mutation commits the task files it wrote, in the repository that holds the task ----------

import subprocess

from task_system import plan_sections


def _run(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def linked(world, tmp_path):
    """The world with dev/tasks moved into its own git repository and linked back, as a tasks repo is."""
    root, local = world
    tasks = tmp_path / "tasks-repo"
    (root / "dev/tasks").rename(tasks)
    (root / "dev/tasks").symlink_to(tasks)
    _run(tasks, "init", "-q", "-b", "main")
    _run(tasks, "config", "user.email", "test@example.invalid")
    _run(tasks, "config", "user.name", "Test")
    _run(tasks, "config", "commit.gpgsign", "false")
    _run(tasks, "add", "-A")
    _run(tasks, "commit", "-q", "-m", "seed tasks")
    return root, local, tasks


def test_mutation_commits_the_written_files_in_the_linked_tasks_repository(linked):
    root, local, tasks = linked
    project_head = _run(root, "rev-parse", "HEAD")
    revision = _input(root)["task"]["revision"]
    stray = tasks / "build-export" / "answers" / "stray.txt"
    stray.write_text("not written by the mutation\n", encoding="utf-8")
    _run(tasks, "add", "-N", str(stray))  # an unrelated staged-intent path stays out of the commit
    done = _mutate((root, local), _request(revision, "item-update", {"item": ITEM, "note": "Reviewed"}))
    assert done["commit"] == _run(tasks, "rev-parse", "HEAD")
    assert _run(tasks, "log", "-1", "--format=%s") == f"task manage: {TASK} item-update {ITEM}"
    committed = set(_run(tasks, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert committed == {"build-export/plan.md", "build-export/log.md"}
    assert _run(root, "rev-parse", "HEAD") == project_head  # the host project gets no commit
    assert _run(tasks, "status", "--porcelain", "--", "build-export/plan.md", "build-export/log.md") == ""


def test_mutation_commit_leaves_other_staged_content_staged(linked):
    root, local, tasks = linked
    other = tasks / "other.txt"
    other.write_text("staged by someone else\n", encoding="utf-8")
    _run(tasks, "add", "other.txt")
    revision = _input(root)["task"]["revision"]
    done = _mutate((root, local), _request(revision))
    assert done["commit"] == _run(tasks, "rev-parse", "HEAD")
    assert "other.txt" not in _run(tasks, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert _run(tasks, "diff", "--cached", "--name-only") == "other.txt"


def test_item_complete_commit_names_the_item(linked):
    root, local, tasks = linked
    revision = _input(root)["task"]["revision"]
    done = _mutate((root, local), _request(revision, "item-complete", {"item": ITEM, "summary": "Acceptance passed"}))
    assert _run(tasks, "log", "-1", "--format=%s") == f"task manage: {TASK} item-complete {ITEM}"
    assert done["commit"] == _run(tasks, "rev-parse", "HEAD")
    assert "Acceptance passed" in _run(tasks, "show", "HEAD:build-export/plan.md")


def test_task_create_commits_all_four_new_files(linked):
    root, local, tasks = linked
    created = _mutate((root, local), _request(None, "task-create", {"title": "A new task"}, "dev/tasks/new-task"))
    assert created["commit"] == _run(tasks, "rev-parse", "HEAD")
    assert _run(tasks, "log", "-1", "--format=%s") == "task manage: dev/tasks/new-task task-create"
    committed = set(_run(tasks, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert committed == {f"new-task/{name}" for name in mutation.FILES}


def test_commit_failure_is_loud_and_says_the_edit_happened(linked):
    root, local, tasks = linked
    hook = tasks / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho hook says no >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    head = _run(tasks, "rev-parse", "HEAD")
    revision = _input(root)["task"]["revision"]
    with pytest.raises(manage.ManageError) as caught:
        _mutate((root, local), _request(revision))
    assert caught.value.code == "commit_failed"
    assert "were written but not committed" in str(caught.value)
    assert "hook says no" in str(caught.value)
    assert {Path(path).name for path in caught.value.written} == {"task.yaml", "log.md"}
    assert "Updated from manager" in (tasks / "build-export/task.yaml").read_text(encoding="utf-8")
    assert _run(tasks, "rev-parse", "HEAD") == head


def test_commit_failure_reaches_the_cli_document(linked):
    root, local, tasks = linked
    hook = tasks / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    revision = _input(root)["task"]["revision"]
    argv = ["mutate", "--format", "json", "--protocol", "1", "--project", str(root)]
    code, result = document(argv, stdin=StringIO(json.dumps(_request(revision))), version="test",
                            data_dir=local, resolve_machine=_mine)
    assert code == 1
    assert result["error"]["code"] == "commit_failed"
    assert result["error"]["written"]


def test_git_ignored_task_root_is_written_without_a_commit(world):
    root, _ = world  # the fixture's dev/tasks is git-ignored: there is nothing git can commit
    head = _run(root, "rev-parse", "HEAD")
    done = _mutate(world, _request(_input(root)["task"]["revision"]))
    assert done["commit"] is None
    assert _run(root, "rev-parse", "HEAD") == head


EXACT_PLAN = """# Plan

## Items

```yaml
task_items:
  items:
  - id: a
    title: A
```

### a
Prose for a.

#### detail
Nested detail of a.

### b
Prose for b.

## Later
"""


def test_exact_item_section_is_removed_through_the_next_same_or_higher_heading():
    text = plan_sections.remove_exact_item_section(EXACT_PLAN, "a")
    assert "Prose for a." not in text
    assert "Nested detail of a." not in text
    assert "### b\nProse for b.\n\n## Later\n" in text
    assert "task_items:" in text


@pytest.mark.parametrize("plan", [
    "# P\n\n### a -- A title\nbody\n",            # more than the id on the heading
    "# P\n\n## a\nbody\n",                          # another level
    "# P\n\n### a\nbody\n\n### a\nagain\n",         # ambiguous
    "# P\n\n```\n### a\nbody\n```\n",               # inside a code fence
    "# P\n\n### ab\nbody\n",                        # another id
])
def test_other_headings_are_left_alone(plan):
    assert plan_sections.remove_exact_item_section(plan, "a") == plan


def test_item_complete_removes_only_the_exact_prose_section(world):
    root, _ = world
    plan_path = root / TASK / "plan.md"
    plan_path.write_text(plan_path.read_text(encoding="utf-8").replace(
        "### write-schema -- Write the schema", "### write-schema"), encoding="utf-8")
    revision = _input(root, None)["task"]["revision"]
    _mutate(world, _request(revision, "item-complete", {"item": ITEM, "summary": "Done"}))
    plan = plan_path.read_text(encoding="utf-8")
    assert "### write-schema" not in plan
    assert "Write one JSON Schema per kind and freeze it." not in plan
    assert "### pick-name -- Pick the command name" in plan
    assert "- write-schema: Done" in plan


def _config(tmp_path, text):
    path = tmp_path / "task.local.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_manage_commit_false_writes_but_does_not_commit(linked, tmp_path, monkeypatch):
    root, local, tasks = linked
    monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", _config(tmp_path, "manage:\n  commit: false\n"))
    head = _run(tasks, "rev-parse", "HEAD")
    revision = _input(root)["task"]["revision"]
    done = _mutate((root, local), _request(revision))
    assert done["commit"] is None
    assert _run(tasks, "rev-parse", "HEAD") == head
    assert "Updated from manager" in (tasks / "build-export/task.yaml").read_text(encoding="utf-8")
    assert "build-export/task.yaml" in _run(tasks, "status", "--porcelain")
    created = _mutate((root, local), _request(None, "task-create", {"title": "Quiet"}, "dev/tasks/quiet"))
    assert created["commit"] is None
    assert _run(tasks, "rev-parse", "HEAD") == head


def test_manage_commit_true_in_config_commits(linked, tmp_path, monkeypatch):
    root, local, tasks = linked
    monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", _config(tmp_path, "manage:\n  commit: true\n"))
    done = _mutate((root, local), _request(_input(root)["task"]["revision"]))
    assert done["commit"] == _run(tasks, "rev-parse", "HEAD")


@pytest.mark.parametrize("text", ["manage:\n  commit: maybe\n", "manage:\n  push: true\n", "manage: yes\n"])
def test_bad_manage_config_refuses_before_any_write(linked, tmp_path, monkeypatch, text):
    root, local, tasks = linked
    monkeypatch.setattr(discovery, "TASK_CONFIG_PATH", _config(tmp_path, text))
    before = mutation.read_files(root / TASK)
    with pytest.raises(manage.ManageError) as caught:
        _mutate((root, local), _request(_input(root)["task"]["revision"]))
    assert caught.value.code == "refused"
    assert "manage" in str(caught.value)
    assert mutation.read_files(root / TASK) == before


def test_a_failed_repository_probe_is_loud_not_a_silent_no_commit(linked, monkeypatch):
    root, local, tasks = linked
    real = manage._git_run

    def dubious(top, *args):
        if args[:1] == ("rev-parse",) and "--show-toplevel" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: detected dubious ownership in repository")
        return real(top, *args)

    monkeypatch.setattr(manage, "_git_run", dubious)
    with pytest.raises(manage.ManageError) as caught:
        _mutate((root, local), _request(_input(root)["task"]["revision"]))
    assert caught.value.code == "commit_failed"
    assert "dubious ownership" in str(caught.value)
    assert caught.value.written


def test_a_folder_in_no_repository_is_not_an_error(tmp_path):
    folder = tmp_path / "plain" / "task"
    folder.mkdir(parents=True)
    assert manage._commit_written(folder, ("task.yaml",), "task manage: x") is None
