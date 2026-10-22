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
import warnings
from pathlib import Path
from typing import TextIO

import psutil

from .. import harness, resolve
from ..datadir import DataDirError, resolve_data_dir
from .state import DashboardStateError, read_state, runtime_root, state_path

TOTAL_TIMEOUT_S = 2.0


def find_session_process(start_pid: int | None = None) -> psutil.Process | None:
    """Deprecated: the Claude Code process for ``start_pid`` (default: this process), or None.

    The former ancestor-name heuristic returned the short-lived shell a Bash tool command runs under.
    This shim answers with ``CLAUDE_PID`` validated as a live ancestor (:func:`harness.claude_process`) and
    is removed after two minor versions (docs/reference/deprecation-policy.md); call that function instead.
    """
    warnings.warn("handshake.find_session_process is deprecated: use task_system.harness.claude_process",
                  DeprecationWarning, stacklevel=2)
    try:
        return harness.claude_process(start_pid=start_pid)
    except harness.IdentityError:
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
    except DataDirError as exc:
        return say(f"dashboard handshake skipped, no hold registered: {exc}")
    root = runtime_root(data)
    try:
        state = read_state(root)
    except DashboardStateError as exc:
        return say(f"dashboard handshake skipped, no hold registered (file {state_path(root)}): {exc}")
    if state is None:
        return 0

    if session_pid is not None:  # an explicit pid is the caller's own choice (the tests', a tool's): no ancestry rule
        try:
            process = psutil.Process(session_pid)
        except psutil.NoSuchProcess:
            return say(f"dashboard handshake skipped, no hold registered: process {session_pid} is not running")
    else:
        try:
            process = harness.claude_process()
        except harness.IdentityError as exc:
            return say(f"dashboard handshake skipped, no hold registered: {exc}; a hold on any other process would "
                       "not end with the session")
    session_id = session or harness.session_id() or f"pid-{process.pid}"
    payload = {"folder": str(folder), "session": session_id, "pid": process.pid, "create_time": process.create_time()}
    try:
        answer = _post(state.port, state.token, payload, max(0.1, deadline - clock()))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        return say(f"dashboard handshake failed, no hold registered (record {state_path(root)}, port {state.port}): "
                   f"{type(exc).__name__}: {exc}")
    active = answer.get("active_run")
    if active:
        say(f"dashboard run {active['id']} ({active['state']}) is live on this task; it keeps running and its edits may "
            "collide with yours. Cancel it in the dashboard (Cancel run), or let it end by itself or at its time "
            "limit. /task work proceeds unchanged.")
    return 0
