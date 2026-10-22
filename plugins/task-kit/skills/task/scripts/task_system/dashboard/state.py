"""Server ownership record, OS lock, and runtime-root resolution.

The dashboard's machine-scoped runtime state lives under one directory,
``<data dir>/dashboard/``, where ``<data dir>`` is the plugin's bootstrap data
directory (``<data root>/<marketplace>/task-kit``). It holds:

- ``server.lock``  -- held (OS file lock) by the one running server for its
  whole lifetime; the lock, not the record, decides who owns the dashboard.
- ``server.json``  -- the ownership record the launcher reads: PID, PID
  creation time, instance nonce, port, token, code hash, start time, log path.
- ``server.log``   -- the server's redacted stdout/stderr.

A record that exists but cannot be read is an ERROR, never "absent": the
launcher must not start a second server, or stop an arbitrary listener, on
the strength of a file it could not understand.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import oslock
from ..datadir import (  # noqa: F401 -- the resolver moved to task_system.datadir; re-exported for callers
    DATA_ROOT_ENV,
    PLUGIN_NAME,
    VENV_DIRNAME,
    DataDirError,
    resolve_data_dir,
)

SCHEMA = 1
STATE_FILE = "server.json"
LOCK_FILE = "server.lock"
LOG_FILE = "server.log"
RUNTIME_DIRNAME = "dashboard"


class DashboardStateError(DataDirError):
    """The ownership record or lock is unusable (a :class:`DataDirError` too, so one handler covers both)."""


class LockHeldError(DashboardStateError):
    """Another process holds ``server.lock``."""


@dataclass(frozen=True)
class ServerState:
    """The contents of ``server.json``."""

    schema: int
    pid: int
    pid_create_time: float
    nonce: str
    port: int
    token: str
    code_hash: str
    started_at: str
    log_path: str

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#{self.token}"


_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "schema": (int,),
    "pid": (int,),
    "pid_create_time": (int, float),
    "nonce": (str,),
    "port": (int,),
    "token": (str,),
    "code_hash": (str,),
    "started_at": (str,),
    "log_path": (str,),
}


def runtime_root(data_dir: Path) -> Path:
    return Path(data_dir) / RUNTIME_DIRNAME


def state_path(root: Path) -> Path:
    return Path(root) / STATE_FILE


def read_state(root: Path) -> ServerState | None:
    """The recorded server state, or ``None`` when no record exists.

    Raises :class:`DashboardStateError` when a record exists but is not a
    valid, current-schema ownership record.
    """
    path = state_path(root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DashboardStateError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DashboardStateError(_corrupt(path, f"not valid JSON ({exc})")) from exc
    if not isinstance(data, dict):
        raise DashboardStateError(_corrupt(path, "not a JSON object"))
    missing = sorted(set(_FIELD_TYPES) - set(data))
    if missing:
        raise DashboardStateError(_corrupt(path, f"missing fields {missing}"))
    extra = sorted(set(data) - set(_FIELD_TYPES))
    if extra:
        raise DashboardStateError(_corrupt(path, f"unknown fields {extra}"))
    for name, types in _FIELD_TYPES.items():
        value = data[name]
        if isinstance(value, bool) or not isinstance(value, types):
            raise DashboardStateError(_corrupt(path, f"field {name!r} has the wrong type"))
    if data["schema"] != SCHEMA:
        raise DashboardStateError(
            _corrupt(path, f"schema {data['schema']!r} is not the supported schema {SCHEMA}")
        )
    if not (0 < data["port"] < 65536) or data["pid"] <= 0:
        raise DashboardStateError(_corrupt(path, "port or pid out of range"))
    if not data["token"] or not data["nonce"]:
        raise DashboardStateError(_corrupt(path, "empty token or nonce"))
    return ServerState(**data)


def _corrupt(path: Path, reason: str) -> str:
    return (
        f"the task-kit dashboard ownership record {path} is unusable: {reason}. "
        "It was left alone on purpose. Run `task-kit status` to see whether a "
        "server is running; if none is, delete that file and run `task-kit` again."
    )


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_root(root: Path) -> bool:
    """Create the runtime root owner-only (0700), and tighten an existing one.

    ``server.json`` holds the token that authorizes queueing runs, so no other
    local account may read the root. On POSIX an existing root with any other
    mode is chmodded to 0700 and the call returns True so the caller can log it
    once. Windows creates the root and chmods nothing: its privacy comes from
    the profile ACL, which this does not assert.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    if os.name == "nt":
        return False
    current = stat.S_IMODE(root.stat().st_mode)
    if current == PRIVATE_DIR_MODE:
        return False
    os.chmod(root, PRIVATE_DIR_MODE)
    return True


def write_state(root: Path, state: ServerState) -> None:
    """Atomically replace ``server.json`` (write a sibling, fsync, replace).

    The file is owner-only (0600) from the moment it exists; a stale sibling
    left by a crashed writer is chmodded too, because ``O_TRUNC`` keeps the old
    mode. Windows relies on the profile ACL.
    """
    root = Path(root)
    final = state_path(root)
    temp = root / f"{STATE_FILE}.{os.getpid()}.tmp"
    payload = (json.dumps(asdict(state), indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        if os.name != "nt":
            os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, final)


def remove_state_if_owned(root: Path, nonce: str) -> bool:
    """Delete ``server.json`` only when it still names ``nonce``."""
    try:
        current = read_state(root)
    except DashboardStateError:
        return False
    if current is None or current.nonce != nonce:
        return False
    try:
        state_path(root).unlink()
    except FileNotFoundError:
        return False
    return True


class ServerLock:
    """An exclusive, non-blocking OS file lock held for the server's lifetime.

    The operating system drops it when the process dies, so a crashed server
    never leaves a lock behind; only ``server.json`` can be stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        if not oslock.try_lock(handle):
            handle.close()
            raise LockHeldError(f"another task-kit dashboard server holds {self.path}")
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            oslock.unlock(handle)
        finally:
            handle.close()
