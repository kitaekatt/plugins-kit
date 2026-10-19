"""Acceptance: the contract that decides whether a worker's run counts.

Judged over a real git project and a real task folder; the worker's "answer" is
canned text. The command line and exit codes are the interface llm-kit sees, so
a subprocess test pins them too.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from dashboard_support import (
    REPO_ROOT,
    SCRIPTS_DIR,
    agent_world,
    git,
)

from task_system.dashboard import acceptance, task_result, vcs
from task_system.dashboard.mutation import folder_revision, raw_items, stored_status

TASK = "tmp/alpha"
ACCEPT_CLI = SCRIPTS_DIR / "task_system" / "dashboard" / "accept_cli.py"


def answer(status="done", **over) -> str:
    body = {"schema": "task_result/v1", "status": status, "summary": "Did it.", "needs": "", "reason": "",
            "resume_hint": "", "checks": ["ran"], "changed_paths": ["x.txt"]}
    body.update(over)
    return "Finished.\n```task_result\n" + json.dumps(body) + "\n```\n"


class Case:
    """A task folder, a recorded start, and helpers that play the agent."""

    def __init__(self, tmp_path, accept=()):
        _devroot, self.project, self.folder = agent_world(tmp_path)
        self.run_dir = tmp_path / "run"
        self.run_dir.mkdir()
        facts = vcs.facts(self.project)
        self.start = {"revision": folder_revision(self.folder), "vcs": facts.vcs, "head": facts.head,
                      "branch": facts.branch, "status": list(facts.status),
                      "log_lines": len((self.folder / "log.md").read_text(encoding="utf-8").splitlines())}
        self.spec = {"project_root": str(self.project), "folder": str(self.folder), "task_ref": TASK, "item_id": "do-it",
                     "lock_path": str(tmp_path / "mutation.lock"), "accept": [list(a) for a in accept], "start": self.start}
        (self.run_dir / "spec.json").write_text(json.dumps(self.spec), encoding="utf-8")

    def complete_item(self, *, log=True, remove=True):
        plan = (self.folder / "plan.md").read_text(encoding="utf-8")
        if remove:
            plan = plan.replace("  - id: do-it\n    title: Do it\n    state: available\n    priority: P1\n", "")
            assert "id: do-it" not in plan
        (self.folder / "plan.md").write_text(plan, encoding="utf-8")
        if log:
            with open(self.folder / "log.md", "a", encoding="utf-8") as handle:
                handle.write("- 2026-10-02 10:00: completed do-it\n")

    def judge(self, text):
        return acceptance.judge(self.spec, text, start_head=self.start["head"], start_branch=self.start["branch"])

    def main(self, text):
        argv = [TASK, "do-it", "run1", self.start["revision"], self.start["head"] or "-", self.start["branch"] or "-", str(self.run_dir)]
        return acceptance.main(argv, text)

    def items(self):
        return {i["id"]: i for i in raw_items((self.folder / "plan.md").read_text(encoding="utf-8"))}


@pytest.fixture
def case(tmp_path):
    return Case(tmp_path)


# -- the result object ---------------------------------------------------------


def test_the_result_must_be_exactly_one_well_formed_task_result_block():
    ok = task_result.extract(answer())
    assert ok.status == "done" and ok.summary == "Did it." and ok.changed_paths == ("x.txt",)
    assert task_result.extract(answer().replace("\n", "\r\n")).status == "done"  # a Windows transcript
    for text, code in [
        ("prose only", "result-missing"),
        (answer() + answer(), "result-duplicate"),
        ("```task_result\n{not json}\n```\n", "result-malformed"),
        ("```task_result\n[1]\n```\n", "result-malformed"),
        (answer(schema="task_result/v2"), "result-malformed"),
        (answer(status="maybe"), "result-malformed"),
        (answer(summary="  "), "result-malformed"),
        (answer(checks="ran"), "result-malformed"),
        (answer(status="blocked"), "result-malformed"),  # a blocked result must say what it needs
    ]:
        with pytest.raises(task_result.ResultError) as raised:
            task_result.extract(text)
        assert raised.value.code == code, text
    with pytest.raises(task_result.ResultError, match="keys are wrong"):
        task_result.extract("```task_result\n" + json.dumps({"schema": "task_result/v1", "status": "done", "extra": 1}) + "\n```\n")


def test_a_json_block_with_another_info_string_is_not_a_result():
    with pytest.raises(task_result.ResultError) as raised:
        task_result.extract('```json\n{"schema": "task_result/v1"}\n```\n')
    assert raised.value.code == "result-missing"


# -- done ------------------------------------------------------------------------


def test_done_is_accepted_when_the_item_is_removed_and_a_log_entry_names_it(case):
    case.complete_item()
    verdict, facts = case.judge(answer())
    assert (verdict.exit_code, verdict.outcome) == (acceptance.EXIT_ACCEPTED, "accepted")
    assert facts["end"]["head"] == case.start["head"]
    assert [c["check"] for c in facts["checks"]] == ["validate"]


def test_sibling_items_may_change_and_only_the_target_is_judged(case):
    case.complete_item()
    plan = (case.folder / "plan.md").read_text(encoding="utf-8").replace("state: available\n    priority: P2", "state: deferred\n    priority: P2")
    (case.folder / "plan.md").write_text(plan, encoding="utf-8")
    assert case.judge(answer())[0].outcome == "accepted"


@pytest.mark.parametrize("kwargs,code", [
    ({"remove": False}, "item-still-present"),
    ({"log": False}, "log-entry-missing"),
])
def test_done_is_rejected_when_completion_is_not_recorded_the_task_way(case, kwargs, code):
    case.complete_item(**kwargs)
    verdict, _facts = case.judge(answer())
    assert (verdict.exit_code, verdict.code) == (acceptance.EXIT_REJECTED, code)


def test_a_log_entry_that_predates_the_run_does_not_count(case):
    case.complete_item(log=False)
    with open(case.folder / "log.md", "a", encoding="utf-8") as handle:
        handle.write("- 2026-10-02 10:00: completed do-it\n")
    # The start snapshot is taken AFTER the entry exists, so it is not new.
    case.start["log_lines"] = len((case.folder / "log.md").read_text(encoding="utf-8").splitlines())
    assert case.judge(answer())[0].code == "log-entry-missing"


def test_validation_findings_after_the_run_reject_it(case):
    case.complete_item()
    task_yaml = (case.folder / "task.yaml").read_text(encoding="utf-8").replace("title: Alpha", "title: Alpha\n  blocked_by:\n  - tmp/gone")
    (case.folder / "task.yaml").write_text(task_yaml, encoding="utf-8")
    verdict, facts = case.judge(answer())
    assert verdict.code == "validation" and facts["checks"][0]["findings"]


def test_accept_argv_lists_run_from_the_project_root_without_a_shell(tmp_path):
    marker = tmp_path / "cwd.txt"
    check = ("import os, sys; open(sys.argv[1], 'w').write(os.getcwd()); sys.exit(0 if sys.argv[2:] == ['a;b', '&&', 'c'] else 1)")
    case = Case(tmp_path, accept=[[sys.executable, "-c", check, str(marker), "a;b", "&&", "c"]])
    case.complete_item()
    verdict, facts = case.judge(answer())
    assert verdict.outcome == "accepted"  # the metacharacters arrived as literal arguments
    assert os.path.samefile(marker.read_text(encoding="utf-8"), case.project)
    assert [c["check"] for c in facts["checks"]] == ["validate", "accept"]


def test_a_failing_accept_argv_rejects_with_its_output(tmp_path):
    case = Case(tmp_path, accept=[[sys.executable, "-c", "print('boom output'); raise SystemExit(3)"]])
    case.complete_item()
    verdict, _facts = case.judge(answer())
    assert verdict.code == "accept-failed" and "boom output" in verdict.detail and "3" in verdict.detail


def test_an_accept_argv_that_cannot_start_rejects_instead_of_crashing(tmp_path):
    case = Case(tmp_path, accept=[["definitely-not-a-command-xyz"]])
    case.complete_item()
    assert case.judge(answer())[0].code == "accept-failed"


# -- git: observed, not attributed ----------------------------------------------


def test_a_branch_that_moved_is_rejected(case):
    case.complete_item()
    git(case.project, "checkout", "-q", "-b", "other")
    verdict, _facts = case.judge(answer())
    assert verdict.code == "branch-changed" and "'main'" in verdict.detail and "'other'" in verdict.detail


def test_a_start_head_that_is_no_longer_an_ancestor_is_rejected(tmp_path):
    case = Case(tmp_path)
    (case.project / "second.txt").write_text("2\n", encoding="utf-8")
    git(case.project, "add", "second.txt")
    git(case.project, "commit", "-q", "-m", "second")
    case.start["head"] = git(case.project, "rev-parse", "HEAD")
    case.spec["start"] = case.start
    git(case.project, "reset", "-q", "--hard", "HEAD~1")  # history rewritten under the run
    case.complete_item()
    assert case.judge(answer())[0].code == "head-not-descendant"


def test_intervening_commits_are_allowed_and_status_delta_is_history_not_a_gate(case):
    case.complete_item()
    (case.project / "new.txt").write_text("n\n", encoding="utf-8")
    git(case.project, "add", "new.txt")
    git(case.project, "commit", "-q", "-m", "someone else")
    (case.project / "untracked.txt").write_text("u\n", encoding="utf-8")
    verdict, facts = case.judge(answer())
    assert verdict.outcome == "accepted" and any("untracked.txt" in line for line in facts["status_delta"])


def test_a_project_that_is_not_a_repository_skips_the_git_checks_and_says_so(tmp_path):
    _devroot, project, folder = agent_world(tmp_path, repo=False)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    start = {"revision": folder_revision(folder), "vcs": False, "head": None, "branch": None, "status": [], "log_lines": 1}
    spec = {"project_root": str(project), "folder": str(folder), "task_ref": TASK, "item_id": "do-it",
            "lock_path": str(tmp_path / "lock"), "accept": [], "start": start}
    plan = (folder / "plan.md").read_text(encoding="utf-8").replace("  - id: do-it\n    title: Do it\n    state: available\n    priority: P1\n", "")
    (folder / "plan.md").write_text(plan, encoding="utf-8")
    with open(folder / "log.md", "a", encoding="utf-8") as handle:
        handle.write("- 2026-10-02 10:00: completed do-it\n")
    verdict, facts = acceptance.judge(spec, answer(), start_head=None, start_branch=None)
    assert verdict.outcome == "accepted" and "not a git repository" in facts["note"]


# -- blocked ---------------------------------------------------------------------


def test_a_blocked_result_uses_its_own_exit_code_and_blocks_task_and_item(case):
    text = answer("blocked", needs="decision", reason="two designs", resume_hint="pick one")
    assert case.main(text) == acceptance.EXIT_AGENT_BLOCKED != acceptance.EXIT_REJECTED
    assert stored_status((case.folder / "task.yaml").read_text(encoding="utf-8")) == "blocked"
    item = case.items()["do-it"]
    assert item["state"] == "blocked-user" and "decision: two designs -- pick one" in item["note"]
    receipt = json.loads((case.run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["verdict"]["outcome"] == "agent-blocked" and receipt["result"]["resume_hint"] == "pick one"
    assert "completed" not in (case.folder / "log.md").read_text(encoding="utf-8")


def test_an_accepted_run_writes_a_receipt_and_changes_nothing_in_the_task(case):
    case.complete_item()
    before = (case.folder / "plan.md").read_bytes(), (case.folder / "task.yaml").read_bytes(), (case.folder / "log.md").read_bytes()
    assert case.main(answer()) == acceptance.EXIT_ACCEPTED
    assert before == ((case.folder / "plan.md").read_bytes(), (case.folder / "task.yaml").read_bytes(), (case.folder / "log.md").read_bytes())
    receipt = json.loads((case.run_dir / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["verdict"]["outcome"] == "accepted" and receipt["block"] is None and receipt["start_revision"] == case.start["revision"]


# -- the command line llm-kit runs -------------------------------------------------


def test_the_contract_command_line_reads_the_answer_on_stdin_and_exits_by_verdict(tmp_path):
    case = Case(tmp_path)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(REPO_ROOT / "plugins" / "skills-kit"), str(SCRIPTS_DIR)]),
               _BOOTSTRAP_GUARD_SKIP_REEXEC="1")
    argv = [sys.executable, str(ACCEPT_CLI), TASK, "do-it", "run1", case.start["revision"], case.start["head"], "main", str(case.run_dir)]
    proc = subprocess.run(argv, input="no result here", capture_output=True, text=True, env=env, cwd=case.project)
    assert proc.returncode == acceptance.EXIT_REJECTED
    verdict = json.loads(proc.stdout)
    assert verdict["outcome"] == "rejected" and verdict["code"] == "result-missing"
    assert stored_status((case.folder / "task.yaml").read_text(encoding="utf-8")) == "blocked"
    case2 = Case(tmp_path / "second")
    argv2 = [sys.executable, str(ACCEPT_CLI), TASK, "do-it", "run1", case2.start["revision"], case2.start["head"], "main", str(case2.run_dir)]
    case2.complete_item()
    ok = subprocess.run(argv2, input=answer(), capture_output=True, text=True, env=env, cwd=case2.project)
    assert ok.returncode == 0 and json.loads(ok.stdout)["outcome"] == "accepted"
    wrong = subprocess.run([sys.executable, str(ACCEPT_CLI), "only-one-arg"], capture_output=True, text=True, env=env)
    assert wrong.returncode == acceptance.EXIT_REJECTED and json.loads(wrong.stdout)["code"] == "usage"
