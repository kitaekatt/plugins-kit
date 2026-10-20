"""The run-owning child process.

One runner exists per active project. It owns exactly one run and one llm-kit
ledger, then exits; it is never a daemon. The dashboard server spawns it
detached, so a server restart does not end a run, and the server learns what
happened only from files in the run directory (the full table, with the single
writer of each file, is in ``runfiles.py``):

- ``runner.json``  -- the runner's PID and creation time, written before dispatch;
- ``child.json``  -- the command process's PID and creation time, written by the
  pre-exec step (``child_exec.py``) before the command can start anything;
- ``tree.json``   -- the child and every descendant seen, rewritten as it grows;
- ``cancel.json`` -- the server's request to stop (read here, never written);
- ``heartbeat``   -- its mtime advances while the runner lives;
- ``llm.out``     -- the stdout of the llm-kit command (its JSON run snapshot);
- ``runner.log``  -- the command's stderr and the runner's own notes;
- ``result.json`` -- written last: how the command ended (``finished``,
  ``timeout``, ``cancelled`` or ``runner-refused``), its exit code, what the
  cancel command did, and any process that survived the shutdown.

The runner owns its process tree. While the command runs it keeps a union of
every descendant it has seen, because a grandchild orphaned by its parent's
death can no longer be found by walking children. When the command ends, a
cancel is requested, or the deadline passes, it stops that whole set
leaves-first and verifies each PID with its creation time. After stopping the
tree for a cancel or a timeout it runs the spec's cancel command so the llm-kit
ledger ends ``cancelled`` (the ledger's one writer is the stopped process's
successor, never two at once); with survivors it leaves the ledger alone. On
Windows it first puts itself in a kill-on-close Job Object, so even a killed
runner takes its descendants with it; if the job cannot be created the runner
refuses to dispatch.

The runner never retries, resumes or repairs anything: a run that did not
finish is reported as it ended.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import proctree, procspawn, runfiles
from .runfiles import (  # noqa: F401 -- the file names are part of this module's public surface
    CANCEL_FILE,
    CHILD_FILE,
    GO_FILE,
    HEARTBEAT_FILE,
    LLM_OUT_FILE,
    LOG_FILE,
    RESULT_FILE,
    RUNNER_FILE,
    TREE_FILE,
)

SPEC_FILE = runfiles.SPEC_FILE
CHILD_EXEC = Path(__file__).with_name("child_exec.py")

POLL_S = 0.25
HEARTBEAT_S = 5.0
DEFAULT_GRACE_S = 5.0
HANDSHAKE_S = 30.0

EXIT_OK = 0
EXIT_REFUSED = 2

TIMEOUT_REASON = "run-wide timeout"
DEFAULT_CANCEL_REASON = "cancelled"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    runfiles.write_json(path, payload)


def _cancel_reason(run_dir: Path) -> str:
    status, data = runfiles.read_json(run_dir / CANCEL_FILE)
    if status == runfiles.OK and isinstance(data.get("reason"), str) and data["reason"]:
        return data["reason"]
    return DEFAULT_CANCEL_REASON


def _refuse(run_dir: Path, detail: str) -> int:
    _write_json(run_dir / RESULT_FILE, {
        "outcome": "runner-refused", "exit_code": None, "ended_at": _now(), "detail": detail, "survivors": [],
    })
    return EXIT_REFUSED


def run(run_dir: Path) -> int:
    run_dir = Path(run_dir)
    spec = runfiles.read_json(run_dir / SPEC_FILE)[1]
    runner = spec["runner"]
    argv = list(runner["argv"])
    cancel_argv = list(runner["cancel_argv"]) if runner.get("cancel_argv") else None
    deadline_s = float(runner["deadline_s"])
    grace_s = float(runner.get("grace_s", DEFAULT_GRACE_S))
    cwd = spec["project_root"]
    started = time.monotonic()

    try:
        job_handle = proctree.assign_kill_on_close_job()
    except proctree.ProcTreeError as exc:
        return _refuse(run_dir, str(exc))

    me = proctree.ident_of(os.getpid())
    if me is None:
        raise RuntimeError(f"the runner cannot read its own process identity (pid {os.getpid()})")
    _write_json(run_dir / RUNNER_FILE, {"pid": me.pid, "create_time": me.create_time, "started_at": _now()})

    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in runner.get("env", {}).items()})

    # The server writes cancel.json before it gives up on a runner it never heard from, and checks
    # runner.json after; this side writes runner.json and then checks cancel.json. Whichever of the two
    # comes second sees the other, so a runner that starts late can never dispatch behind a server that
    # already wrote the run off.
    if (run_dir / CANCEL_FILE).is_file():
        _write_json(run_dir / RESULT_FILE, {
            "outcome": "cancelled", "exit_code": None, "ended_at": _now(), "survivors": [],
            "cancel": {"reason": _cancel_reason(run_dir), "command": {"skipped": "no-dispatch"}},
        })
        return EXIT_OK

    flags = procspawn.hidden(windows=procspawn.WINDOWS)
    launch = [sys.executable, str(CHILD_EXEC), str(run_dir), str(me.pid), repr(me.create_time), "--", *argv]

    with open(run_dir / LLM_OUT_FILE, "wb") as out, open(run_dir / LOG_FILE, "ab") as log:
        log.write(f"{_now()} runner: starting {argv[0]} (deadline {deadline_s:g}s)\n".encode("utf-8"))
        log.flush()
        try:
            child = subprocess.Popen(launch, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=log, **flags)
        except OSError as exc:
            return _refuse(run_dir, f"cannot start the command: {exc}")

        # Handshake: the child records itself, then we record it in tree.json, then we say go.
        child_ident = _await_child(run_dir, child)
        if child_ident is None:
            survivors = proctree.stop_idents([i for i in [proctree.ident_of(child.pid)] if i is not None], grace_s=grace_s)
            code = child.poll()
            _write_json(run_dir / RESULT_FILE, {
                "outcome": "runner-refused", "exit_code": code, "ended_at": _now(), "survivors": [s.to_json() for s in survivors],
                "detail": "the command's pre-exec step never recorded itself; see runner.log",
            })
            return EXIT_REFUSED
        seen: dict[int, proctree.ProcIdent] = {}
        _write_tree(run_dir, child_ident, seen)
        _write_json(run_dir / GO_FILE, {"at": _now()})

        outcome = "finished"
        cancel_reason: str | None = None
        last_beat = 0.0
        while child.poll() is None:
            grew = False
            for item in proctree.descendants(child.pid):
                if item.pid not in seen:
                    seen[item.pid] = item
                    grew = True
            if grew:
                _write_tree(run_dir, child_ident, seen)
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                (run_dir / HEARTBEAT_FILE).write_text(_now(), encoding="utf-8")
                last_beat = now
            if (run_dir / CANCEL_FILE).is_file():
                outcome, cancel_reason = "cancelled", _cancel_reason(run_dir)
                log.write(f"{_now()} runner: cancel requested ({cancel_reason}); stopping the process tree\n".encode("utf-8"))
                break
            if now - started >= deadline_s:
                outcome, cancel_reason = "timeout", TIMEOUT_REASON
                log.write(f"{_now()} runner: deadline reached; stopping the process tree\n".encode("utf-8"))
                break
            time.sleep(POLL_S)
        # Everything of the command that is still around: what was seen, what descends from the child now, and
        # whatever is left in the child's session. The last matters on a normal finish too: a grandchild that
        # started and was orphaned between two polls was never seen, and only its session still names it.
        leftover = [*seen.values(), *proctree.session_members(child_ident)]
        if outcome != "finished":
            survivors = proctree.stop_tree(child_ident, leftover, grace_s=grace_s)
        else:
            survivors = proctree.stop_idents(leftover, grace_s=grace_s)
        exit_code = child.poll()
        if exit_code is None:
            exit_code = child.wait(timeout=grace_s) if not survivors else None

        record: dict = {"outcome": outcome, "exit_code": exit_code, "survivors": [s.to_json() for s in survivors]}
        if cancel_reason is not None:
            record["cancel"] = {"reason": cancel_reason, "command": _cancel_ledger(cancel_argv, cancel_reason, cwd, env, survivors)}
        log.write(f"{_now()} runner: {outcome}, exit {exit_code}, {len(survivors)} survivor(s)\n".encode("utf-8"))

    record["ended_at"] = _now()
    _write_json(run_dir / RESULT_FILE, record)
    del job_handle  # held to here on purpose: the job (and its kill-on-close) lives as long as the runner
    return EXIT_OK


def _await_child(run_dir: Path, child: subprocess.Popen) -> proctree.ProcIdent | None:
    """The identity the pre-exec step recorded, once it matches the process we started; else None."""
    deadline = time.monotonic() + HANDSHAKE_S
    while time.monotonic() < deadline:
        status, value = runfiles.read_ident(run_dir / CHILD_FILE)
        if status == runfiles.OK and proctree.live(value) is not None:
            # The recorded process is the one we started, or (a Windows venv interpreter is a launcher that
            # starts the real one) a descendant of it; either way recovery can reach it from here.
            if value.pid == child.pid or any(d.pid == value.pid for d in proctree.descendants(child.pid)):
                return value
        if child.poll() is not None:
            return None
        time.sleep(0.02)
    return None


def _write_tree(run_dir: Path, child: proctree.ProcIdent, seen: dict[int, proctree.ProcIdent]) -> None:
    _write_json(run_dir / TREE_FILE, {"child": child.to_json(), "members": [i.to_json() for i in seen.values()]})


def _cancel_ledger(cancel_argv, reason: str, cwd: str, env: dict, survivors) -> dict:
    """Close the ledger as cancelled -- only once nothing of the command is left to write to it."""
    if survivors:
        return {"skipped": "survivors"}
    if not cancel_argv:
        return {"skipped": "no-cancel-command"}
    return runfiles.run_cancel_command(cancel_argv, reason, cwd=cwd, env=env)
