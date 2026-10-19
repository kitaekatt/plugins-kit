"""The `/task work` handshake: tell a running dashboard "the harness has this task".

The skill runs it once before the unchanged ``work`` verb. It registers an
interactive HOLD on the task folder, keyed by the Claude Code session id and
the session process's identity (PID plus creation time). The dashboard's
scheduler then never starts a run on that task while the session lives, and
the hold ends with the session or with Re-check.

The contract is one-directional and never in the way of the harness:

- No dashboard (no ``server.json``): print nothing, change nothing.
- A fault (corrupt ``server.json``, nothing answering on the recorded port, a
  hung server, a refusal): print exactly ONE line on stderr naming the fault
  and the file, register nothing, and let ``/task work`` proceed.
- The whole exchange is bounded by :data:`TOTAL_TIMEOUT_S`.
- A live dashboard run on this very task prints one notice (run id, state, what
  the user can do) and ``/task work`` proceeds regardless.

The exit status is always 0: this command can never fail ``/task work``.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
from pathlib import Path
from typing import TextIO

import psutil

from .. import resolve
from .state import DashboardStateError, read_state, resolve_data_dir, runtime_root, state_path

TOTAL_TIMEOUT_S = 2.0


def find_session_process(start_pid: int | None = None) -> psutil.Process | None:
    """The nearest ancestor that looks like the Claude Code process, or None."""
    try:
        proc = psutil.Process(start_pid if start_pid is not None else os.getpid()).parent()
    except psutil.NoSuchProcess:
        return None
    while proc is not None:
        try:
            name = proc.name().lower()
            command = " ".join(proc.cmdline()[:3]).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name, command = "", ""
        if "claude" in name or "claude" in command:
            return proc
        try:
            proc = proc.parent()
        except psutil.NoSuchProcess:
            return None
    return None


def _post(port: int, token: str, payload: dict, timeout_s: float) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_s)
    body = json.dumps(payload).encode("utf-8")
    try:
        connection.request("POST", "/api/v1/holds", body=body, headers={
            "Content-Type": "application/json", "X-Task-Kit-Token": token,
            "Origin": f"http://127.0.0.1:{port}", "Content-Length": str(len(body)),
        })
        response = connection.getresponse()
        data = response.read()
        if response.status != 200:
            raise OSError(f"the dashboard answered HTTP {response.status}")
        return json.loads(data.decode("utf-8"))
    finally:
        connection.close()


def run(
    ref: str,
    project_root: Path,
    *,
    session: str | None = None,
    data_dir: Path | None = None,
    session_pid: int | None = None,
    err: TextIO | None = None,
    clock=time.monotonic,
) -> int:
    err = err if err is not None else sys.stderr
    deadline = clock() + TOTAL_TIMEOUT_S

    def say(message: str) -> int:
        err.write(f"task-kit: {message}\n")
        err.flush()
        return 0

    try:
        resolved = resolve.resolve_ref(ref, Path(project_root))
    except resolve.RefResolutionError:
        return 0  # the work verb reports an unresolvable ref itself
    folder = Path(project_root) / resolved.canonical

    try:
        data = Path(data_dir) if data_dir is not None else resolve_data_dir()
    except DashboardStateError as exc:
        return say(f"dashboard handshake skipped, no hold registered: {exc}")
    root = runtime_root(data)
    try:
        state = read_state(root)
    except DashboardStateError as exc:
        return say(f"dashboard handshake skipped, no hold registered (file {state_path(root)}): {exc}")
    if state is None:
        return 0

    process = psutil.Process(session_pid) if session_pid is not None else find_session_process()
    if process is None:
        return say("dashboard handshake skipped, no hold registered: no Claude Code process was found among this "
                   "command's ancestors, so a hold would not end with the session")
    session_id = session or os.environ.get("CLAUDE_SESSION_ID") or f"pid-{process.pid}"
    payload = {"folder": str(folder), "session": session_id, "pid": process.pid, "create_time": process.create_time()}
    try:
        answer = _post(state.port, state.token, payload, max(0.1, deadline - clock()))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        return say(f"dashboard handshake failed, no hold registered (record {state_path(root)}, port {state.port}): "
                   f"{type(exc).__name__}: {exc}")
    active = answer.get("active_run")
    if active:
        say(f"dashboard run {active['id']} ({active['state']}) is live on this task; it keeps running and its edits may "
            "collide with yours. Cancel arrives in a later release: the run ends by itself or at its time limit. "
            "/task work proceeds unchanged.")
    return 0
