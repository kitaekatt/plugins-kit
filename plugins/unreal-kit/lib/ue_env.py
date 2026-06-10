"""
UE host-environment scaffolding -- detect and launch the Unreal Editor.

Pure functions. The CLI entry point lives at
``plugins/unreal-kit/scripts/ue_env.py`` -- this module contains the
process-scan, process-spawn, and bridge-readiness helpers used both by that
CLI and by other consumers (hooks, project-side facades).

Operations:

- ``is_mcp_ready(host, port)`` -- best-effort handshake against the bridge.
  Uses ``ue_mcp_client.McpClient`` when the host venv has it installed;
  otherwise falls back to a bare TCP probe (proves a socket is listening
  but not that the bridge is functional).

- ``find_editor_processes(editor_exe=None)`` -- enumerate running
  UnrealEditor.exe processes. Returns a list of dicts ``{pid, has_window,
  exe_path}``. ``has_window`` is True only when the process's
  MainWindowHandle is non-zero (i.e. an interactive editor window has
  appeared; not a still-loading splash, a hung post-crash process, or a
  headless commandlet). If ``editor_exe`` is provided, results are filtered
  to processes whose ExecutablePath matches it (case-insensitive on
  Windows). Windows-only; returns ``[]`` on other platforms.

- ``launch_editor(editor_exe, uproject, map_arg, extra_args)`` -- Popen the
  editor fully detached (no inherited stdio, new process group) and return
  the child PID.

- ``wait_for_mcp_ready`` -- thin polling wrapper around ``is_mcp_ready``
  for callers that want to block until the bridge comes up after a launch.

Why ``has_window`` and not just "process exists":
``tasklist`` / ``Get-Process`` match by image name, which collapses three
distinct states into one signal:
  1. Fully-loaded interactive editor (MainWindowHandle != 0).
  2. Still-loading editor (splash up, main window not yet shown).
  3. Hung or zombie editor (process alive, window gone, often after a
     crash; high RSS, idle CPU, no UI).
State (1) is what the user means by "editor is up"; (2) and (3) look
identical to image-name matching but are not interactively usable. Callers
that need "should I spawn another?" should AND together "any process" with
"no interactive process"; callers that need "can I drive the editor right
now?" should look at ``has_window`` (and probably ``is_mcp_ready``).
"""

import csv
import io
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional


DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORT = 8090
DEFAULT_READINESS_TIMEOUT_S = 180.0
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_PROBE_TIMEOUT_S = 1.0


def is_mcp_ready(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
) -> bool:
    """Return True if the MCP Automation Bridge accepts a handshake.

    Tries a full bridge_hello/bridge_ack roundtrip via ue_mcp_client.
    If the websocket dependency isn't available, falls back to a bare TCP
    connect on (host, port) -- a weaker signal that only proves something
    is listening on the port.
    """
    try:
        from ue_mcp_client import HandshakeError, McpClient, McpConnectionError
    except ImportError:
        return _tcp_probe(host, port, probe_timeout_s)

    try:
        client = McpClient(host=host, port=port, timeout_s=probe_timeout_s)
        client.connect()
        client.close()
        return True
    except (McpConnectionError, HandshakeError, OSError):
        return False


def _tcp_probe(host: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def wait_for_mcp_ready(
    host: str = DEFAULT_MCP_HOST,
    port: int = DEFAULT_MCP_PORT,
    total_timeout_s: float = DEFAULT_READINESS_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    on_attempt: Optional[Callable[[int, float], None]] = None,
) -> bool:
    """Poll is_mcp_ready until success or total_timeout_s elapses.

    ``on_attempt(attempt_number, seconds_remaining)`` is called once per
    failed probe so callers can surface progress without re-implementing
    the loop.
    """
    deadline = time.monotonic() + total_timeout_s
    attempt = 0
    while True:
        attempt += 1
        if is_mcp_ready(host, port):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if on_attempt is not None:
            on_attempt(attempt, remaining)
        time.sleep(min(poll_interval_s, remaining))


_PS_ENUMERATE_EDITORS = (
    "$ErrorActionPreference='SilentlyContinue';"
    "Get-Process -Name UnrealEditor | "
    "ForEach-Object { "
    "[PSCustomObject]@{"
    "Id=$_.Id;"
    "HasWindow=($_.MainWindowHandle.ToInt64() -ne 0);"
    "Path=$_.Path"
    "} } | ConvertTo-Csv -NoTypeInformation"
)


def find_editor_processes(
    editor_exe: Optional[str] = None,
) -> list[dict]:
    """Enumerate running UnrealEditor.exe processes (Windows-only).

    Returns a list of dicts: ``{"pid": int, "has_window": bool,
    "exe_path": str}``. ``has_window`` is True iff the process's
    MainWindowHandle is non-zero -- the discriminator between a usable
    interactive editor and a still-loading, hung, or headless one (see
    module docstring).

    If ``editor_exe`` is supplied, results are filtered to processes whose
    ExecutablePath matches (case-insensitive on Windows). Empty
    ``editor_exe`` means no filtering.

    On non-Windows platforms or if PowerShell is unavailable, returns ``[]``.
    """
    if sys.platform != "win32":
        return []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _PS_ENUMERATE_EDITORS],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []

    results: list[dict] = []
    reader = csv.DictReader(io.StringIO(proc.stdout))
    target_norm = _normalize_path(editor_exe) if editor_exe else None
    for row in reader:
        pid_s = row.get("Id", "").strip()
        if not pid_s:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        has_window = row.get("HasWindow", "").strip().lower() == "true"
        path = row.get("Path", "").strip()
        if target_norm is not None and _normalize_path(path) != target_norm:
            continue
        results.append({"pid": pid, "has_window": has_window, "exe_path": path})
    return results


def _normalize_path(p: Optional[str]) -> str:
    if not p:
        return ""
    return os.path.normcase(os.path.normpath(p))


def launch_editor(
    editor_exe: str,
    uproject: str,
    map_arg: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> int:
    """Spawn the GUI editor fully detached and return the child PID.

    Args:
        editor_exe: Absolute path to UnrealEditor.exe.
        uproject: Absolute path to the .uproject. Empty/None opens the
            last-used project.
        map_arg: Optional map asset path or .umap to load on startup.
            Passed positionally after the .uproject (UE's CLI convention).
        extra_args: Additional command-line tokens appended verbatim.

    Returns:
        PID of the spawned editor process.

    Raises:
        FileNotFoundError: editor_exe does not exist.
        OSError: spawn failure.
    """
    if not Path(editor_exe).is_file():
        raise FileNotFoundError(f"Editor binary not found: {editor_exe}")

    cmd: list[str] = [str(editor_exe)]
    if uproject:
        cmd.append(str(uproject))
    if map_arg:
        cmd.append(str(map_arg))
    if extra_args:
        cmd.extend(extra_args)

    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = True
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid
