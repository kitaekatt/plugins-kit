"""Expected-revision, atomic mutation of a task folder (the dashboard's only write path).

The harness writes task folders without taking the dashboard's lock, so every
dashboard write must re-check the bytes it was built from immediately before it
replaces them, and a lost race must be loud, not an overwrite.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from dashboard_support import agent_world

from task_system.dashboard import blocker, mutation
from task_system.dashboard.mutation import (
    LOG,
    TASK_YAML,
    MutationError,
    ProcessLock,
    StaleRevision,
    TaskMutator,
    append_log,
    folder_revision,
    raw_items,
    set_item,
    set_task_status,
    stored_status,
)


@pytest.fixture
def folder(tmp_path):
    _devroot, _project, folder = agent_world(tmp_path, repo=False)
    return folder


@pytest.fixture
def mutator(tmp_path):
    return TaskMutator(tmp_path / "mutation.lock", lock_timeout_s=1.0)


def test_the_revision_changes_with_each_mutable_file_and_ignores_the_others(folder):
    base = folder_revision(folder)
    assert folder_revision(folder) == base
    (folder / "CLAUDE.md").write_text("changed\n", encoding="utf-8")
    assert folder_revision(folder) == base  # not a file the dashboard edits
    for name in ("plan.md", "task.yaml", "log.md"):
        before = folder_revision(folder)
        with open(folder / name, "a", encoding="utf-8") as handle:
            handle.write("\n")
        assert folder_revision(folder) != before, name


def test_a_stale_expected_revision_writes_nothing(folder, mutator):
    old = folder_revision(folder)
    with open(folder / "log.md", "a", encoding="utf-8") as handle:
        handle.write("- 2026-10-03 08:00: harness edit\n")
    snapshot = {n: (folder / n).read_bytes() for n in ("plan.md", "task.yaml", "log.md")}
    with pytest.raises(StaleRevision) as raised:
        mutator.mutate(folder, lambda t: {LOG: t.log + "- x: y\n"}, expected_revision=old)
    assert raised.value.code == "stale-revision" and raised.value.current == folder_revision(folder)
    assert snapshot == {n: (folder / n).read_bytes() for n in snapshot}


def test_a_harness_write_between_the_read_and_the_replace_is_never_overwritten(folder, mutator):
    """The race the lock cannot prevent, because the harness does not take it."""
    harness_text = "- 2026-10-03 08:00: the harness got there first\n"

    def edit(texts):
        with open(folder / "log.md", "a", encoding="utf-8") as handle:  # the harness writes now
            handle.write(harness_text)
        return {LOG: texts.log + "- 2026-10-03 08:01: dashboard\n"}

    with pytest.raises(StaleRevision, match="log.md"):
        mutator.mutate(folder, edit)
    log = (folder / "log.md").read_text(encoding="utf-8")
    assert harness_text in log and "dashboard" not in log  # the harness's edit survives
    assert not list(folder.glob("*.dash.tmp"))  # and no temp file is left behind


def test_a_failing_replace_is_a_loud_error_naming_the_file_with_no_retry(folder, mutator, monkeypatch):
    calls = []

    def boom(src, dst):
        calls.append(dst)
        raise PermissionError("locked by another process")

    monkeypatch.setattr(mutation.os, "replace", boom)
    with pytest.raises(MutationError) as raised:
        mutator.mutate(folder, lambda t: {LOG: t.log + "- x: y\n"})
    assert raised.value.code == "replace-failed" and "log.md" in str(raised.value) and len(calls) == 1
    assert not list(folder.glob("*.dash.tmp"))
    assert "x: y" not in (folder / "log.md").read_text(encoding="utf-8")


def test_a_file_keeps_its_line_endings(folder, mutator):
    crlf = folder / "log.md"
    crlf.write_bytes(b"- 2026-10-01 09:00: began\r\n")
    plan = folder / "plan.md"
    before_plan = plan.read_bytes()
    mutator.mutate(folder, lambda t: {LOG: append_log(t.log, "added")})
    assert crlf.read_bytes().count(b"\r\n") == 2 and b"\n" not in crlf.read_bytes().replace(b"\r\n", b"")
    assert plan.read_bytes() == before_plan  # an untouched file is untouched, byte for byte
    lf = folder / "task.yaml"
    lf.write_bytes(lf.read_bytes().replace(b"\r\n", b"\n"))  # (the fixture writes the platform's line ending)
    assert b"\r" not in lf.read_bytes()
    mutator.mutate(folder, lambda t: {TASK_YAML: set_task_status(t.task_yaml, "blocked")})
    assert b"\r" not in lf.read_bytes() and stored_status(lf.read_text(encoding="utf-8")) == "blocked"


def test_an_edit_that_names_another_file_is_refused(folder, mutator):
    with pytest.raises(MutationError, match="may only change"):
        mutator.mutate(folder, lambda t: {"CLAUDE.md": "x"})


def test_a_second_writer_waits_for_the_lock_then_fails_loudly(folder, tmp_path):
    lock_path = tmp_path / "mutation.lock"
    with ProcessLock(lock_path, timeout_s=1.0):
        with pytest.raises(MutationError) as raised:
            TaskMutator(lock_path, lock_timeout_s=0.3).mutate(folder, lambda t: {})
        assert raised.value.code == "lock-timeout"
    TaskMutator(lock_path, lock_timeout_s=0.3).mutate(folder, lambda t: {})  # free again


def test_the_lock_excludes_another_process(folder, tmp_path):
    lock_path = tmp_path / "mutation.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, sys.argv[1]);"
         "from task_system.dashboard.mutation import ProcessLock;"
         "l = ProcessLock(sys.argv[2], 5); l.__enter__(); print('held', flush=True); time.sleep(60)",
         str(mutation.__file__).rsplit(os.sep + "task_system", 1)[0], str(lock_path)],
        stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), _BOOTSTRAP_GUARD_SKIP_REEXEC="1"))
    try:
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(MutationError, match="another dashboard writer"):
            TaskMutator(lock_path, lock_timeout_s=0.3).mutate(folder, lambda t: {})
    finally:
        holder.kill()
        holder.wait()


# -- the pure text helpers ----------------------------------------------------------


def test_set_item_changes_only_that_items_state_and_note(folder):
    plan = (folder / "plan.md").read_text(encoding="utf-8")
    new = set_item(plan, "do-it", state="blocked-user", note="why: because")
    assert new.split("### do-it")[1] == plan.split("### do-it")[1]  # everything after the block is byte-identical
    items = {i["id"]: i for i in raw_items(new)}
    assert items["do-it"]["state"] == "blocked-user" and items["do-it"]["note"] == "why: because"
    assert items["later-one"] == {"id": "later-one", "title": "Later one", "state": "available", "priority": "P2"}
    assert "note" not in {k for k in raw_items(set_item(new, "do-it", state="available", note=None))[0]}


def test_set_item_refuses_a_missing_or_ambiguous_item_and_a_missing_block(folder):
    plan = (folder / "plan.md").read_text(encoding="utf-8")
    with pytest.raises(MutationError, match="0 items"):
        set_item(plan, "ghost", state="blocked-user", note="x")
    with pytest.raises(MutationError, match="2 items"):
        set_item(plan.replace("id: later-one", "id: do-it"), "do-it", state="blocked-user", note="x")
    with pytest.raises(MutationError, match="no task_items block"):
        set_item("# Plan\n", "do-it", state="blocked-user", note="x")


def test_append_log_uses_the_task_systems_entry_shape(folder):
    from task_system.discovery import read_task_updates

    (folder / "log.md").write_text("- 2026-10-01 09:00: began", encoding="utf-8")  # no trailing newline
    (folder / "log.md").write_text(append_log((folder / "log.md").read_text(encoding="utf-8"), "multi\nline   detail"), encoding="utf-8")
    updates = read_task_updates(folder)
    assert [u.detail for u in updates][:2] == ["multi line detail", "began"] or "multi line detail" in [u.detail for u in updates]


def test_record_block_marks_task_item_and_log_in_one_mutation(folder, mutator):
    outcome = blocker.record_block(mutator, folder, item_id="do-it", note="x " * 400, log_detail="run r1: blocked")
    assert outcome.item_mutated and outcome.item_error is None
    assert stored_status((folder / "task.yaml").read_text(encoding="utf-8")) == "blocked"
    note = {i["id"]: i for i in raw_items((folder / "plan.md").read_text(encoding="utf-8"))}["do-it"]["note"]
    assert len(note) <= blocker.NOTE_LIMIT and note.endswith("...")
    assert "run r1: blocked" in (folder / "log.md").read_text(encoding="utf-8")


def test_record_block_that_cannot_write_task_yaml_fails_rather_than_pretending(folder, mutator):
    (folder / "task.yaml").write_text("not: [valid", encoding="utf-8")
    with pytest.raises(MutationError, match="task.yaml"):
        blocker.record_block(mutator, folder, item_id="do-it", note="x", log_detail="y")


# -- the two-phase mutate: gate, precommit, `written` -------------------------------------------------


def test_the_gate_is_entered_after_the_process_lock_and_only_for_the_commit(folder, tmp_path):
    """RED CHECK: enter the gate before the process lock and a gate holder waits for the lock with the gate held."""
    import threading

    events = []

    class Gate:
        def __enter__(self):
            events.append("gate-in")

        def __exit__(self, *exc):
            events.append("gate-out")

    lock_path = tmp_path / "mutation.lock"
    mutator = TaskMutator(lock_path, lock_timeout_s=1.0)

    def edit(texts):
        events.append("edit")
        return {LOG: texts.log + "- x: y\n"}

    mutator.mutate(folder, edit, gate=Gate(), precommit=lambda: events.append("precommit"))
    assert events == ["edit", "gate-in", "precommit", "gate-out"]  # the edit (and the temp writes) are outside the gate
    # a writer waiting on the process lock does not hold the gate
    held = threading.Event()
    done = threading.Event()
    with ProcessLock(lock_path, timeout_s=1.0):
        events.clear()
        thread = threading.Thread(target=lambda: (held.set(), pytest.raises(MutationError, TaskMutator(lock_path, lock_timeout_s=0.3).mutate, folder, edit, gate=Gate()), done.set()))
        thread.start()
        held.wait(5)
        done.wait(5)
        thread.join(5)
    assert "gate-in" not in events


def test_precommit_runs_even_when_the_edit_changes_nothing_and_a_refusal_writes_nothing(folder, mutator):
    seen = []
    mutator.mutate(folder, lambda t: {}, precommit=lambda: seen.append("ran"))
    assert seen == ["ran"]

    class Refuse(Exception):
        pass

    def refuse():
        raise Refuse()

    before = {n: (folder / n).read_bytes() for n in ("plan.md", "task.yaml", "log.md")}
    with pytest.raises(Refuse):
        mutator.mutate(folder, lambda t: {LOG: t.log + "- x: y\n", "plan.md": t.plan + "\nmore\n"}, precommit=refuse)
    assert before == {n: (folder / n).read_bytes() for n in before}  # nothing replaced
    assert not list(folder.glob("*.dash.tmp"))  # and the temps are gone


def test_every_mutation_error_says_which_files_were_already_replaced(folder, mutator, monkeypatch):
    """RED CHECK: stop recording `written` and a part-way failure looks like no failure at all."""
    real = os.replace

    def fail_on_log(src, dst):
        if str(dst).endswith("log.md"):
            raise PermissionError("locked")
        return real(src, dst)

    monkeypatch.setattr(mutation.os, "replace", fail_on_log)
    with pytest.raises(MutationError) as raised:
        mutator.mutate(folder, lambda t: {"plan.md": t.plan + "\nmore\n", LOG: t.log + "- x: y\n"})
    assert raised.value.code == "replace-failed" and raised.value.written == ("plan.md",)
    assert raised.value.intended == ("plan.md", "log.md") and raised.value.path.name == "log.md"
    monkeypatch.setattr(mutation.os, "replace", real)
    with pytest.raises(StaleRevision) as stale:
        mutator.mutate(folder, lambda t: {}, expected_revision="0" * 24)
    assert stale.value.written == ()


def test_the_new_text_helpers_edit_only_what_they_name(folder):
    plan = (folder / "plan.md").read_text(encoding="utf-8")
    appended = mutation.append_item(plan, {"id": "fresh", "title": "A fresh one", "state": "available"})
    assert [i["id"] for i in raw_items(appended)] == ["do-it", "later-one", "fresh"]
    assert appended.split("### do-it")[1] == plan.split("### do-it")[1]
    with pytest.raises(MutationError, match="already has an item"):
        mutation.append_item(plan, {"id": "do-it", "title": "dup", "state": "available"})
    deferred = mutation.set_item_state(set_item(plan, "do-it", state="available", note="kept"), "do-it", "deferred")
    assert {i["id"]: i for i in raw_items(deferred)}["do-it"] == {"id": "do-it", "title": "Do it", "state": "deferred", "priority": "P1", "note": "kept"}
    task_yaml = (folder / "task.yaml").read_text(encoding="utf-8")
    assert yaml_priority(mutation.set_task_fields(task_yaml, priority="P3")) == "P3"


def yaml_priority(text: str):
    import yaml

    return yaml.safe_load(text)["task"]["priority"]


def test_the_block_finder_is_the_readers(folder):
    """RED CHECK: restore the writer's private fence pattern and it edits a block the reader does not recognise."""
    plan = (folder / "plan.md").read_text(encoding="utf-8").replace("```yaml\n", "```yaml extra words\n", 1)
    assert mutation._items_block(plan) is None and raw_items(plan) is None
