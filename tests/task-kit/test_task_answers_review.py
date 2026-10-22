"""U7: ``task.py answers``, ``review`` through the answers engine, ``update --summary``'s note, the review HTML for
answer states, and answer notes in ``validate`` (summary-on-commit-plan.md, "Contract changes" and the U7 row).

The world is the real topology: a project directory whose ``dev/tasks`` is a symlink into a tasks repository cloned
from a bare remote on local disk. A fake ``declaration.run`` answers; no model is called.

Revert checks (each named test goes red when the named change is made; recorded in the unit's report):
- ``summary_ops.answer_missing_orientations`` also writing ``summary`` through ``state_ops.update`` (the legacy write)
  -> ``test_review_answers_through_the_engine_and_leaves_task_yaml_and_log_md_byte_identical``
- ``answers_cli.main`` replaced by another entry in ``_cmd_answers`` -> ``test_answers_forwards_its_arguments_to_the_answers_cli``
- ``_answer_file_notes`` emitted as warnings (they gate ``work``) -> ``test_an_invalid_answer_file_is_a_note_and_never_gates_work``
- the invalid, not-applicable or stale-answer branch of ``review_html._render_task`` removed -> ``test_review_html_renders_every_answer_state``
- ``orientation_supersedes_summary`` returning False -> ``test_update_summary_notes_that_an_orientation_answer_supersedes_it``
- ``_wants_orientation`` accepting a current answer -> ``test_a_listing_with_nothing_wanted_touches_neither_git_nor_llm_kit``
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path

import pytest
import yaml
from dashboard_support import ENTRYPOINT, TASK_CLI, clean_env
from git_remote_support import clone, git, make_remote, remote_files, write
from test_task_answer_engine import (  # noqa: F401 -- fixtures
    CONFIG, LLM_KIT_LIB, ME, NOW, ORIENTATION, FakeRun, NeverRun, isolated_git, llm, single_question_catalog,
)

from task_system import answer_store, listing, review_html, state_ops, summary_ops
from task_system.review_html import render_review_html
from task_system.validate import validate_ref

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fixture links dev/tasks with a real symlink")

SKIP_REEXEC = "_BOOTSTRAP_GUARD_SKIP_REEXEC"
SKILLS_KIT = LLM_KIT_LIB.parents[1] / "skills-kit"
PLAN = "# Plan\n\n```yaml\ntask_items:\n  items: []\n```\n"
LOG = "# Log\n\n- 2026-10-01: update: created\n"


class Project:
    """``root/dev/tasks`` -> ``top/proj`` in a tasks repository cloned from a bare remote."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.remote = make_remote(tmp_path)
        self.top = clone(tmp_path, self.remote, "tasks")
        self.root = tmp_path / "project"
        (self.root / "dev").mkdir(parents=True)
        (self.top / "proj").mkdir()
        os.symlink(self.top / "proj", self.root / "dev" / "tasks", target_is_directory=True)
        self.data = tmp_path / "data"
        self.catalog = single_question_catalog(tmp_path)

    def add(self, stub: str, status: str = "active") -> Path:
        block = {"_schema_version": "1", "type": "hand-off", "title": stub, "status": status}
        write(self.top, f"proj/{stub}/task.yaml", yaml.safe_dump({"task": block}))
        write(self.top, f"proj/{stub}/CLAUDE.md", f"Notes of {stub}.\n")
        write(self.top, f"proj/{stub}/plan.md", PLAN)
        write(self.top, f"proj/{stub}/log.md", LOG)
        git(self.top, "add", "--", f"proj/{stub}")
        git(self.top, "commit", "-q", "-m", f"add {stub}")
        return self.top / "proj" / stub

    def publish(self) -> None:
        git(self.top, "push", "-q", "origin", "HEAD:main")

    def folder(self, stub: str) -> Path:
        return self.top / "proj" / stub

    def listing(self):
        return listing.collect_listing("project", self.root, catalog=self.catalog, now=NOW)

    def adapter(self, run, **extra):
        """``answer_missing_orientations`` with this world's machine, config, catalog and data directory."""
        return partial(
            summary_ops.answer_missing_orientations, allow=["free"], run=run, config=CONFIG, catalog=self.catalog,
            machine=ME, data_dir=self.data, now=lambda: NOW, **extra,
        )

    def tracked_bytes(self, *stubs: str) -> dict[str, bytes]:
        return {
            f"{stub}/{name}": (self.folder(stub) / name).read_bytes()
            for stub in stubs
            for name in ("task.yaml", "log.md")
        }


