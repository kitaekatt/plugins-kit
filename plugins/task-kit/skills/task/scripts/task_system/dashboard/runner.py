"""The run-owning child process.

One runner exists per active project. It owns exactly one run and one llm-kit
ledger, then exits; it is never a daemon. The dashboard server spawns it
detached, so a server restart does not end a run, and the server learns what
happened only from files in the run directory:

- ``runner.json``  -- the runner's PID and creation time, written before dispatch;
- ``heartbeat``    -- its mtime advances while the runner lives;
- ``llm.out``      -- the stdout of the llm-kit command (its JSON run snapshot);
- ``runner.log``   -- the command's stderr and the runner's own notes;
- ``result.json``  -- written last: how the command ended (``finished`` or
  ``timeout``), its exit code, and any process that survived the shutdown.

The runner owns its process tree. While the command runs it keeps a union of
every descendant it has seen, because a grandchild orphaned by its parent's
death can no longer be found by walking children. When the command ends, or
the deadline passes, it stops that whole set leaves-first and verifies each
PID with its creation time. On Windows it first puts itself in a kill-on-close
Job Object, so even a killed runner takes its descendants with it; if the job
cannot be created the runner refuses to dispatch.

The runner never retries, resumes or repairs anything: a run that did not
finish is reported as it ended.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import proctree

RUNNER_FILE = "runner.json"
HEARTBEAT_FILE = "heartbeat"
LLM_OUT_FILE = "llm.out"
LOG_FILE = "runner.log"
RESULT_FILE = "result.json"
SPEC_FILE = "spec.json"

POLL_S = 0.25
HEARTBEAT_S = 5.0
DEFAULT_GRACE_S = 5.0

EXIT_OK = 0
EXIT_REFUSED = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def run(run_dir: Path) -> int:
    run_dir = Path(run_dir)
    spec = json.loads((run_dir / SPEC_FILE).read_text(encoding="utf-8"))
    runner = spec["runner"]
    argv = list(runner["argv"])
    deadline_s = float(runner["deadline_s"])
    grace_s = float(runner.get("grace_s", DEFAULT_GRACE_S))
    cwd = spec["project_root"]
    started = time.monotonic()

    try:
        job_handle = proctree.assign_kill_on_close_job()
    except proctree.ProcTreeError as exc:
        _write_json(run_dir / RESULT_FILE, {
            "outcome": "runner-refused", "exit_code": None, "ended_at": _now(), "detail": str(exc), "survivors": [],
        })
        return EXIT_REFUSED

    me = proctree.ident_of(os.getpid())
    assert me is not None
    _write_json(run_dir / RUNNER_FILE, {"pid": me.pid, "create_time": me.create_time, "started_at": _now()})

    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in runner.get("env", {}).items()})
    flags: dict = {}
    if os.name == "posix":
        flags["start_new_session"] = True
    else:
        flags["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    with open(run_dir / LLM_OUT_FILE, "wb") as out, open(run_dir / LOG_FILE, "ab") as log:
        log.write(f"{_now()} runner: starting {argv[0]} (deadline {deadline_s:g}s)\n".encode("utf-8"))
        log.flush()
        try:
            child = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=log, **flags)
        except OSError as exc:
            _write_json(run_dir / RESULT_FILE, {
                "outcome": "runner-refused", "exit_code": None, "ended_at": _now(),
                "detail": f"cannot start the command: {exc}", "survivors": [],
            })
            return EXIT_REFUSED
        child_ident = proctree.ident_of(child.pid)
        seen: dict[int, proctree.ProcIdent] = {}
        outcome = "finished"
        last_beat = 0.0
        while child.poll() is None:
            for item in proctree.descendants(child.pid):
                seen.setdefault(item.pid, item)
            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_S:
                (run_dir / HEARTBEAT_FILE).write_text(_now(), encoding="utf-8")
                last_beat = now
            if now - started >= deadline_s:
                outcome = "timeout"
                log.write(f"{_now()} runner: deadline reached; stopping the process tree\n".encode("utf-8"))
                break
            time.sleep(POLL_S)
        survivors: list[proctree.ProcIdent] = []
        if outcome == "timeout" and child_ident is not None:
            survivors = proctree.stop_tree(child_ident, list(seen.values()), grace_s=grace_s)
        else:
            survivors = proctree.stop_idents(list(seen.values()), grace_s=grace_s)
        exit_code = child.poll()
        if exit_code is None:
            exit_code = child.wait(timeout=grace_s) if not survivors else None
        log.write(f"{_now()} runner: {outcome}, exit {exit_code}, {len(survivors)} survivor(s)\n".encode("utf-8"))

    _write_json(run_dir / RESULT_FILE, {
        "outcome": outcome, "exit_code": exit_code, "ended_at": _now(),
        "survivors": [s.to_json() for s in survivors],
    })
    del job_handle  # held to here on purpose: the job (and its kill-on-close) lives as long as the runner
    return EXIT_OK
