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
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

SCHEMA = 1
STATE_FILE = "server.json"
LOCK_FILE = "server.lock"
LOG_FILE = "server.log"
RUNTIME_DIRNAME = "dashboard"
PLUGIN_NAME = "task-kit"
VENV_DIRNAME = ".venv"
DATA_ROOT_ENV = "CLAUDE_BOOTSTRAP_DATA_ROOT"


class DashboardStateError(RuntimeError):
    """The ownership record, data directory or lock is unusable."""


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


def resolve_data_dir(
    env: Mapping[str, str] | None = None, prefix: str | None = None
) -> Path:
    """The plugin's bootstrap data directory, from the venv this process runs in.

    The provisioned venv is ``<data root>/<marketplace>/task-kit/.venv``, so
    its parent IS the data directory and the marketplace comes from the
    resolved bootstrap context rather than a literal. When
    ``CLAUDE_BOOTSTRAP_DATA_ROOT`` redirects the data tree (a claudx session),
    a venv outside that root is refused: running the production venv's data
    directory in a test session would read the production ``server.json``.
    """
    env = os.environ if env is None else env
    venv = Path(sys.prefix if prefix is None else prefix)
    try:
        venv = venv.resolve()
    except OSError as exc:
        raise DashboardStateError(f"cannot resolve the venv path {venv}: {exc}") from exc
    if venv.name != VENV_DIRNAME or venv.parent.name != PLUGIN_NAME:
        raise DashboardStateError(
            "the task-kit dashboard must run under its bootstrap-provisioned venv "
            f"(<data root>/<marketplace>/{PLUGIN_NAME}/{VENV_DIRNAME}); this interpreter's "
            f"prefix is {venv}. Start it with the task-kit launcher, or pass --data-dir."
        )
    data_dir = venv.parent
    redirected = env.get(DATA_ROOT_ENV)
    if redirected:
        data_root = data_dir.parent.parent
        want = os.path.normcase(os.path.realpath(redirected))
        if os.path.normcase(str(data_root)) != want:
            raise DashboardStateError(
                f"{DATA_ROOT_ENV} redirects the data tree to {redirected}, but this "
                f"interpreter belongs to the data directory {data_dir}; refusing to use "
                "the dashboard state of another data tree."
            )
    return data_dir


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
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise LockHeldError(
                f"another task-kit dashboard server holds {self.path}"
            ) from exc
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
