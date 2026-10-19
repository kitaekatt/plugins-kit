"""Translating one task item into one llm-kit job, or refusing with typed reasons.

The user's ruling is to block the task and fix the wording rather than build a
job system robust to any wording, so these tests pin the refusals as hard as the
success: every reason is typed, a refusal creates nothing, and the deny floor
sits where llm-kit will actually apply it.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from dashboard_support import REPO_ROOT, FakeEdge, agent_world

from task_system.dashboard import floor, translate
from task_system.dashboard.config import DashboardConfig, DashboardConfigError, load_config
from task_system.dashboard.mutation import folder_revision
from task_system.dashboard.scheduler import ACCEPT_SCRIPT
from task_system.dashboard.translate import RunOptions, translate as do_translate

TASK = "tmp/alpha"


def run_translate(project, *, item="do-it", options=None, edge=None, held=None, config=None, tmp_path=None):
    folder = project / TASK
    run_dir = (tmp_path or project.parent) / "run-dir-that-must-not-appear"
    result = do_translate(
        project_root=project, task_ref=TASK, item_id=item, options=options or RunOptions(),
        config=config or DashboardConfig(), edge=edge or FakeEdge(), hold_for=lambda f: held, run_id="r1",
        run_dir=run_dir, accept_script=ACCEPT_SCRIPT, revision=folder_revision(folder))
    assert not run_dir.exists(), "translation must create nothing"
    return result


def codes(result):
    return [r.code for r in result.reasons]


@pytest.fixture
def world(tmp_path):
    _devroot, project, folder = agent_world(tmp_path)
    return tmp_path, project, folder


def rewrite(folder, name, old, new):
    path = folder / name
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


# -- success -----------------------------------------------------------------


def test_a_ready_item_becomes_one_immutable_job_specification(world):
    tmp_path, project, folder = world
    result = run_translate(project, tmp_path=tmp_path)
    assert result.ready and result.reasons == ()
    spec = result.spec
    (job,) = spec.document["jobs"]
    assert spec.document["max_parallel"] == 1 and "disallowed_tools" not in spec.document
    assert job["id"] == "tmp-alpha--do-it--r1"
    assert job["models"] == ["opus"] and job["max_attempts"] == 1
    assert job["directory"] == str(project) and job["workspace"] == {"isolate": False}
    assert job["options"]["system_prompt_mode"] == "append"
    command = job["contract"]["command"]
    assert command[0] == sys.executable and Path(command[0]).is_absolute()
    assert Path(command[1]).is_absolute() and Path(command[1]).name == "accept_cli.py"
    assert command[2:6] == [TASK, "do-it", "r1", folder_revision(folder)]
    assert command[6] == spec.start.head and command[7] == "main"
    with pytest.raises(Exception):
        spec.item_id = "other"  # frozen
    system, user = job["prompt"]["system"], job["prompt"]["user"]
    assert str(project) in system and TASK in system and "do-it" in system and "Do not start sibling items" in system
    assert "task_result" in system and "Native sub-agents are not available" in system
    assert "Make the thing." in user and "id: do-it" in user and "Later one" not in user  # only this item
    assert "task work" not in user and "Skill(" not in user


def test_accept_argv_lists_ride_along_and_are_never_shell_interpreted(world):
    tmp_path, project, folder = world
    rewrite(folder, "plan.md", "    priority: P1\n", "    priority: P1\n    accept:\n    - - uv\n      - run\n      - pytest\n      - -q\n")
    result = run_translate(project, tmp_path=tmp_path)
    assert result.ready and result.spec.accept == (("uv", "run", "pytest", "-q"),)


# -- refusals: the item --------------------------------------------------------


def test_a_missing_plan_section_is_a_typed_blocking_reason(world):
    tmp_path, project, folder = world
    rewrite(folder, "plan.md", "### do-it -- section", "### other-thing -- section")
    result = run_translate(project, tmp_path=tmp_path)
    assert not result.ready and result.spec is None
    assert codes(result) == ["section-missing"] and result.reasons[0].blocking


def test_a_duplicate_or_empty_section_is_refused_and_titles_never_match_fuzzily(world):
    tmp_path, project, folder = world
    plan = (folder / "plan.md").read_text(encoding="utf-8")
    (folder / "plan.md").write_text(plan + "\n### do-it -- again\n\nMore.\n", encoding="utf-8")
    assert codes(run_translate(project, tmp_path=tmp_path)) == ["section-duplicate"]
    (folder / "plan.md").write_text(plan.replace("Make the thing.\n\nDone when it exists.\n", "   \n"), encoding="utf-8")
    assert "section-empty" in codes(run_translate(project, tmp_path=tmp_path))
    (folder / "plan.md").write_text(plan.replace("### do-it -- section", "### Do it"), encoding="utf-8")  # the TITLE, not the id
    assert codes(run_translate(project, tmp_path=tmp_path)) == ["section-missing"]


@pytest.mark.parametrize("edit,expected", [
    (("state: available\n    priority: P1", "state: deferred\n    priority: P1"), "item-not-available"),
    (("state: available\n    priority: P1", "state: blocked-user\n    priority: P1\n    note: waiting"), "item-not-available"),
])
def test_only_an_available_item_runs(world, edit, expected):
    tmp_path, project, folder = world
    rewrite(folder, "plan.md", *edit)
    assert expected in codes(run_translate(project, tmp_path=tmp_path))


def test_an_unknown_item_and_a_non_active_task_are_refused(world):
    tmp_path, project, folder = world
    assert "item-missing" in codes(run_translate(project, item="nope", tmp_path=tmp_path))
    rewrite(folder, "task.yaml", "status: active", "status: blocked")
    assert "task-not-active" in codes(run_translate(project, tmp_path=tmp_path))


def test_a_task_with_validation_findings_is_refused_with_the_findings(world):
    tmp_path, project, folder = world
    rewrite(folder, "task.yaml", "title: Alpha", "title: Alpha\n  blocked_by:\n  - tmp/does-not-exist")
    result = run_translate(project, tmp_path=tmp_path)
    assert "validation" in codes(result) and any("does-not-exist" in r.detail for r in result.reasons)


@pytest.mark.parametrize("accept", [
    "    accept: not-a-list\n",
    "    accept: []\n",
    "    accept:\n    - []\n",
    "    accept:\n    - - ok\n      - 3\n",
    "    accept:\n    - echo hello world\n",  # a shell string, not an argv list
])
def test_malformed_accept_is_refused(world, accept):
    tmp_path, project, folder = world
    rewrite(folder, "plan.md", "    priority: P1\n", "    priority: P1\n" + accept)
    assert "bad-accept" in codes(run_translate(project, tmp_path=tmp_path))


# -- refusals: the worker and the model -----------------------------------------


def test_an_unproved_codex_floor_refuses_codex_workers(world, monkeypatch):
    """RED CHECK: Codex stays REFUSED until the live deny test has passed."""
    tmp_path, project, _folder = world
    result = run_translate(project, options=RunOptions(worker="codex"), tmp_path=tmp_path)
    assert "codex-floor-unproved" in codes(result) and not result.ready
    assert translate.CODEX_FLOOR_PROVED is False
    # The flag is the only gate: with it flipped (and nothing else changed) the same request is ready.
    monkeypatch.setattr(translate, "CODEX_FLOOR_PROVED", True)
    assert run_translate(project, options=RunOptions(worker="codex"), tmp_path=tmp_path).ready


@pytest.mark.parametrize("options,code", [
    (RunOptions(entry="or-qwen"), "model-entry"),       # an OpenRouter transport entry is not a task worker
    (RunOptions(entry="no-such-entry"), "model-entry"),
    (RunOptions(entry="sol"), "model-entry"),           # a codex entry on the claude worker
    (RunOptions(worker="gemini"), "bad-worker"),
])
def test_only_claude_and_codex_harness_entries_can_work_a_task(world, options, code):
    tmp_path, project, _folder = world
    result = run_translate(project, options=options, tmp_path=tmp_path)
    assert code in codes(result) and not result.ready


def test_llm_kit_unavailable_or_a_held_task_is_a_reason_that_blocks_nothing(world):
    tmp_path, project, _folder = world
    gone = run_translate(project, edge=FakeEdge(state="absent"), tmp_path=tmp_path)
    assert codes(gone) == ["llm-kit-unavailable"] and not gone.reasons[0].blocking
    held = run_translate(project, held="a Claude Code session has it", tmp_path=tmp_path)
    assert codes(held) == ["held"] and not held.reasons[0].blocking and not held.ready


def test_every_refusal_is_returned_together_not_one_at_a_time(world):
    tmp_path, project, folder = world
    rewrite(folder, "plan.md", "### do-it -- section", "### x -- section")
    rewrite(folder, "task.yaml", "status: active", "status: blocked")
    got = codes(run_translate(project, options=RunOptions(entry="or-qwen"), tmp_path=tmp_path))
    assert {"section-missing", "task-not-active", "model-entry"} <= set(got)


# -- configuration ---------------------------------------------------------------


def test_machine_defaults_come_from_task_local_yaml_and_bad_values_are_errors(tmp_path):
    path = tmp_path / "task.local.yaml"
    assert load_config(path) == DashboardConfig()  # absent file: defaults
    path.write_text("dashboard:\n  max_parallel_projects: 3\n  default_timeout_s: 60\n  claude_entry: sonnet\n  codex_network: true\n", encoding="utf-8")
    config = load_config(path)
    assert (config.max_parallel_projects, config.default_timeout_s, config.claude_entry, config.codex_network) == (3, 60.0, "sonnet", True)
    for bad in ("dashboard:\n  surprise: 1\n", "dashboard:\n  max_parallel_projects: 0\n", "dashboard:\n  default_worker: gemini\n",
                "dashboard:\n  default_timeout_s: yes\n", "dashboard: 3\n"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(DashboardConfigError, match="task.local.yaml"):
            load_config(path)


def test_per_run_options_override_the_machine_defaults(world):
    tmp_path, project, _folder = world
    config = DashboardConfig(claude_entry="opus", default_timeout_s=111.0)
    result = run_translate(project, options=RunOptions(entry="sonnet", timeout_s=5.0, effort="high"), config=config, tmp_path=tmp_path)
    (job,) = result.spec.document["jobs"]
    assert job["models"] == ["sonnet"] and result.spec.timeout_s == 5.0 and job["options"]["effort"] == "high"
    assert run_translate(project, config=config, tmp_path=tmp_path).spec.timeout_s == 111.0


# -- the deny floor and where it lives -------------------------------------------


def test_the_floor_names_every_family_the_design_denies():
    families = {
        "commit": "Bash(git commit:*)", "push": "Bash(git push:*)", "reset": "Bash(git reset:*)", "rebase": "Bash(git rebase:*)",
        "stash": "Bash(git stash:*)", "clean": "Bash(git clean:*)", "restore": "Bash(git restore:*)",
        "checkout": "Bash(git checkout:*)", "switch": "Bash(git switch:*)", "branch": "Bash(git branch:*)",
        "worktree": "Bash(git worktree:*)", "add": "Bash(git add:*)", "publish": "Bash(python scripts/publish.py:*)",
        "rm": "Bash(rm:*)", "claude": "Bash(claude -p:*)", "codex": "Bash(codex exec:*)", "task-kit": "Bash(task-kit:*)",
        "llm-kit": "Bash(llm-kit jobs:*)",
    }
    present = floor.CLAUDE_DISALLOWED_TOOLS.split(",")
    for name, pattern in families.items():
        assert pattern in present, name
    assert len(present) == len(set(present))


def good_document():
    return {"jobs": [{"id": "j", "options": {"disallowed_tools": floor.CLAUDE_DISALLOWED_TOOLS}}], "max_parallel": 1}


def test_the_floor_check_accepts_the_job_level_floor_and_refuses_anything_else():
    floor.check_job_document(good_document())
    run_level = good_document()
    run_level["disallowed_tools"] = floor.CLAUDE_DISALLOWED_TOOLS
    with pytest.raises(floor.FloorPlacementError, match="never on the job file"):
        floor.check_job_document(run_level)
    moved = {"jobs": [{"id": "j", "options": {}}], "disallowed_tools": floor.CLAUDE_DISALLOWED_TOOLS}
    with pytest.raises(floor.FloorPlacementError):
        floor.check_job_document(moved)
    thin = good_document()
    thin["jobs"][0]["options"]["disallowed_tools"] = "Bash(git commit:*)"
    with pytest.raises(floor.FloorPlacementError, match="missing deny patterns"):
        floor.check_job_document(thin)
    with pytest.raises(floor.FloorPlacementError):
        floor.check_job_document({"jobs": []})


def test_a_translated_claude_job_passes_the_floor_check_and_codex_gets_none(world, monkeypatch):
    tmp_path, project, _folder = world
    floor.check_job_document(run_translate(project, tmp_path=tmp_path).spec.document)
    monkeypatch.setattr(translate, "CODEX_FLOOR_PROVED", True)
    codex = run_translate(project, options=RunOptions(worker="codex"), tmp_path=tmp_path).spec.document
    assert "disallowed_tools" not in codex["jobs"][0]["options"]  # llm-kit's dropped field is never presented as enforcement


# -- against the real llm-kit ------------------------------------------------------


@pytest.fixture
def real_llm_kit(monkeypatch):
    """The working copy of llm_kit on sys.path for one test; its modules are purged afterwards."""
    before = {name for name in sys.modules if name == "llm_kit" or name.startswith("llm_kit.")}
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()
    try:
        yield importlib.import_module("llm_kit.jobs.model")
    finally:
        for name in [n for n in sys.modules if (n == "llm_kit" or n.startswith("llm_kit.")) and n not in before]:
            del sys.modules[name]


def write_job_file(tmp_path, document) -> Path:
    path = tmp_path / "job.yaml"
    path.write_text(yaml.safe_dump(dict(document), sort_keys=False), encoding="utf-8")
    return path


def test_the_emitted_job_document_loads_through_llm_kit_with_the_floor_on_the_job(world, real_llm_kit):
    tmp_path, project, _folder = world
    spec = run_translate(project, tmp_path=tmp_path).spec
    job_file = real_llm_kit.load_job_file(write_job_file(tmp_path, spec.document))
    assert job_file.disallowed_tools is None  # the run-level floor is NOT used
    (job,) = job_file.jobs
    assert job.options["disallowed_tools"] == floor.CLAUDE_DISALLOWED_TOOLS
    assert job.models == ("opus",) and job.max_attempts == 1 and job.workspace.isolate is False
    assert tuple(job.contract.command) == tuple(spec.contract_argv) and job.contract.directory == project.resolve()


def test_a_run_level_floor_changes_routing_which_is_why_it_is_refused(world, real_llm_kit):
    tmp_path, project, _folder = world
    from llm_kit.jobs import run as jobs_run

    spec = run_translate(project, tmp_path=tmp_path).spec
    job = real_llm_kit.load_job_file(write_job_file(tmp_path, spec.document)).jobs[0]
    assert dict(job.requirements) == {}  # a job-level floor adds no routing requirement
    floored = jobs_run._require_floor_subjects(job, floor.CLAUDE_DISALLOWED_TOOLS)
    assert floored.requirements  # a run-level floor would add guarantees every candidate must meet
    with pytest.raises(floor.FloorPlacementError):
        floor.check_job_document({**spec.document, "disallowed_tools": floor.CLAUDE_DISALLOWED_TOOLS})


def test_the_emitted_claude_argv_carries_bypass_permissions_and_every_floor_pattern(world, real_llm_kit):
    """RED CHECK (argv test): the argv llm-kit really builds, from the job the dashboard really emits."""
    tmp_path, project, _folder = world
    from llm_kit.adapters.backends import ClaudeCliBackend
    from llm_kit.jobs import run as jobs_run

    spec = run_translate(project, tmp_path=tmp_path).spec
    job = real_llm_kit.load_job_file(write_job_file(tmp_path, spec.document)).jobs[0]
    seen = {}

    def fake_runner(cmd, user, cwd, log_prefix="", timeout_s=None):
        seen.update(cmd=list(cmd), user=user, cwd=cwd)
        return json.dumps({"result": "ok", "usage": {}}), "", 0

    selection = SimpleNamespace(endpoint="opus", effort=None)
    options = jobs_run._backend_options("r1", job, selection, project, 30.0, run_floor=None)
    ClaudeCliBackend(runner=fake_runner, executable="claude").complete(
        job.prompt.system, job.prompt.user, model="opus", options=options)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    denied = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert denied == list(floor.PATTERNS)
    assert "--append-system-prompt" in cmd and "--system-prompt" not in cmd
    assert seen["cwd"] == project and seen["user"] == job.prompt.user
    # With the floor removed from the job the flag disappears: the test above could fail.
    bare = real_llm_kit.Job(id="j", prompt=job.prompt, models=job.models, contract=job.contract, options={"system_prompt_mode": "append"})
    options = jobs_run._backend_options("r1", bare, selection, project, 30.0, run_floor=None)
    ClaudeCliBackend(runner=fake_runner, executable="claude").complete("s", "u", model="opus", options=options)
    assert "--disallowedTools" not in seen["cmd"]
