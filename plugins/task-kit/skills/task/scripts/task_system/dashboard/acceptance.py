"""Acceptance: the contract llm-kit runs after the worker answers.

llm-kit runs this as the job's contract command with the worker's final answer
on stdin; exit 0 accepts the attempt. The command line is
``<python> accept_cli.py <task-ref> <item> <run-id> <start-revision>
<start-head|-> <branch|-> <run-dir>``; everything else comes from
``<run-dir>/spec.json``, which the dashboard wrote before dispatch.

Exit codes (the typed reason is the single JSON object on stdout):

- 0  accepted;
- 1  rejected: the result was missing or malformed, the branch moved, HEAD is
     not a descendant of the start HEAD, validation failed, the item is still
     present, no log entry names it, or an ``accept`` argv failed. The task is
     blocked so a person inspects the shared tree;
- 3  the worker reported ``blocked`` through its structured result. The task
     is blocked with the worker's reason and resume hint.

Checks target the one item only: sibling items may change. Intervening commits
by other writers are observed, never attributed. The start/end ``git status``
and their path delta are history in the receipt, not gates. The receipt
(``<run-dir>/receipt.json``) is written on every outcome.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..validate import validate_ref
from . import blocker, task_result, vcs
from .mutation import TaskMutator, raw_items

EXIT_ACCEPTED = 0
EXIT_REJECTED = 1
EXIT_AGENT_BLOCKED = 3

SPEC_FILE = "spec.json"
RECEIPT_FILE = "receipt.json"
DEFAULT_ACCEPT_TIMEOUT_S = 1800.0
OUTPUT_TAIL = 2000


@dataclass(frozen=True)
class Verdict:
    exit_code: int
    outcome: str  # accepted | rejected | agent-blocked
    code: str
    detail: str


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_TAIL else "..." + text[-OUTPUT_TAIL:]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"acceptance cannot read {path}: {exc}") from exc


def judge(spec: dict, answer: str, *, start_head: str | None, start_branch: str | None) -> tuple[Verdict, dict]:
    """Decide the outcome; returns the verdict and the facts for the receipt.

    Does not mutate the task folder.
    """
    root = Path(spec["project_root"])
    folder = Path(spec["folder"])
    item_id = spec["item_id"]
    start = spec["start"]
    facts: dict = {"checks": []}

    try:
        result = task_result.extract(answer)
    except task_result.ResultError as exc:
        return Verdict(EXIT_REJECTED, "rejected", exc.code, str(exc)), facts
    facts["result"] = {
        "status": result.status, "summary": result.summary, "needs": result.needs, "reason": result.reason,
        "resume_hint": result.resume_hint, "checks": list(result.checks), "changed_paths": list(result.changed_paths),
    }

    end = vcs.facts(root)
    facts["end"] = {"vcs": end.vcs, "head": end.head, "branch": end.branch, "status": list(end.status)}
    if start["vcs"]:
        if not end.vcs:
            return Verdict(EXIT_REJECTED, "rejected", "vcs-lost", "the project is no longer a git repository"), facts
        if end.branch != start_branch:
            return Verdict(EXIT_REJECTED, "rejected", "branch-changed",
                           f"the branch moved from {start_branch!r} to {end.branch!r}"), facts
        if start_head and (end.head is None or not vcs.is_ancestor(root, start_head, end.head)):
            return Verdict(EXIT_REJECTED, "rejected", "head-not-descendant",
                           f"the start HEAD {start_head} is not an ancestor of the end HEAD {end.head}"), facts
        facts["status_delta"] = sorted(set(end.status) ^ set(start["status"]))
    else:
        facts["note"] = "not a git repository: the branch and ancestry checks did not apply"

    if result.status == "blocked":
        detail = f"{result.needs}: {result.reason} -- {result.resume_hint}"
        return Verdict(EXIT_AGENT_BLOCKED, "agent-blocked", "agent-blocked", detail), facts

    # done: the checks target the one item only.
    task_ref = spec["task_ref"]
    validation = validate_ref(task_ref, root)
    findings = list(validation.errors) + list(validation.warnings)
    facts["checks"].append({"check": "validate", "findings": findings})
    if findings:
        return Verdict(EXIT_REJECTED, "rejected", "validation", "; ".join(findings[:3])), facts
    plan_text = _read(folder / "plan.md")
    items = raw_items(plan_text)
    if items is None:
        return Verdict(EXIT_REJECTED, "rejected", "items-block-missing", "plan.md no longer has a task_items block"), facts
    if any(i.get("id") == item_id for i in items):
        return Verdict(EXIT_REJECTED, "rejected", "item-still-present",
                       f"item {item_id!r} is still in plan.md; completion is removal from the block"), facts
    new_log = _read(folder / "log.md").splitlines()[start["log_lines"]:]
    if not any(item_id in line for line in new_log):
        return Verdict(EXIT_REJECTED, "rejected", "log-entry-missing",
                       f"no log.md entry added during the run names {item_id!r}"), facts
    for argv in spec.get("accept", []):
        timeout = float(spec.get("accept_timeout_s", DEFAULT_ACCEPT_TIMEOUT_S))
        try:
            proc = subprocess.run(argv, cwd=root, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
            code, out = proc.returncode, _tail(proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired:
            code, out = None, f"timed out after {timeout:g} seconds"
        except OSError as exc:
            code, out = None, f"could not run: {exc}"
        facts["checks"].append({"check": "accept", "argv": argv, "exit_code": code, "output": out})
        if code != 0:
            return Verdict(EXIT_REJECTED, "rejected", "accept-failed", f"{' '.join(argv)} -> {code}: {out}"), facts
    return Verdict(EXIT_ACCEPTED, "accepted", "accepted", result.summary), facts


def main(argv: list[str], answer: str) -> int:
    if len(argv) != 7:
        print(json.dumps({"outcome": "rejected", "code": "usage",
                          "detail": "expected: task-ref item run-id start-revision start-head branch run-dir"}))
        return EXIT_REJECTED
    task_ref, item_id, run_id, start_revision, start_head, branch, run_dir = argv
    run_path = Path(run_dir)
    spec = json.loads(_read(run_path / SPEC_FILE))
    start_head_v = None if start_head == "-" else start_head
    branch_v = None if branch == "-" else branch
    verdict, facts = judge(spec, answer, start_head=start_head_v, start_branch=branch_v)

    block: dict | None = None
    if verdict.exit_code != EXIT_ACCEPTED:
        mutator = TaskMutator(Path(spec["lock_path"]))
        label = "worker reported blocked" if verdict.outcome == "agent-blocked" else f"run rejected ({verdict.code})"
        outcome = blocker.record_block(
            mutator, Path(spec["folder"]), item_id=item_id,
            note=f"{verdict.code}: {verdict.detail}",
            log_detail=f"dashboard run {run_id}: {label}: {blocker.concise(verdict.detail)}",
        )
        block = {"item_mutated": outcome.item_mutated, "item_error": outcome.item_error}

    receipt = {
        "schema": 1, "run_id": run_id, "task_ref": task_ref, "item": item_id,
        "start": spec["start"], "start_revision": start_revision,
        "verdict": {"outcome": verdict.outcome, "code": verdict.code, "detail": verdict.detail},
        "block": block, **facts,
    }
    (run_path / RECEIPT_FILE).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"outcome": verdict.outcome, "code": verdict.code, "detail": blocker.concise(verdict.detail)}))
    return verdict.exit_code


if __name__ == "__main__":  # pragma: no cover -- accept_cli.py is the entry point
    raise SystemExit(main(sys.argv[1:], sys.stdin.read()))
