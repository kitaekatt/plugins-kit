"""``task-kit answers``: refresh, status and prune under the export's output, exit-code and error-code rules
(databench-integration-plan.md, M5: "U4 adds task-kit answers refresh ... and task-kit answers status ...").

The world is the real topology: a project directory whose ``dev/tasks`` is a symlink into a tasks repository
(cloned from a bare remote on local disk). A fake ``declaration.run`` answers; no model is called.

Revert checks (each named test goes red when the named change is made; recorded in the unit's report):
- the protocol check removed from ``answers_cli.document`` -> ``test_an_unsupported_protocol_is_refused_with_the_exports_error_shape``
- the stop file ignored -> ``test_a_stop_file_ends_the_pass_before_it_starts``
- ``--deadline-s`` ignored -> ``test_a_zero_deadline_ends_the_pass_before_it_starts``
- ``status`` committing the dirty own files -> ``test_status_reports_coverage_and_pending_without_writing``
- ``--allow`` ignored -> ``test_allow_names_the_classes_and_wins_over_consent``
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dashboard_support import ENTRYPOINT, clean_env
from git_remote_support import git, remote_files
from test_task_answer_engine import (  # noqa: F401 -- fixtures
    CONFIG, LLM_KIT_LIB, ME, NOW, FakeRun, NeverRun, World, isolated_git, llm, new_answer, world,
)

from task_system import answer_store, answers_cli, export_cli, questions

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fixture links dev/tasks with a real symlink")

SKIP_REEXEC = "_BOOTSTRAP_GUARD_SKIP_REEXEC"
SKILLS_KIT = LLM_KIT_LIB.parents[1] / "skills-kit"


@pytest.fixture
def parent(world: World) -> Path:
    """``parent/proj/dev/tasks`` -> the tasks repository's ``proj`` directory."""
    root = world.tmp_path / "parent"
    (root / "proj" / "dev").mkdir(parents=True)
    os.symlink(world.top / "proj", root / "proj" / "dev" / "tasks", target_is_directory=True)
    return root


def call(world: World, parent: Path, *argv: str, run=None, **inject):
    """``(exit code, parsed stdout, raw stdout, stderr)`` of ``task-kit answers <argv>``."""
    out, err = io.StringIO(), io.StringIO()
    inject.setdefault("budget", {"parallel": 1})
    inject.setdefault("data_dir", world.data)
    code = answers_cli.run(
        list(argv), stdout=out, stderr=err, resolve_machine=lambda: ME, catalog=world.catalog, config=CONFIG,
        run=run, local_now=lambda: NOW, **inject,
    )
    text = out.getvalue()
    return code, json.loads(text), text, err.getvalue()


def refresh(world, parent, *extra, run=None, **inject):
    return call(world, parent, "refresh", "--format", "json", "--protocol", "1", "--parent", str(parent), *extra,
                run=run, **inject)


def only_repository(doc: dict) -> dict:
    [repository] = doc["payload"]["repositories"]
    return repository


# --- refresh --------------------------------------------------------------------------------------------------------


def test_refresh_writes_one_document_and_answers_the_pending_tasks(world, parent, llm):
    run = FakeRun(llm)
    code, doc, text, stderr = refresh(world, parent, "--allow", "free", run=run)
    assert code == 0 and stderr == ""
    assert text.endswith("\n") and text.count("\n") == 1  # exactly one document and a newline
    assert (doc["protocol"], doc["kind"]) == (1, "answers-pass") and doc["task_kit_version"]
    repository = only_repository(doc)
    assert repository["outcome"] == "completed" and repository["repo"] == str(world.top.resolve())
    assert {item["task"] for item in repository["answered"]} == {"proj/a", "proj/b", "proj/c"}
    assert doc["payload"]["stopped"] is None and doc["payload"]["errors"] == []
    assert run.calls == 3
    assert "proj/c/answers/orientation/studio.yaml" in remote_files(world.remote)


