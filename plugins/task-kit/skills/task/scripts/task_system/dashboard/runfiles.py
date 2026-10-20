"""The files of a run directory, and the few primitives that read and write them.

A run directory is the only channel between the dashboard server and the
run-owning processes (the server may die and come back; the runner does not
wait for it). Every file here has one writer:

====================  ==========  ===============================================================
file                  written by  meaning
====================  ==========  ===============================================================
spec.json, job.yaml   server      what to run (written before the runner is spawned)
runner.json           runner      the runner's PID and creation time, before anything is dispatched
child.json            child_exec  the command process's PID and creation time, BEFORE it execs
go                    runner      "the child is recorded; exec the command now"
tree.json             runner      the child plus every descendant seen, rewritten as it grows
cancel.json           server      "stop this run" (and, written first, "never dispatch it")
heartbeat             runner      mtime advances while the runner lives
llm.out, runner.log   command     stdout / stderr of the llm-kit command (and the runner's notes)
result.json           runner      written last: how the command ended; see ``runner.py``
receipt.json          acceptance  the verdict, written atomically by the contract command
ledger.sqlite3        llm-kit     the durable job ledger (the server only reads it, read-only)
====================  ==========  ===============================================================

Every JSON file is written to a temporary name and renamed into place, so a
reader sees the whole file or none of it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import proctree

SPEC_FILE = "spec.json"
RUNNER_FILE = "runner.json"
CHILD_FILE = "child.json"
GO_FILE = "go"
TREE_FILE = "tree.json"
CANCEL_FILE = "cancel.json"
HEARTBEAT_FILE = "heartbeat"
LLM_OUT_FILE = "llm.out"
LOG_FILE = "runner.log"
RESULT_FILE = "result.json"
RECEIPT_FILE = "receipt.json"
LEDGER_FILE = "ledger.sqlite3"

REASON_PLACEHOLDER = "{reason}"
CANCEL_COMMAND_TIMEOUT_S = 120.0
OUTPUT_TAIL = 1000

# A file that is there but cannot be used is not the same as a file that is not there.
MISSING = "missing"
UNREADABLE = "unreadable"
OK = "ok"


def write_json(path: Path, payload) -> None:
    """Write ``payload`` to ``path`` atomically (temporary file, then rename)."""
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path, *, kind: type = dict) -> tuple[str, object]:
    """``(OK, data)``, ``(MISSING, None)``, or ``(UNREADABLE, why)``: never raises on a bad file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return MISSING, None
    except (OSError, UnicodeDecodeError) as exc:
        return UNREADABLE, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(text)
    except ValueError as exc:
        return UNREADABLE, f"not valid JSON ({exc})"
    if not isinstance(data, kind):
        return UNREADABLE, f"expected a JSON {kind.__name__}, found {type(data).__name__}"
    return OK, data


def read_ident(path: Path) -> tuple[str, proctree.ProcIdent | str | None]:
    """The process identity a ``runner.json`` / ``child.json`` records."""
    status, data = read_json(path)
    if status != OK:
        return status, data
    try:
        return OK, proctree.ProcIdent(int(data["pid"]), float(data["create_time"]))
    except (KeyError, TypeError, ValueError) as exc:
        return UNREADABLE, f"no usable pid/create_time ({exc})"


def read_tree(run_dir: Path) -> tuple[proctree.ProcIdent | None, list[proctree.ProcIdent], list[str]]:
    """The command process and every descendant ever recorded for the run, plus any problems found."""
    problems: list[str] = []
    child = None
    status, value = read_ident(Path(run_dir) / CHILD_FILE)
    if status == OK:
        child = value
    elif status == UNREADABLE:
        problems.append(f"{CHILD_FILE} is unreadable: {value}")
    members: list[proctree.ProcIdent] = []
    status, data = read_json(Path(run_dir) / TREE_FILE)
    if status == OK:
        try:
            members = [proctree.ProcIdent.from_json(m) for m in data.get("members", [])]
            if child is None and data.get("child"):
                child = proctree.ProcIdent.from_json(data["child"])
        except (TypeError, ValueError, IndexError) as exc:
            problems.append(f"{TREE_FILE} is unusable: {exc}")
    elif status == UNREADABLE:
        problems.append(f"{TREE_FILE} is unreadable: {data}")
    return child, members, problems


def substitute_reason(argv: list[str], reason: str) -> list[str]:
    """The cancel command with the reason placed where the spec left ``{reason}``."""
    return [reason if part == REASON_PLACEHOLDER else part for part in argv]


def run_cancel_command(argv: list[str], reason: str, *, cwd: str | None = None, env: dict | None = None,
                       timeout_s: float = CANCEL_COMMAND_TIMEOUT_S) -> dict:
    """Run the llm-kit cancel command; the record of what happened (never raises).

    Exit 0 means the ledger now holds the run as cancelled (or it already ended); exit 3 means the
    store has no such run, i.e. nothing durable was ever created. Anything else, a timeout or a
    command that cannot start is a failure the caller must not hide.
    """
    command = substitute_reason(list(argv), reason)
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "error": f"timed out after {timeout_s:g} seconds"}
    except OSError as exc:
        return {"exit_code": None, "error": f"cannot run the cancel command: {exc}"}
    return {"exit_code": proc.returncode, "stdout": proc.stdout[-OUTPUT_TAIL:], "stderr": proc.stderr[-OUTPUT_TAIL:]}


def cancel_command_ok(record: dict | None) -> bool:
    """True when a recorded cancel command left the ledger closed (or showed there was none)."""
    if not isinstance(record, dict):
        return False
    if record.get("skipped") == "no-dispatch":
        return True
    return record.get("exit_code") in (0, 3)