@pytest.fixture
def project(tmp_path) -> Project:
    world = Project(tmp_path)
    for stub in ("a", "b", "c"):  # committed in this order: c is the most recently modified
        world.add(stub)
    world.publish()
    return world


@pytest.fixture(scope="module")
def task_cli():
    """task.py loaded in process (the re-exec guard is disarmed by the suite's conftest)."""
    spec = importlib.util.spec_from_file_location("task_cli_under_test", TASK_CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def review(task_cli, project: Project, monkeypatch, run, *extra: str, **adapter):
    monkeypatch.setattr(summary_ops, "answer_missing_orientations", project.adapter(run, **adapter))
    output = project.tmp_path / "review.html"
    code = task_cli.main(
        ["review", "--scope", "project", "--no-open", "--output", str(output), "--root", str(project.root), *extra]
    )
    return code, output.read_text(encoding="utf-8")


# --- review -----------------------------------------------------------------------------------------------------


def test_review_answers_through_the_engine_and_leaves_task_yaml_and_log_md_byte_identical(
    project, task_cli, monkeypatch, llm, capsys
):
    before = project.tracked_bytes("a", "b", "c")
    run = FakeRun(llm)
    code, html = review(task_cli, project, monkeypatch, run)
    assert code == 0 and run.calls == 3
    assert project.tracked_bytes("a", "b", "c") == before  # task.yaml and log.md: byte for byte
    assert "summary" not in yaml.safe_load((project.folder("c") / "task.yaml").read_text(encoding="utf-8"))["task"]
    for stub in ("a", "b", "c"):  # this machine's answer files, committed and published
        assert f"proj/{stub}/answers/orientation/studio.yaml" in remote_files(project.remote)
    assert git(project.top, "status", "--porcelain").stdout == ""
    assert "A headline" in html and "Summary is missing." not in html  # the rendered summary is the headline
    states = {view.id: view.summary_status for view in project.listing().views}
    assert states == {"dev/tasks/a": "present", "dev/tasks/b": "present", "dev/tasks/c": "present"}
    assert capsys.readouterr().err.count("error:") == 0


def test_a_current_answer_is_not_answered_again(project, task_cli, monkeypatch, llm):
    review(task_cli, project, monkeypatch, FakeRun(llm))
    head = git(project.top, "rev-parse", "HEAD").stdout
    code, _html = review(task_cli, project, monkeypatch, NeverRun())  # nothing wanted: no model, no git write
    assert code == 0 and git(project.top, "rev-parse", "HEAD").stdout == head


def test_a_stale_answer_is_answered_again_through_the_engine_not_the_legacy_summary(
    project, task_cli, monkeypatch, llm
):
    review(task_cli, project, monkeypatch, FakeRun(llm))
    write(project.top, "proj/b/plan.md", PLAN + "\nMore.\n")
    git(project.top, "commit", "-q", "-m", "edit b", "--", "proj/b/plan.md")
    stale = {view.id: view for view in project.listing().views}["dev/tasks/b"]
    assert (stale.summary_status, stale.summary_source) == ("stale", "answer")
    before = project.tracked_bytes("a", "b", "c")
    run = FakeRun(llm)
    code, _html = review(task_cli, project, monkeypatch, run)
    assert code == 0 and run.calls == 1  # b only
    assert project.tracked_bytes("a", "b", "c") == before
    assert {view.id: view.summary_status for view in project.listing().views}["dev/tasks/b"] == "present"


def test_no_generate_missing_summaries_asks_nothing_and_writes_nothing(project, task_cli, monkeypatch):
    head = git(project.top, "rev-parse", "HEAD").stdout
    code, html = review(task_cli, project, monkeypatch, NeverRun(), "--no-generate-missing-summaries")
    assert code == 0 and git(project.top, "rev-parse", "HEAD").stdout == head
    assert html.count("Summary is missing.") == 3
    assert not (project.folder("a") / "answers").exists()


def test_a_deferred_task_is_answered_like_any_eligible_task(project, task_cli, monkeypatch, llm):
    project.add("on-hold", status="deferred")
    report = project.adapter(FakeRun(llm))(project.listing(), project.root)
    assert "dev/tasks/on-hold" in report.answered and report.failed == ()


def test_a_task_folder_with_uncommitted_changes_is_a_note_not_a_failure(project, task_cli, monkeypatch, llm, capsys):
    write(project.top, "proj/b/CLAUDE.md", "Edited and not committed.\n")
    run = FakeRun(llm)
    code, _html = review(task_cli, project, monkeypatch, run)
    err = capsys.readouterr().err
    assert code == 0 and run.calls == 2  # a and c
    assert "note: dev/tasks/b: skipped: the task folder has uncommitted changes outside answers/" in err
    assert not (project.folder("b") / "answers").exists()


def test_model_answering_unavailable_is_reported_once_and_exits_nonzero(project, task_cli, monkeypatch, capsys):
    # no `llm` fixture: llm-kit is hidden from this test, as on a machine without it
    code, html = review(task_cli, project, monkeypatch, None)
    err = capsys.readouterr().err
    assert code == 1 and err.count("error: model answering unavailable:") == 1
    assert html.count("Summary is missing.") == 3
    assert not (project.folder("a") / "answers").exists()


def test_a_failed_model_call_names_the_task_and_exits_nonzero(project, task_cli, monkeypatch, llm, capsys):
    code, _html = review(task_cli, project, monkeypatch, FakeRun(llm, fail="boom"))
    err = capsys.readouterr().err
    assert code == 1 and err.count("error: could not answer the orientation of dev/tasks/") == 3


def test_a_listing_with_nothing_wanted_touches_neither_git_nor_llm_kit(project, llm, tmp_path):
    project.adapter(FakeRun(llm))(project.listing(), project.root)  # every task answered
    head = git(project.top, "rev-parse", "HEAD").stdout
    # no injected machine, config, catalog or run: nothing is wanted, so none of them is read
    report = summary_ops.answer_missing_orientations(project.listing(), project.root)
    assert report == summary_ops.ReviewAnswerReport() and git(project.top, "rev-parse", "HEAD").stdout == head


# --- the review HTML ----------------------------------------------------------------------------------------------


def _task(**fields):
    base = {
        "key": "t", "id": "t", "title": "T", "status": "active", "last_update": "2026-10-01", "last_activity": None,
        "summary": None, "summary_status": "missing", "summary_missing": True, "summary_source": None,
    }
    return {**base, **fields}


def _html(task: dict) -> str:
    data = {
        "tasks": [task], "diagnostics": [], "notes": [], "scope": "project",
        "projects": [{"name": "p", "root": "/p", "last_update": None, "last_activity": None, "tasks": ["t"],
                      "sections": {"open": ["t"], "deferred": [], "closed": [], "other": []}}],
    }
    return render_review_html(data, now=NOW)


def test_review_html_renders_every_answer_state():
    invalid = _html(_task(summary_status="invalid"))
    assert 'class="task-card invalid"' in invalid and "Orientation answer file is invalid" in invalid
    na = _html(_task(summary_status="not-applicable", summary_missing=False))
    assert 'class="task-card unavailable"' in na and "No orientation answer applies to this task's status." in na
    stale = _html(_task(summary="Where <it> stands", summary_status="stale", summary_source="answer"))
    assert 'class="task-card stale"' in stale and "Where &lt;it&gt; stands" in stale and "(stale:" in stale
    legacy = _html(_task(summary="Old text", summary_status="stale", summary_source="legacy"))
    assert 'class="task-card missing"' in legacy and "Summary is stale and needs regeneration." in legacy
    assert ".task-card.invalid .summary-card" in invalid and ".task-card.stale .summary-card" in invalid


def test_review_renders_an_invalid_answer_file_from_a_real_listing(project, task_cli, monkeypatch, llm):
    path = answer_store.record_path(project.folder("a"), "orientation", "studio")
    path.parent.mkdir(parents=True)
    path.write_text("not: [valid", encoding="utf-8")
    view = {v.id: v for v in project.listing().views}["dev/tasks/a"]
    assert view.summary_status == "invalid"
    html = render_review_html(listing.listing_data(project.listing()), now=NOW)
    assert "Orientation answer file is invalid" in html


# --- update --summary ---------------------------------------------------------------------------------------------


def test_update_summary_notes_that_an_orientation_answer_supersedes_it(project, task_cli, monkeypatch, llm, capsys):
    review(task_cli, project, monkeypatch, FakeRun(llm))
    capsys.readouterr()
    code = task_cli.main(["update", "dev/tasks/a", "--summary", "A legacy line", "--root", str(project.root)])
    captured = capsys.readouterr()
    assert code in (0, 1)  # the write persisted either way; findings (the dirtied folder) set the exit code
    block = yaml.safe_load((project.folder("a") / "task.yaml").read_text(encoding="utf-8"))["task"]
    assert block["summary"] == "A legacy line"  # the write is unchanged
    assert "note: task.summary was written, but this task's orientation answer supersedes it" in captured.err


def test_update_summary_prints_no_note_without_an_orientation_answer(project, task_cli, capsys):
    task_cli.main(["update", "dev/tasks/a", "--summary", "A legacy line", "--root", str(project.root)])
    block = yaml.safe_load((project.folder("a") / "task.yaml").read_text(encoding="utf-8"))["task"]
    assert block["summary"] == "A legacy line"
    assert "supersedes" not in capsys.readouterr().err
    view = {v.id: v for v in project.listing().views}["dev/tasks/a"]
    assert (view.summary, view.summary_source) == ("A legacy line", "legacy")


# --- validate -----------------------------------------------------------------------------------------------------


def test_an_invalid_answer_file_is_a_note_and_never_gates_work(project):
    folder = project.folder("a")
    assert validate_ref("dev/tasks/a", project.root).clean
    path = answer_store.record_path(folder, "orientation", "studio")
    path.parent.mkdir(parents=True)
    path.write_text("not: [valid", encoding="utf-8")
    git(project.top, "add", "--", "proj/a/answers")
    git(project.top, "commit", "-q", "-m", "a broken answer file")  # committed: the folder is clean
    result = validate_ref("dev/tasks/a", project.root)
    assert result.clean and result.errors == [] and result.warnings == []
    assert any(note.startswith("invalid answer file: ") and "studio.yaml" in note for note in result.notes)
    worked = state_ops.work("dev/tasks/a", project.root)  # the gate: ANY error or warning blocks
    assert worked.canonical == "dev/tasks/a"


def test_a_file_that_is_not_a_record_name_is_not_noted(project):
    folder = project.folder("a")
    directory = answer_store.answers_dir(folder, "orientation")
    directory.mkdir(parents=True)
    (directory / ".studio.yaml.tmp").write_text("partial", encoding="utf-8")
    (directory / "README.txt").write_text("not a record", encoding="utf-8")
    assert not [n for n in validate_ref("dev/tasks/a", project.root).notes if "answer file" in n]


# --- answers ------------------------------------------------------------------------------------------------------


def test_answers_forwards_its_arguments_to_the_answers_cli(task_cli, monkeypatch):
    from task_system import answers_cli  # noqa: PLC0415

    seen: list[list[str]] = []
    monkeypatch.setattr(answers_cli, "main", lambda argv=None: seen.append(list(argv)) or 7)
    argv = ["status", "--format", "json", "--protocol", "1", "--parent", "/p"]
    assert task_cli.main(["answers", *argv]) == 7
    assert seen == [argv]


def run_script(script: Path, *args: str, home: Path) -> subprocess.CompletedProcess:
    env = clean_env({SKIP_REEXEC: "1", "PYTHONPATH": str(SKILLS_KIT), "HOME": str(home), "USERPROFILE": str(home)})
    return subprocess.run([sys.executable, str(script), *args], env=env, capture_output=True, text=True, timeout=120)


def test_task_py_answers_and_task_kit_answers_are_one_implementation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    for args in (
        ("status", "--format", "json", "--protocol", "1", "--bogus"),  # a usage error document
        ("status", "--format", "json"),  # no protocol
        ("status", "--format", "json", "--protocol", "99"),  # unsupported protocol
        ("frobnicate", "--format", "json", "--protocol", "1"),
    ):
        via_task = run_script(TASK_CLI, "answers", *args, home=home)
        via_kit = run_script(ENTRYPOINT, "answers", *args, home=home)
        assert via_task.returncode == via_kit.returncode != 0, args
        assert json.loads(via_task.stdout) == json.loads(via_kit.stdout), args
        assert via_task.stdout.count("\n") == 1  # exactly one document and a newline