def test_the_batch_flag_limits_the_tasks_of_the_pass(world, parent, llm):
    code, doc, _text, _err = refresh(world, parent, "--allow", "free", "--batch", "1", run=FakeRun(llm))
    assert code == 0
    repository = only_repository(doc)
    assert repository["kept"] == ["proj/c"] and repository["pending"] == 3
    assert [item["task"] for item in repository["answered"]] == ["proj/c"]


def test_a_stop_file_ends_the_pass_before_it_starts(world, parent):
    stop = world.tmp_path / "stop"
    stop.write_text("", encoding="utf-8")
    code, doc, _text, _err = refresh(world, parent, "--allow", "free", "--stop-file", str(stop), run=NeverRun())
    assert code == 0 and doc["payload"]["stopped"] == "stop"
    assert only_repository(doc)["outcome"] == "stopped"
    assert not (world.folder("c") / "answers").exists()


def test_a_zero_deadline_ends_the_pass_before_it_starts(world, parent):
    code, doc, _text, _err = refresh(world, parent, "--allow", "free", "--deadline-s", "0", run=NeverRun())
    assert code == 0 and doc["payload"]["stopped"] == "deadline"
    assert only_repository(doc)["stopped_before"] == "prepare"


def test_a_deadline_that_passes_during_answering_leaves_the_rest_unattempted(world, parent, llm):
    clock = {"now": 0.0}
    run = FakeRun(llm, on_call=lambda _request: clock.update(now=1000.0))
    code, doc, _text, _err = refresh(
        world, parent, "--allow", "free", "--deadline-s", "100", run=run, clock=lambda: clock["now"]
    )
    repository = only_repository(doc)
    assert code == 0 and doc["payload"]["stopped"] == "deadline" and run.calls == 1
    assert [item["task"] for item in repository["answered"]] == ["proj/c"]
    assert {item["task"] for item in repository["unattempted"]} == {"proj/a", "proj/b"}


def test_allow_names_the_classes_and_wins_over_consent(world, parent, llm, monkeypatch, tmp_path):
    from llm_kit.registry import consent  # noqa: PLC0415

    path = tmp_path / "consent.json"
    consent.save_consent(usage=False, spend=True, path=path)
    monkeypatch.setattr(consent, "default_consent_path", lambda: path)
    _code, doc, _text, _err = refresh(world, parent, "--allow", "free,usage", "--batch", "1", run=FakeRun(llm))
    assert doc["payload"]["allow"] == {"classes": ["free", "usage"], "source": "explicit", "note": ""}
    # without --allow the classes are llm-kit's consent: spend, not usage, plus free
    _code, doc, _text, _err = refresh(world, parent, "--batch", "1", run=FakeRun(llm))
    assert doc["payload"]["allow"]["source"] == "consent"
    assert doc["payload"]["allow"]["classes"] == ["free", "spend"]


def test_a_pass_with_nothing_to_answer_resolves_no_consent(world, parent, llm):
    refresh(world, parent, "--allow", "free", run=FakeRun(llm))
    code, doc, _text, stderr = refresh(world, parent, run=NeverRun())  # no llm-kit needed, nothing pending
    assert code == 0 and doc["payload"]["allow"] is None and stderr == ""
    assert only_repository(doc)["pending"] == 0


def test_without_llm_kit_the_document_reports_the_refusal_and_stderr_names_the_remedy(world, parent):
    code, doc, _text, stderr = refresh(world, parent, "--allow", "free", run=NeverRun())
    repository = only_repository(doc)
    assert code == 0 and repository["outcome"] == "refused" and repository["refusal"]["state"] == "absent"
    assert "claude plugin install llm-kit" in repository["refusal"]["message"]
    assert "model answering unavailable (absent)" in stderr
    assert repository["answered"] == []


