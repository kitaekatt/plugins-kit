"""The answer store: record layout, ownership, the choice among records, coverage, modification order, and
the list projection and export that read it (summary-on-commit-plan.md, U2).

Revert checks (each named test goes red when the named change is made; recorded in the unit's report):
- hashing ``answers/`` into the fingerprint -> ``test_writing_an_answer_changes_no_fingerprint``
- dropping the ownership check in ``write_answer`` -> ``test_a_file_of_another_machine_is_refused_and_left_byte_identical``
- choosing by configuration or clock -> ``test_the_winner_ignores_the_declaration_and_the_clock``
- choosing among stale records by host alone -> ``test_among_stale_records_the_descendant_commit_wins``
- checking only the working copy in ``delete_answer`` -> ``test_delete_checks_the_committed_preimage``
- dropping the legacy branch in ``listing`` -> ``test_the_legacy_summary_is_served_and_labelled``
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from task_export_support import MACHINE as EXPORT_MACHINE
from task_export_support import inject, load_schema, make_world, schema_errors

from task_system import answer_scripts, answer_store, export, listing, questions
from task_system.answer_store import (
    AnswerOwnershipError,
    AnswerStoreError,
    AsOf,
    NewAnswer,
)
from task_system.answers_types import Answerer
from task_system.discovery import read_task_block
from task_system.export_cli import document
from task_system.machine import Machine

GOLDEN_TASK = Path(__file__).parent / "fixtures" / "answers" / "golden" / "task"
NOW = datetime.datetime(2026, 10, 22, 12, 0)
SHA_A, SHA_B = "a" * 40, "b" * 40

ME = Machine(host="studio", fingerprint="3f9a0c1d2e4b5a6c", os_instance="macos")
OTHER = Machine(host="laptop", fingerprint="9999999999999999", os_instance="linux")


@pytest.fixture(scope="module")
def catalog():
    return questions.load_catalog(questions.default_shipped_dir())


@pytest.fixture
def orientation(catalog):
    return catalog.get("orientation")


@pytest.fixture
def folder(tmp_path) -> Path:
    target = tmp_path / "task"
    shutil.copytree(GOLDEN_TASK, target)
    return target


def git(top: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    done = subprocess.run(
        ["git", "-C", str(top), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", env=env, check=True,
    )
    return done.stdout.strip()


def commit(top: Path, subject: str, **files: str) -> str:
    """Write files (keys use ``__`` for ``/``), commit everything, return the sha."""
    for name, content in files.items():
        path = top / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(top, "add", "-A")
    git(top, "commit", "-q", "-m", subject)
    return git(top, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    top = tmp_path / "repo"
    top.mkdir()
    git(top, "init", "-q", "-b", "main")
    return top


def new_answer(fingerprint: str, *, entry="qwen38-5090", generated_at="2026-10-21T19:05:12-05:00",
               task_commit=SHA_A, headline="Generate each task's orientation") -> NewAnswer:
    return NewAnswer(
        question_version=1,
        answer={"headline": headline, "state": "Design in its third round", "next": "Start the U1 freeze"},
        inputs_fingerprint=fingerprint,
        as_of=AsOf(task_commit=task_commit, code_commit=None),
        answerer=Answerer(kind="model", entry=entry, name=None, tier="free", effort="medium",
                          attempts=((entry, "completed"),)),
        generated_at=generated_at,
    )


def fingerprint_of(folder: Path, question) -> str:
    return answer_store.question_fingerprint(folder, read_task_block(folder), question)


def state_of(folder: Path, question, repo_top=None, status="active"):
    return answer_store.chosen_answer(folder, question, fingerprint_of(folder, question), repo_top, status=status)


# --- record layout ---------------------------------------------------------------


def test_a_record_is_written_where_the_plan_says_and_reads_back(folder, orientation):
    fingerprint = fingerprint_of(folder, orientation)
    path = answer_store.write_answer(folder, "orientation", ME, new_answer(fingerprint))
    assert path == folder / "answers" / "orientation" / "studio.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(data) == [
        "_schema_version", "host", "machine", "os_instance", "question", "question_version", "answer",
        "inputs_fingerprint", "as_of", "answerer", "generated_at",
    ]
    assert (data["_schema_version"], data["host"], data["machine"], data["os_instance"]) == (
        "1", "studio", "3f9a0c1d2e4b5a6c", "macos")
    assert data["as_of"] == {"task_commit": SHA_A, "code_commit": None}
    assert data["answerer"] == {
        "kind": "model", "entry": "qwen38-5090", "tier": "free", "effort": "medium",
        "attempts": [{"entry": "qwen38-5090", "outcome": "completed"}],
    }
    path.read_bytes().decode("ascii")  # a task file stays ASCII
    [record], invalid = answer_store.read_records(folder, "orientation")
    assert invalid == ()
    assert (record.host, record.machine, record.inputs_fingerprint) == ("studio", ME.fingerprint, fingerprint)
    assert record.answer["headline"] == "Generate each task's orientation"
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".")]  # no temporary file is left behind


def test_a_digit_only_fingerprint_survives_the_yaml_round_trip(folder, orientation):
    answer_store.write_answer(folder, "orientation", ME, new_answer("1" * 64, task_commit="2" * 40))
    [record], invalid = answer_store.read_records(folder, "orientation")
    assert invalid == () and record.inputs_fingerprint == "1" * 64 and record.as_of.task_commit == "2" * 40


def test_only_a_model_answer_is_stored(folder):
    script = NewAnswer(1, {"x": "y"}, "0" * 64, AsOf(SHA_A, None),
                       Answerer("script", None, "activity", None, None, ()), "2026-10-21T19:05:12-05:00")
    with pytest.raises(AnswerStoreError, match="only a model answer is stored"):
        answer_store.write_answer(folder, "activity", ME, script)
    assert not (folder / "answers").exists()


def test_writing_an_answer_changes_no_fingerprint(folder, orientation):
    before = fingerprint_of(folder, orientation)
    answer_store.write_answer(folder, "orientation", ME, new_answer(before))
    assert fingerprint_of(folder, orientation) == before
    state = state_of(folder, orientation)
    assert state.state == "current" and state.fingerprint == before == state.current_fingerprint


# --- ownership -------------------------------------------------------------------


def test_a_file_of_another_machine_is_refused_and_left_byte_identical(folder, orientation):
    fingerprint = fingerprint_of(folder, orientation)
    mine = answer_store.write_answer(folder, "orientation", ME, new_answer(fingerprint))
    imposter = Machine(host="studio", fingerprint="aaaaaaaaaaaaaaaa", os_instance="linux")
    before = mine.read_bytes()
    with pytest.raises(AnswerOwnershipError, match="sessions.host") as caught:
        answer_store.write_answer(folder, "orientation", imposter, new_answer(fingerprint, headline="Overwritten"))
    assert str(mine) in str(caught.value)
    assert mine.read_bytes() == before


def test_a_file_whose_owner_cannot_be_proven_is_refused(folder, orientation):
    path = folder / "answers" / "orientation" / "studio.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("not: [a record", encoding="utf-8")
    with pytest.raises(AnswerOwnershipError, match="owner cannot be proven"):
        answer_store.write_answer(folder, "orientation", ME, new_answer(fingerprint_of(folder, orientation)))
    assert path.read_text(encoding="utf-8") == "not: [a record"


def test_an_own_file_is_replaced(folder, orientation):
    fingerprint = fingerprint_of(folder, orientation)
    answer_store.write_answer(folder, "orientation", ME, new_answer(fingerprint, headline="First"))
    answer_store.write_answer(folder, "orientation", ME, new_answer(fingerprint, headline="Second"))
    [record], _ = answer_store.read_records(folder, "orientation")
    assert record.answer["headline"] == "Second"


# --- choosing among records --------------------------------------------------------


def put(folder: Path, machine: Machine, fingerprint: str, **kwargs) -> None:
    answer_store.write_answer(folder, "orientation", machine, new_answer(fingerprint, **kwargs))


def test_the_winner_ignores_the_declaration_and_the_clock(folder, orientation):
    """Two current records: the lowest host id wins whatever entry answered and whatever ``generated_at`` says."""
    fingerprint = fingerprint_of(folder, orientation)
    early, late = "2026-10-20T08:00:00-05:00", "2026-10-22T08:00:00-05:00"
    winners = []
    for first, second in ((("qwen38-5090", early), ("luna", late)), (("luna", late), ("qwen38-5090", early)),
                          (("luna", early), ("qwen38-5090", late)), (("qwen38-5090", late), ("luna", early))):
        target = folder / "answers"
        shutil.rmtree(target, ignore_errors=True)
        put(folder, OTHER, fingerprint, entry=first[0], generated_at=first[1], headline="from laptop")
        put(folder, ME, fingerprint, entry=second[0], generated_at=second[1], headline="from studio")
        state = state_of(folder, orientation)
        assert state.state == "current"
        winners.append((state.host, state.answer["headline"]))
    assert set(winners) == {("laptop", "from laptop")}  # "laptop" < "studio"


def test_among_current_records_a_stale_one_never_wins(folder, orientation):
    fingerprint = fingerprint_of(folder, orientation)
    put(folder, OTHER, "f" * 64, headline="stale from laptop")
    put(folder, ME, fingerprint, headline="current from studio")
    state = state_of(folder, orientation)
    assert (state.state, state.host) == ("current", "studio")


def test_among_stale_records_the_descendant_commit_wins(repo, orientation):
    first = commit(repo, "one", README="one\n")
    second = commit(repo, "two", README="two\n")
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    put(folder, OTHER, "f" * 64, task_commit=first, headline="older, lower host id")
    put(folder, ME, "e" * 64, task_commit=second, headline="newer, higher host id")
    state = state_of(folder, orientation, repo)
    assert (state.state, state.host, state.answer["headline"]) == ("stale", "studio", "newer, higher host id")


def test_incomparable_stale_records_fall_back_to_the_host_id(repo, orientation):
    base = commit(repo, "base", README="base\n")
    left = commit(repo, "left", README="left\n")
    git(repo, "checkout", "-q", "-b", "side", base)
    right = commit(repo, "right", README="right\n")
    git(repo, "checkout", "-q", "main")
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    put(folder, ME, "e" * 64, task_commit=left)
    put(folder, OTHER, "f" * 64, task_commit=right)
    assert state_of(folder, orientation, repo).host == "laptop"


def test_a_commit_the_repository_lacks_ranks_below_every_known_one(repo, orientation):
    known = commit(repo, "known", README="known\n")
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    put(folder, OTHER, "f" * 64, task_commit="9" * 40, headline="unreachable, lower host id")
    put(folder, ME, "e" * 64, task_commit=known, headline="reachable")
    assert state_of(folder, orientation, repo).answer["headline"] == "reachable"


def test_stale_records_outside_a_repository_choose_by_host_id(folder, orientation):
    put(folder, ME, "e" * 64)
    put(folder, OTHER, "f" * 64)
    assert state_of(folder, orientation, None).host == "laptop"


# --- states ------------------------------------------------------------------------


def test_missing_and_not_applicable(folder, orientation):
    assert state_of(folder, orientation).state == "missing"
    assert state_of(folder, orientation, status="archived").state == "not-applicable"
    assert answer_store.chosen_answer(folder, orientation, "0" * 64, None, status="archived").answer is None


def write_raw(folder: Path, name: str, text: str, question="orientation") -> Path:
    path = folder / "answers" / question / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_an_invalid_file_is_a_note_and_valid_records_still_count(folder, orientation):
    fingerprint = fingerprint_of(folder, orientation)
    put(folder, ME, fingerprint)
    put(folder, OTHER, fingerprint)
    renamed = (folder / "answers" / "orientation" / "laptop.yaml").read_text(encoding="utf-8")
    write_raw(folder, "ghost.yaml", renamed)  # host "laptop" in a file named "ghost"
    write_raw(folder, "garbage.yaml", "not: [a record")
    write_raw(folder, "wrong-schema.yaml", renamed.replace("host: laptop", "host: wrong-schema").replace(
        "headline: Generate each task's orientation", "headline: ''"))
    write_raw(folder, "readme.txt", "ignored: not a record file")
    write_raw(folder, ".studio.yaml.123.tmp", "ignored")
    state = state_of(folder, orientation)
    assert state.state == "current" and state.host == "laptop"
    names = sorted(Path(item.path).name for item in state.invalid)
    assert names == ["garbage.yaml", "ghost.yaml", "wrong-schema.yaml"]
    reasons = {Path(item.path).name: item.reason for item in state.invalid}
    assert "differs from the file name" in reasons["ghost.yaml"]
    assert "answer does not match the question's schema" in reasons["wrong-schema.yaml"].replace("the answer", "answer")
    assert all(str(folder) in note for note in state.notes) and len(state.notes) == 3


def test_a_folder_with_only_invalid_files_is_invalid(folder, orientation):
    write_raw(folder, "garbage.yaml", "- just\n- a list\n")
    state = state_of(folder, orientation)
    assert state.state == "invalid" and state.answer is None and len(state.notes) == 1


def test_an_unknown_key_in_a_record_is_invalid(folder, orientation):
    put(folder, ME, fingerprint_of(folder, orientation))
    path = folder / "answers" / "orientation" / "studio.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "extra: 1\n", encoding="utf-8")
    state = state_of(folder, orientation)
    assert state.state == "invalid" and "unknown keys ['extra']" in state.notes[0]


# --- script answers and coverage ----------------------------------------------------


def test_script_answers_are_computed_on_read_and_never_stored(repo, catalog):
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    head = commit(repo, "add task")
    log = answer_scripts.read_repo_log(repo)
    block = read_task_block(folder)
    result = answer_store.task_answers(catalog, folder, block, "active", repo_top=repo, log=log, now=NOW)
    commits, activity = result.states["task_commits"], result.states["activity"]
    assert commits.state == activity.state == "current"
    assert commits.answerer == Answerer("script", None, "task_commits", None, None, ())
    assert commits.as_of == AsOf(head, None) and commits.fingerprint is None
    assert [c["sha"] for c in commits.answer["commits"]] == [head]
    assert activity.answer["last_entry"]
    assert not (folder / "answers").exists()
    later = answer_store.task_answers(catalog, folder, block, "active", repo_top=repo, log=log,
                                      now=NOW + datetime.timedelta(days=3))
    assert later.states["activity"].answer["days_since"] == activity.answer["days_since"] + 3


def test_coverage_counts_current_of_applicable(repo, catalog, orientation):
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    commit(repo, "add task")
    log = answer_scripts.read_repo_log(repo)
    block = read_task_block(folder)

    def covered(status="active"):
        return answer_store.task_answers(catalog, folder, block, status, repo_top=repo, log=log, now=NOW).coverage

    assert covered().as_dict() == {"catalog_version": 1, "applicable": 4, "current": 2}
    put(folder, ME, fingerprint_of(folder, orientation))
    assert covered().as_dict() == {"catalog_version": 1, "applicable": 4, "current": 3}
    assert covered("closed").as_dict() == {"catalog_version": 1, "applicable": 3, "current": 3}
    assert covered("archived").as_dict() == {"catalog_version": 1, "applicable": 0, "current": 0}


# --- deleting ------------------------------------------------------------------------


def test_delete_removes_an_own_committed_file_and_the_emptied_folders(repo, orientation):
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    put(folder, ME, fingerprint_of(folder, orientation))
    commit(repo, "answer")
    removed = answer_store.delete_answer(folder, "orientation", ME, repo)
    assert removed == folder / "answers" / "orientation" / "studio.yaml"
    assert not (folder / "answers").exists()
    assert answer_store.delete_answer(folder, "orientation", ME, repo) is None


def test_delete_removes_an_uncommitted_own_file(repo, orientation):
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    commit(repo, "task")
    put(folder, ME, fingerprint_of(folder, orientation))
    assert answer_store.delete_answer(folder, "orientation", ME, repo) is not None


def test_delete_checks_the_committed_preimage(repo, orientation):
    """The commit holds another machine's file under this host id; the working copy was rewritten to look mine."""
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    fingerprint = fingerprint_of(folder, orientation)
    imposter = Machine(host="studio", fingerprint="aaaaaaaaaaaaaaaa", os_instance="linux")
    put(folder, imposter, fingerprint)
    commit(repo, "another machine's answer")
    path = folder / "answers" / "orientation" / "studio.yaml"
    # the working copy is rewritten to carry this machine's fingerprint
    path.write_text(path.read_text(encoding="utf-8").replace(imposter.fingerprint, ME.fingerprint), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(AnswerOwnershipError, match="committed preimage"):
        answer_store.delete_answer(folder, "orientation", ME, repo)
    assert path.read_bytes() == before


def test_delete_refuses_a_working_copy_that_is_another_machines(repo, orientation):
    folder = repo / "task"
    shutil.copytree(GOLDEN_TASK, folder)
    fingerprint = fingerprint_of(folder, orientation)
    put(folder, ME, fingerprint)
    commit(repo, "mine")
    put(folder, OTHER, fingerprint)
    path = folder / "answers" / "orientation" / "laptop.yaml"
    with pytest.raises(AnswerOwnershipError, match="laptop.yaml"):
        answer_store.delete_answer(folder, "orientation", ME, repo, host="laptop")
    assert path.exists()


# --- modification order ---------------------------------------------------------------


def test_modification_order_follows_the_commit_graph(repo):
    folders = {}
    for name in ("alpha", "beta", "gamma"):
        folders[name] = repo / "tasks" / name
        commit(repo, f"task {name}", **{f"tasks__{name}__task.yaml": "task: {}\n"})
    never = repo / "tasks" / "never-committed"
    never.mkdir(parents=True)
    log = answer_scripts.read_repo_log(repo)
    ordered = answer_store.modification_order(repo, [folders["alpha"], never, folders["gamma"], folders["beta"]], log)
    assert [p.name for p in ordered] == ["gamma", "beta", "alpha", "never-committed"]
    # a commit that touches only alpha's answers/ does not make alpha newer
    commit(repo, "answer for alpha", tasks__alpha__answers__orientation__studio__yaml="x\n")
    log = answer_scripts.read_repo_log(repo)
    ordered = answer_store.modification_order(repo, list(folders.values()) + [never], log)
    assert [p.name for p in ordered] == ["gamma", "beta", "alpha", "never-committed"]
    # a task-record edit does
    commit(repo, "edit alpha", tasks__alpha__plan__md="plan\n")
    log = answer_scripts.read_repo_log(repo)
    assert [p.name for p in answer_store.modification_order(repo, list(folders.values()), log)][0] == "alpha"


def test_modification_order_breaks_ties_by_path(repo):
    commit(repo, "both", tasks__b__task__yaml="x\n", tasks__a__task__yaml="x\n")
    log = answer_scripts.read_repo_log(repo)
    ordered = answer_store.modification_order(repo, [repo / "tasks" / "b", repo / "tasks" / "a"], log)
    assert [p.name for p in ordered] == ["a", "b"]


# --- the list projection ----------------------------------------------------------------


@pytest.fixture
def project(tmp_path, catalog) -> Path:
    """A project whose dev/tasks holds one active task without a legacy summary (a git-less project)."""
    root = tmp_path / "project"
    task = root / "dev" / "tasks" / "golden"
    shutil.copytree(GOLDEN_TASK, task)
    task_yaml = task / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    for key in listing.SUMMARY_METADATA_KEYS:  # a task without a legacy summary
        data["task"].pop(key, None)
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return root


def view_json(root: Path, catalog, **kwargs) -> dict:
    data = listing.listing_data(listing.collect_listing("project", root, catalog=catalog, now=NOW, **kwargs))
    [task] = data["tasks"]
    return {"task": task, "data": data}


def test_the_projection_is_version_2_and_carries_answers(project, catalog):
    result = view_json(project, catalog)
    assert result["data"]["schema_version"] == "2" == listing.PROJECTION_VERSION
    task = result["task"]
    assert set(task["answers"]) == set(catalog.ids)
    assert task["coverage"]["catalog_version"] == 1
    assert task["answers"]["activity"]["state"] == "current"
    assert task["answers"]["activity"]["answerer"]["kind"] == "script"
    assert (task["summary"], task["summary_source"], task["summary_status"]) == (None, None, "missing")


def test_the_legacy_summary_is_served_and_labelled(project, catalog):
    task_yaml = project / "dev" / "tasks" / "golden" / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    data["task"]["summary"] = "A legacy one-line summary."
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    task = view_json(project, catalog)["task"]
    assert (task["summary"], task["summary_source"]) == ("A legacy one-line summary.", "legacy")
    assert task["summary_status"] in ("present", "stale")
    assert task["answers"]["orientation"]["state"] == "missing"


def test_an_orientation_answer_supersedes_the_legacy_summary(project, catalog, orientation):
    task = project / "dev" / "tasks" / "golden"
    task_yaml = task / "task.yaml"
    data = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    data["task"]["summary"] = "A legacy one-line summary."
    task_yaml.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    fingerprint = fingerprint_of(task, orientation)
    put(task, ME, fingerprint, headline="The answer's headline")
    served = view_json(project, catalog)["task"]
    assert (served["summary"], served["summary_source"], served["summary_status"]) == (
        "The answer's headline", "answer", "present")
    assert served["answers"]["orientation"]["fingerprint"] == fingerprint
    assert served["answers"]["orientation"]["answerer"]["entry"] == "qwen38-5090"
    # editing the inputs makes the same answer stale, and it is still served
    (task / "plan.md").write_text("# New plan\n", encoding="utf-8")
    stale = view_json(project, catalog)
    assert (stale["task"]["summary"], stale["task"]["summary_status"]) == ("The answer's headline", "stale")
    assert any(item["code"] == "stale_summary" for item in stale["data"]["diagnostics"])


def test_an_invalid_answer_file_reports_without_a_summary(project, catalog):
    task = project / "dev" / "tasks" / "golden"
    write_raw(task, "garbage.yaml", "not: [a record")
    result = view_json(project, catalog)
    assert result["task"]["summary_status"] == "invalid" and result["task"]["summary_missing"] is True
    assert any(item["code"] == "invalid_answer" for item in result["data"]["diagnostics"])
    assert any("garbage.yaml" in note for note in result["data"]["notes"])


def test_a_question_that_does_not_apply_reads_not_applicable(project, catalog):
    narrowed = questions.load_catalog(questions.default_shipped_dir())
    from dataclasses import replace
    narrowed = replace(narrowed, questions=tuple(
        replace(q, applies_to=("closed",)) if q.id == "orientation" else q for q in narrowed.questions))
    task = view_json(project, narrowed)["task"]
    assert task["summary_status"] == "not-applicable" and task["answers"]["orientation"]["state"] == "not-applicable"


def test_a_catalog_that_cannot_load_is_a_note_not_a_crash(project, tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    monkeypatch.setattr(questions, "default_fleet_dir", lambda: fleet)
    data = listing.listing_data(listing.collect_listing("project", project))
    assert any(note.startswith("answers: the question catalog cannot be loaded") and "broken.yaml" in note
               for note in data["notes"])
    assert data["tasks"][0]["answers"] == {} and data["tasks"][0]["coverage"] is None


# --- the export -----------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def tasks_payload(world) -> dict:
    code, doc = document(["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)], **inject())
    assert code == 0, doc
    assert schema_errors(doc, load_schema(), load_schema()) == []
    return doc


def test_the_protocol_stays_1_with_the_additive_fields(world):
    assert export.PROTOCOLS == (1,)
    assert export.protocols_payload() == {"protocols": [1]}
    doc = tasks_payload(world)
    assert doc["protocol"] == 1
    for task in doc["payload"]["tasks"]:
        assert set(task["answers"]) == {"orientation", "decisions", "activity", "task_commits"}
        assert set(task["coverage"]) == {"catalog_version", "applicable", "current"}
    code, doc = document(["tasks", "--format", "json", "--protocol", "2", "--parent", str(world)], **inject())
    assert code == 4


def test_the_export_serves_summary_from_the_answer_and_labels_the_source(world):
    by_path = {t["path"]: t for t in tasks_payload(world)["payload"]["tasks"]}
    assert by_path["dev/tasks/wait-on-review"]["summary_source"] == "answer"
    assert by_path["dev/tasks/wait-on-review"]["summary"] == "Wait for the build-export review"
    assert by_path["dev/tasks/wait-on-review"]["answers"]["orientation"]["state"] == "current"
    build = by_path["dev/tasks/build-export"]
    assert build["answers"]["orientation"]["state"] == "stale"  # the fixture record's fingerprint matches nothing
    assert build["answers"]["orientation"]["answerer"]["entry"] == "example-endpoint"
    legacy = by_path["dev/tasks/old-cleanup"]
    assert legacy["answers"]["orientation"]["state"] == "missing"
    assert legacy["summary_source"] in ("task.yaml", None)


def test_the_export_reports_an_invalid_answer_file_in_errors(world):
    folder = world / "alpha" / "dev" / "tasks" / "old-cleanup"
    write_raw(folder, "garbage.yaml", "not: [a record")
    payload = tasks_payload(world)["payload"]
    [error] = payload["errors"]
    assert error["code"] == "answers" and "garbage.yaml" in error["message"]
    old = next(t for t in payload["tasks"] if t["path"] == "dev/tasks/old-cleanup")
    assert old["answers"]["orientation"]["state"] == "invalid"


def test_the_export_reports_a_catalog_that_cannot_load(world, tmp_path, monkeypatch):
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    (fleet / "broken.yaml").write_text("id: broken\n", encoding="utf-8")
    monkeypatch.setattr(questions, "default_fleet_dir", lambda: fleet)
    arguments = inject()
    arguments.pop("catalog")
    code, doc = document(["tasks", "--format", "json", "--protocol", "1", "--parent", str(world)], **arguments)
    assert code == 0
    payload = doc["payload"]
    [error] = payload["errors"]
    assert error["code"] == "questions" and "broken.yaml" in error["message"]
    assert all(t["answers"] == {} and t["coverage"] is None for t in payload["tasks"])


def test_the_export_writes_nothing_for_answers(world):
    def listing_of(root: Path):
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if ".git" not in p.parts)

    before = listing_of(world)
    tasks_payload(world)
    assert listing_of(world) == before
    assert EXPORT_MACHINE.host == "thismac"
    json.dumps(tasks_payload(world))
