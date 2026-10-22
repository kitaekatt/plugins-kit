"""``task-kit stop``: stop a dashboard server an earlier task-kit version started.

The dashboard itself is retired (databench provides the task view), but a server started by an earlier
version can still be running. This module reads that server's legacy ownership record,
``<task-kit data dir>/dashboard/server.json``, and stops the server it names. It is kept for one task-kit
minor, then removed together with the ``stop`` command.

Ownership is never guessed. A process counts as the recorded server only when its PID and its creation time
(via ``psutil``) both match the record. Anything else that answers on the recorded port is foreign and is
never signalled. A record that cannot be parsed stops the command; it is never treated as absent. Nothing
here deletes the record or any other file under ``dashboard/``.

Exit codes: 0 stopped, or nothing to stop; 1 error; 2 usage; 5 refused because jobs are running (``--force`` stops
anyway); 6 refused inside a dashboard run.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import psutil

from .datadir import DataDirError, resolve_data_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BUSY = 5
EXIT_NESTED = 6
AGENT_DEPTH_ENV = "TASK_KIT_AGENT_DEPTH"
NESTED_MESSAGE = "task-kit: refused inside a dashboard run: a run may not stop its dashboard"

RUNTIME_DIRNAME = "dashboard"
STATE_FILE = "server.json"
TOKEN_HEADER = "X-Task-Kit-Token"
CREATE_TIME_TOLERANCE_S = 2.0
PROBE_TIMEOUT_S = 2.0
STOP_TIMEOUT_S = 10.0

_FIELDS: dict[str, tuple[type, ...]] = {
    "pid": (int,),
    "pid_create_time": (int, float),
    "port": (int,),
    "token": (str,),
}


class LegacyStopError(RuntimeError):
    """A condition reported (exit 1) rather than worked around."""


class BusyError(LegacyStopError):
    """Refused because jobs are running (exit 5)."""


@dataclass(frozen=True)
class Record:
    """The fields of the legacy ``server.json`` that stopping needs."""

    pid: int
    pid_create_time: float
    port: int
    token: str


def record_path(data_dir: Path) -> Path:
    return Path(data_dir) / RUNTIME_DIRNAME / STATE_FILE


def read_record(data_dir: Path) -> Record | None:
    """The recorded server, ``None`` when no record exists; raises :class:`LegacyStopError` on an unusable one."""
    path = record_path(data_dir)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LegacyStopError(f"cannot read {path}: {exc}") from exc

    def unusable(reason: str) -> LegacyStopError:
        return LegacyStopError(f"the legacy dashboard ownership record {path} is unusable: {reason}. It was left alone.")

    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise unusable(f"not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise unusable("not a JSON object")
    for name, types in _FIELDS.items():
        if name not in data:
            raise unusable(f"missing field {name!r}")
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, types):
            raise unusable(f"field {name!r} has the wrong type")
    if not (0 < data["port"] < 65536) or data["pid"] <= 0 or not data["token"]:
        raise unusable("port, pid or token out of range")
    return Record(data["pid"], float(data["pid_create_time"]), data["port"], data["token"])


class ProbeFailed(Exception):
    """The health endpoint did not give a usable answer."""


def process_create_time(pid: int) -> float | None:
    """The creation time of ``pid``, or None when no such process exists."""
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None


def identity_matches(record: Record, create_time: float | None) -> bool:
    return create_time is not None and abs(create_time - record.pid_create_time) <= CREATE_TIME_TOLERANCE_S


def probe_health(port: int, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """``GET /api/v1/health`` on the loopback port, or raise :class:`ProbeFailed`."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise ProbeFailed(f"HTTP {response.status}")
        data = json.loads(raw.decode("utf-8"))
    except (OSError, http.client.HTTPException, UnicodeDecodeError, ValueError) as exc:
        raise ProbeFailed(f"{type(exc).__name__}: {exc}") from exc
    finally:
        connection.close()
    if not isinstance(data, dict) or "busy" not in data:
        raise ProbeFailed("not a task-kit health document")
    return data