def test_a_task_outside_git_is_an_error_entry_not_a_failure(world, parent):
    loose = world.tmp_path / "parent" / "loose" / "dev" / "tasks" / "x"
    loose.mkdir(parents=True)
    (loose / "task.yaml").write_text("task:\n  _schema_version: '1'\n  type: hand-off\n  title: x\n  status: active\n")
    code, doc, _text, _err = refresh(world, parent, "--allow", "free", run=NeverRun())
    assert code == 0
    assert [e["code"] for e in doc["payload"]["errors"]] == ["answers"]
    assert "not inside a git work tree" in doc["payload"]["errors"][0]["message"]


# --- status -----------------------------------------------------------------------------------------------------------


def test_status_reports_coverage_and_pending_without_writing(world, parent, llm):
    refresh(world, parent, "--allow", "free", "--batch", "1", run=FakeRun(llm))  # c answered
    folder = world.folder("b")  # an answer written but never committed
    answer_store.write_answer(folder, "orientation", ME, new_answer(folder, world.catalog))
    before = world.snapshot()
    fresh_data = world.tmp_path / "data-for-status"
    code, doc, _text, _err = call(
        world, parent, "status", "--format", "json", "--protocol", "1", "--parent", str(parent), data_dir=fresh_data
    )
    assert code == 0 and doc["kind"] == "answers-status"
    assert world.snapshot() == before and not fresh_data.exists()  # no commit, no lock file
    payload = doc["payload"]
    assert payload["catalog_version"] == 1 and payload["questions"] == ["orientation"]
    assert payload["totals"] == {
        "tasks": 3, "applicable": 3, "current": 2, "pending_tasks": 1, "pending_questions": 1, "unpublished": 0,
    }
    repository = only_repository(doc)
    assert repository["own_uncommitted"] == ["proj/b/answers/orientation/studio.yaml"]
    tasks = {task["task"]: task for task in repository["tasks"]}
    assert tasks["proj/c"]["states"] == {"orientation": "current"} and tasks["proj/c"]["pending"] == []
    assert tasks["proj/b"]["states"] == {"orientation": "current"}
    assert tasks["proj/a"]["states"] == {"orientation": "missing"} and tasks["proj/a"]["pending"] == ["orientation"]
    assert tasks["proj/a"]["status"] == "active"
    assert tasks["proj/a"]["coverage"] == {"catalog_version": 1, "applicable": 1, "current": 0}


def test_status_serves_model_answering_availability(world, parent, llm):
    _code, doc, _text, _err = call(world, parent, "status", "--format", "json", "--protocol", "1", "--parent", str(parent))
    assert doc["payload"]["model_answering"] == {"available": True, "state": "ok", "message": None}


def test_status_names_a_missing_llm_kit_and_its_remedy(world, parent):
    _code, doc, _text, _err = call(world, parent, "status", "--format", "json", "--protocol", "1", "--parent", str(parent))
    answering = doc["payload"]["model_answering"]
    assert (answering["available"], answering["state"]) == (False, "absent")
    assert "claude plugin install llm-kit" in answering["message"]


# --- prune ------------------------------------------------------------------------------------------------------------


def test_prune_reports_the_files_it_removed(world, parent, llm):
    refresh(world, parent, "--allow", "free", run=FakeRun(llm))
    retired = world.folder("c") / "answers" / "retired_q"
    retired.mkdir()
    own = world.folder("c") / "answers" / "orientation" / "studio.yaml"
    (retired / "studio.yaml").write_text(own.read_text(encoding="utf-8").replace("orientation", "retired_q"))
    git(world.top, "add", "--", "proj/c/answers/retired_q/studio.yaml")
    git(world.top, "commit", "-q", "-m", "retired", "--", "proj/c/answers/retired_q/studio.yaml")
    code, doc, _text, _err = call(world, parent, "prune", "--format", "json", "--protocol", "1", "--parent", str(parent))
    repository = only_repository(doc)
    assert code == 0 and doc["kind"] == "answers-prune"
    assert repository["pruned"] == ["proj/c/answers/retired_q/studio.yaml"] and repository["commit"]
    assert not retired.exists() and own.exists()


# --- the export's rules -------------------------------------------------------------------------------------------------


def test_an_unsupported_protocol_is_refused_with_the_exports_error_shape(world, parent):
    for verb in ("refresh", "status", "prune"):
        code, doc, text, _err = call(world, parent, verb, "--format", "json", "--protocol", "2")
        assert code == 4 and text.count("\n") == 1
        assert doc["protocol"] == 1 and doc["error"]["code"] == "protocol_unsupported"
    export_code, export_doc = export_cli.document(["tasks", "--format", "json", "--protocol", "2"])
    assert (export_code, export_doc) == (code, doc)  # the same code, the same document


@pytest.mark.parametrize(
    "argv, fragment",
    [
        (["status", "--format", "json"], "requires --protocol"),
        (["status", "--format", "json", "--protocol", "1", "--batch", "2"], "takes no --batch"),
        (["prune", "--format", "json", "--protocol", "1", "--allow", "free"], "takes no --allow"),
        (["refresh", "--format", "json", "--protocol", "1", "--batch", "0"], "--batch must be at least 1"),
        (["refresh", "--format", "json", "--protocol", "1", "--deadline-s", "-1"], "must not be negative"),
        (["refresh", "--format", "json", "--protocol", "1", "--allow", "gold"], "unknown cost class"),
        (["refresh", "--protocol", "1"], "--format"),
        (["frobnicate", "--format", "json", "--protocol", "1"], "invalid choice"),
    ],
)
def test_usage_errors_are_documents_with_exit_2(world, parent, argv, fragment):
    code, doc, text, stderr = call(world, parent, *argv)
    assert code == 2 and text.count("\n") == 1
    assert doc["error"]["code"] == "usage" and fragment in doc["error"]["message"]
    assert stderr.startswith("task-kit answers: usage:")


def test_a_bad_parent_is_a_discovery_failure(world, parent):
    code, doc, _text, _err = call(
        world, parent, "status", "--format", "json", "--protocol", "1", "--parent", str(world.tmp_path / "missing")
    )
    assert code == 1 and doc["error"]["code"] == "discovery_failed"


def test_an_unreadable_catalog_is_a_config_error_naming_the_file(world, parent, tmp_path, monkeypatch):
    broken = tmp_path / "fleet"
    broken.mkdir()
    (broken / "orientation.yaml").write_text("not: [valid", encoding="utf-8")
    monkeypatch.setattr(questions, "default_fleet_dir", lambda: broken)
    out = io.StringIO()
    code = answers_cli.run(
        ["status", "--format", "json", "--protocol", "1", "--parent", str(parent)], stdout=out, stderr=io.StringIO(),
        resolve_machine=lambda: ME, data_dir=world.data,
    )
    doc = json.loads(out.getvalue())
    assert code == 1 and doc["error"]["code"] == "config_invalid"
    assert str(broken / "orientation.yaml") in doc["error"]["message"]


# --- through the launcher -----------------------------------------------------------------------------------------------


def run_entrypoint(*args: str) -> subprocess.CompletedProcess:
    env = clean_env({SKIP_REEXEC: "1", "PYTHONPATH": str(SKILLS_KIT)})  # skills_kit_lib, as the repo's pytest config adds it
    return subprocess.run([sys.executable, str(ENTRYPOINT), *args], env=env, capture_output=True, text=True, timeout=120)


def test_the_launcher_routes_answers_to_the_cli():
    done = run_entrypoint("answers", "status", "--format", "json", "--protocol", "2")
    doc = json.loads(done.stdout)
    assert done.returncode == 4 and doc["error"]["code"] == "protocol_unsupported"
    assert done.stdout.count("\n") == 1


def test_the_launcher_answers_a_usage_error_with_a_document():
    done = run_entrypoint("answers", "refresh", "--format", "json")
    assert done.returncode == 2 and json.loads(done.stdout)["error"]["code"] == "usage"


def test_the_retired_message_lists_the_answers_command():
    done = run_entrypoint("frobnicate")
    assert done.returncode == 2 and "task-kit answers ..." in done.stderr
