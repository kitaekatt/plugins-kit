"""A stand-in for ``llm-kit jobs run`` used by the dashboard's end-to-end tests.

Usage: ``fake_llm.py <job-file> <store> <run-id>``. It reads the dashboard's
real job file and spec, plays the agent named by ``behavior.json`` next to the
job file, then runs the job's REAL contract command (the acceptance script) with
the agent's answer on stdin, exactly as llm-kit does, and prints a JSON run
snapshot. No model is called.

Behaviors: done, done-no-removal, blocked, no-result, duplicate-result,
branch-switch, commit, hang, lingering, exit-1, attempt-timeout.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

JOB_FILE, STORE, RUN_ID = sys.argv[1:4]
RUN_DIR = Path(JOB_FILE).parent
BEHAVIOR = json.loads((RUN_DIR / "behavior.json").read_text(encoding="utf-8"))["behavior"]
DOCUMENT = yaml.safe_load(Path(JOB_FILE).read_text(encoding="utf-8"))
JOB = DOCUMENT["jobs"][0]
SPEC = json.loads((RUN_DIR / "spec.json").read_text(encoding="utf-8"))
FOLDER = Path(SPEC["folder"])
ROOT = Path(SPEC["project_root"])
ITEM = SPEC["item_id"]


def result_block(status: str, **over) -> str:
    body = {"schema": "task_result/v1", "status": status, "summary": "Did the item.", "needs": "", "reason": "",
            "resume_hint": "", "checks": ["ran"], "changed_paths": ["x.txt"]}
    body.update(over)
    return "```task_result\n" + json.dumps(body) + "\n```\n"


def finish_item(remove: bool = True) -> None:
    """What a diligent agent does to the task folder when the item is complete."""
    from task_system.dashboard import mutation

    plan = (FOLDER / "plan.md").read_text(encoding="utf-8")
    if remove:
        match, data = mutation._items_block(plan)
        data["task_items"]["items"] = [i for i in data["task_items"]["items"] if i.get("id") != ITEM]
        body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
        plan = plan[: match.start(2)] + body + plan[match.end(2):]
    (FOLDER / "plan.md").write_text(plan + f"\nAccomplished: {ITEM}.\n", encoding="utf-8")
    with open(FOLDER / "log.md", "a", encoding="utf-8") as handle:
        handle.write(f"- 2026-10-02 10:00: completed {ITEM}\n")
    (ROOT / "x.txt").write_text("made\n", encoding="utf-8")


def run_contract(answer: str) -> int:
    proc = subprocess.run(
        JOB["contract"]["command"], input=answer, text=True, capture_output=True, cwd=JOB["contract"]["directory"],
    )
    (RUN_DIR / "contract.out").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    state = "accepted" if proc.returncode == 0 else "rejected"
    print(json.dumps({"run": RUN_ID, "jobs": [{"id": JOB["id"], "state": state, "acceptance": {"exit_code": proc.returncode}}]}))
    return 0 if proc.returncode == 0 else 1


def grandchild() -> subprocess.Popen:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    (RUN_DIR / "grandchild.pid").write_text(str(child.pid), encoding="utf-8")
    return child


if BEHAVIOR == "done":
    finish_item()
    sys.exit(run_contract("All done.\n" + result_block("done")))
if BEHAVIOR == "done-no-removal":
    finish_item(remove=False)
    sys.exit(run_contract(result_block("done")))
if BEHAVIOR == "blocked":
    sys.exit(run_contract(result_block("blocked", needs="decision", reason="two designs fit",
                                       resume_hint="pick one in plan.md, then re-check")))
if BEHAVIOR == "no-result":
    sys.exit(run_contract("I think it went fine."))
if BEHAVIOR == "duplicate-result":
    sys.exit(run_contract(result_block("done") + result_block("done")))
if BEHAVIOR == "branch-switch":
    finish_item()
    subprocess.run(["git", "-C", str(ROOT), "checkout", "-q", "-b", "other"], check=True)
    sys.exit(run_contract(result_block("done")))
if BEHAVIOR == "commit":
    finish_item()
    subprocess.run(["git", "-C", str(ROOT), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", "agent commit"], check=True)
    sys.exit(run_contract(result_block("done")))
if BEHAVIOR == "hang":
    grandchild()
    time.sleep(600)
if BEHAVIOR == "lingering":
    grandchild()
    finish_item()
    code = run_contract(result_block("done"))
    sys.exit(code)  # exits while its grandchild still sleeps
if BEHAVIOR == "attempt-timeout":
    # What llm-kit prints when the claude call hits the job's own timeout: the attempt is `timeout`
    # but the job is `failed` (error code "execution") and the runner exits 1.
    snapshot = {"attempts": [{"attempt_no": 1, "status": "timeout", "error": {
        "code": "execution", "message": "claude -p exceeded 30.0s timeout (elapsed 30s; likely rate-limit backoff at the CLI layer).\nstderr tail:\n<empty>"}}],
        "jobs": [{"id": JOB["id"], "state": "failed", "error": None}]}
    print(json.dumps(snapshot))
    sys.exit(1)
if BEHAVIOR == "exit-1":
    print("model unavailable", file=sys.stderr)
    sys.exit(1)
raise SystemExit(f"unknown behavior {BEHAVIOR!r}")