def post_shutdown(record: Record, force: bool, timeout: float = PROBE_TIMEOUT_S) -> None:
    """Ask the recorded server (authenticated by its token) to shut down gracefully."""
    connection = http.client.HTTPConnection("127.0.0.1", record.port, timeout=timeout)
    try:
        connection.request(
            "POST",
            "/api/v1/shutdown",
            body=json.dumps({"force": bool(force)}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{record.port}",
                TOKEN_HEADER: record.token,
            },
        )
        response = connection.getresponse()
        detail = response.read().decode("utf-8", "replace").strip()
    finally:
        connection.close()
    if response.status == 409:
        raise BusyError(f"jobs are running; the server refused to stop ({detail})")
    if response.status != 200:
        raise LegacyStopError(f"the server refused the shutdown request: HTTP {response.status} {detail}")


def kill_verified(record: Record) -> None:
    """Terminate the recorded server and its children after re-verifying identity."""
    try:
        process = psutil.Process(record.pid)
    except psutil.NoSuchProcess:
        return
    if not identity_matches(record, process.create_time()):
        raise LegacyStopError(f"pid {record.pid} no longer has the recorded creation time; refusing to kill it")
    victims = process.children(recursive=True) + [process]
    for victim in victims:
        try:
            victim.terminate()
        except psutil.NoSuchProcess:
            pass
    _gone, alive = psutil.wait_procs(victims, timeout=5)
    for victim in alive:
        try:
            victim.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(alive, timeout=5)


def stop(
    data_dir: Path,
    *,
    force: bool,
    out=None,
    probe: Callable[[int], dict] = probe_health,
    create_time: Callable[[int], float | None] = process_create_time,
    shutdown: Callable[[Record, bool], None] = post_shutdown,
    kill: Callable[[Record], None] = kill_verified,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    stop_timeout: float = STOP_TIMEOUT_S,
) -> int:
    """Stop the legacy dashboard server named by ``data_dir``'s record; every effect is injectable."""
    out = out if out is not None else sys.stdout

    def say(message: str) -> None:
        print(message, file=out)

    record = read_record(data_dir)
    if record is None:
        say("task-kit: no legacy dashboard server is recorded; nothing to stop")
        return EXIT_OK
    matches = identity_matches(record, create_time(record.pid))
    try:
        health: dict | None = probe(record.port)
        probe_error = ""
    except ProbeFailed as exc:
        health, probe_error = None, str(exc)
    if not matches:
        if health is not None:
            raise LegacyStopError(
                f"something answers on 127.0.0.1:{record.port}, but the recorded server process (pid {record.pid}) "
                "is not running with the recorded identity. It is not ours to stop; nothing was signalled."
            )
        say(
            f"task-kit: no legacy dashboard server is running (stale record {record_path(data_dir)}: "
            f"pid {record.pid} is gone); nothing was signalled"
        )
        return EXIT_OK
    if health is None:
        if not force:
            raise LegacyStopError(
                f"the recorded dashboard server (pid {record.pid}) is running but not answering on "
                f"127.0.0.1:{record.port} ({probe_error}). It was left alone. "
                "Run `task-kit stop --force` to stop it (identity-checked)."
            )
        kill(record)
        say(f"task-kit: dashboard stopped (pid {record.pid}, forced)")
        return EXIT_OK
    busy = int(health["busy"])
    if busy and not force:
        raise BusyError(f"{busy} job(s) are running; stop is refused. Wait for them, or use `task-kit stop --force`.")
    shutdown(record, force)
    deadline = monotonic() + stop_timeout
    while identity_matches(record, create_time(record.pid)):
        if monotonic() >= deadline:
            if force:
                kill(record)
                break
            raise LegacyStopError(
                f"the dashboard server (pid {record.pid}) did not exit within {stop_timeout:g}s of the shutdown "
                "request. Run `task-kit stop --force` to stop it."
            )
        sleep(0.1)
    say(f"task-kit: dashboard stopped (pid {record.pid})")
    return EXIT_OK


def main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        prog="task-kit stop",
        description="Stop a dashboard server that an earlier task-kit version started.",
    )
    parser.add_argument("--force", action="store_true", help="stop even if jobs are running or the server is unresponsive")
    parser.add_argument("--data-dir", type=Path, help="use another task-kit data directory")
    args = parser.parse_args(argv)
    if AGENT_DEPTH_ENV in os.environ:
        print(NESTED_MESSAGE, file=sys.stderr)
        return EXIT_NESTED
    try:
        data_dir = args.data_dir if args.data_dir is not None else resolve_data_dir()
    except DataDirError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        return stop(data_dir, force=args.force)
    except BusyError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except LegacyStopError as exc:
        print(f"task-kit: {exc}", file=sys.stderr)
        return EXIT_ERROR
