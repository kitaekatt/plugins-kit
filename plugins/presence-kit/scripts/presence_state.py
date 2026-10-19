"""presence-kit session state: one JSON document per session, locked and atomic.

Leaf module (stdlib only). Contract: dev/tasks/presence-kit/contracts.md
sections 1-3. Every write is a locked read-modify-write that lands by atomic
rename; the file is the whole truth (nothing is filled in or repaired on read).

Locking: an exclusive lock on a sibling `<sid>.lock` file, polled without
blocking for up to LOCK_TIMEOUT_SECONDS. Windows uses msvcrt.locking on byte 0
(LK_NBLCK); POSIX uses fcntl.flock (LOCK_EX | LOCK_NB). Both are kernel locks
tied to the open handle, so a crashed process releases its lock and no stale
lock file can wedge a session; both also conflict between two handles in one
process, so threads exclude each other too. Readers take the same lock so a
read never overlaps the rename (on Windows a rename over an open file fails).
"""
from __future__ import annotations

import contextlib
import copy
import errno
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA_VERSION = 1
KEEP_PROMPTS = 20
LOCK_TIMEOUT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.02

MARKETPLACE = "plugins-kit"
PLUGIN = "presence-kit"

PRESENCE_VALUES = ("present", "afk")
RECAP_TRIGGERS = ("return", "afk-ending", "autonomous-done")
SET_TARGETS = ("afk", "autonomous", "normal")
SET_BYS = ("user", "model")
# `state set ... --by model` is gated by these behavior ids (contracts.md section 2).
GATE_IDS = {"afk": "afk-on-departure", "autonomous": "autonomous-on-handover"}

_SID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_PID_RE = re.compile(r"[^\s]{1,128}")
_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})"
)

# Document keys in file order (contracts.md section 3).
_KEYS = (
    "version", "presence", "afk_since", "goal", "goal_since", "recaps",
    "nudges", "greeted", "unresolved", "pending_return", "notices",
)

if sys.platform == "win32":
    import msvcrt

    _CONTENTION = (errno.EACCES, errno.EAGAIN, errno.EDEADLK, 36)

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    _CONTENTION = (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK)

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _CONTENTION:
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class StateError(Exception):
    """A state failure: `.kind` is the contracts.md section 3 kind, `.text` the message."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


# ---------------------------------------------------------------- ids and paths

def check_session_id(sid: Any) -> str:
    if sid is None or sid == "":
        raise StateError(
            "no-session",
            "no session id: pass --session or set CLAUDE_CODE_SESSION_ID",
        )
    if not isinstance(sid, str) or not _SID_RE.fullmatch(sid):
        raise StateError(
            "bad-session-id",
            f"session id '{sid}' must match [A-Za-z0-9_-]{{1,128}}",
        )
    return sid


def check_prompt_id(pid: Any) -> str:
    if not isinstance(pid, str) or not _PID_RE.fullmatch(pid):
        raise StateError(
            "bad-prompt-id",
            f"prompt id '{pid}' must be 1-128 characters without whitespace",
        )
    return pid


def data_root(env: Mapping[str, str]) -> Path:
    """`<base>`: CLAUDE_BOOTSTRAP_DATA_ROOT when set and non-empty, else ~/.claude/plugins/data."""
    override = env.get("CLAUDE_BOOTSTRAP_DATA_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "plugins" / "data"


def state_path(sid: str, env: Mapping[str, str]) -> Path:
    """`<base>/plugins-kit/presence-kit/sessions/<sid>.json` (sid validated)."""
    check_session_id(sid)
    return data_root(env) / MARKETPLACE / PLUGIN / "sessions" / f"{sid}.json"


def _lock_path(path: Path) -> Path:
    return path.with_suffix(".lock")


# ------------------------------------------------------------------ timestamps

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def format_timestamp(dt: datetime) -> str:
    """UTC `YYYY-MM-DDTHH:MM:SSZ`; a naive datetime is a caller bug."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("format_timestamp needs a timezone-aware datetime")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(s: Any) -> datetime | None:
    """Parse ISO-8601 with `Z` or a numeric offset to an aware UTC datetime.

    None for non-strings, naive strings and anything unparsable.
    """
    if not isinstance(s, str):
        return None
    match = _TS_RE.fullmatch(s)
    if match is None:
        return None
    base, fraction, zone = match.groups()
    micro = (fraction or "0")[:6].ljust(6, "0")
    offset = "+00:00" if zone == "Z" else zone
    try:
        parsed = datetime.fromisoformat(f"{base}.{micro}{offset}")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


# -------------------------------------------------------------------- document

def default_state() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "presence": "present",
        "afk_since": None,
        "goal": None,
        "goal_since": None,
        "recaps": {},
        "nudges": {},
        "greeted": False,
        "unresolved": [],
        "pending_return": None,
        "notices": [],
    }


def _is_ts_or_none(value: Any) -> bool:
    return value is None or parse_timestamp(value) is not None


def _validate(doc: Any) -> str | None:
    """Return None when `doc` is a valid state document, else the problem."""
    if not isinstance(doc, dict):
        return f"root must be an object, got {type(doc).__name__}"
    missing = [k for k in _KEYS if k not in doc]
    extra = [k for k in doc if k not in _KEYS]
    if missing:
        return f"missing key(s): {', '.join(missing)}"
    if extra:
        return f"unknown key(s): {', '.join(sorted(extra))}"
    version = doc["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        return f"'version' must be the integer {SCHEMA_VERSION}"
    if version != SCHEMA_VERSION:
        return f"unsupported version {version}; this build reads version {SCHEMA_VERSION}"
    if doc["presence"] not in PRESENCE_VALUES:
        return "'presence' must be \"present\" or \"afk\""
    for key in ("afk_since", "goal_since"):
        if not _is_ts_or_none(doc[key]):
            return f"'{key}' must be null or a UTC timestamp string"
    if doc["goal"] is not None and not isinstance(doc["goal"], str):
        return "'goal' must be null or a string"
    recaps = doc["recaps"]
    if not isinstance(recaps, dict) or any(
        not isinstance(k, str) or v not in RECAP_TRIGGERS for k, v in recaps.items()
    ):
        return "'recaps' must map prompt ids to return, afk-ending or autonomous-done"
    nudges = doc["nudges"]
    if not isinstance(nudges, dict) or any(
        not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int) or v < 0
        for k, v in nudges.items()
    ):
        return "'nudges' must map prompt ids to non-negative integers"
    if not isinstance(doc["greeted"], bool):
        return "'greeted' must be a boolean"
    for key in ("unresolved", "notices"):
        value = doc[key]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return f"'{key}' must be a list of strings"
    if doc["pending_return"] is not None and not isinstance(doc["pending_return"], str):
        return "'pending_return' must be null or a prompt id string"
    return None


def _read(path: Path) -> dict:
    """Read and validate one state file (caller holds the lock). Absent -> defaults."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return default_state()
    except OSError as exc:
        raise StateError("malformed", f"{path}: malformed state file -- cannot read: {exc}")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateError("malformed", f"{path}: malformed state file -- {exc}")
    problem = _validate(doc)
    if problem is not None:
        raise StateError("malformed", f"{path}: malformed state file -- {problem}")
    return doc


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive lock on the sibling lock file; polls up to LOCK_TIMEOUT_SECONDS."""
    lock = _lock_path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise StateError("lock-timeout", f"{path}: could not lock within 5 s")
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def _write_atomic(path: Path, doc: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    payload = json.dumps(doc, ensure_ascii=True, indent=2) + "\n"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _load(sid: str, env: Mapping[str, str]) -> tuple[bool, dict]:
    path = state_path(sid, env)
    if not path.is_file():
        return False, default_state()
    with _locked(path):
        return True, _read(path)


def load(sid: str, env: Mapping[str, str]) -> dict:
    """The session's state document; defaults when the file is absent (nothing is created)."""
    return _load(sid, env)[1]


def update(sid: str, fn: Callable[[dict], None], env: Mapping[str, str]) -> dict:
    """Locked read-modify-write. `fn` mutates the document in place.

    Writes (temp file, then os.replace) only when `fn` changed the document;
    returns the new document. `fn` must not call load/update for this session.
    """
    path = state_path(sid, env)
    with _locked(path):
        doc = _read(path)
        before = copy.deepcopy(doc)
        fn(doc)
        if doc != before:
            problem = _validate(doc)
            if problem is not None:
                raise StateError(
                    "malformed", f"{path}: refusing to write an invalid document -- {problem}"
                )
            _write_atomic(path, doc)
        return doc


# -------------------------------------------------------------------- mutators

def enter_afk(state: dict, since: str) -> None:
    """Already afk: nothing changes. Else afk, `afk_since` = since, `greeted` cleared."""
    if state["presence"] == "afk":
        return
    state["presence"] = "afk"
    state["afk_since"] = since
    state["greeted"] = False


def enter_autonomous(state: dict, goal: str, now: str) -> None:
    """Same goal: unchanged (a repeat is not a new hand-over). Else set goal and goal_since."""
    if state["goal"] == goal:
        return
    state["goal"] = goal
    state["goal_since"] = now


def set_normal(state: dict) -> None:
    """Revoke authority: clear goal and goal_since; presence is left alone."""
    state["goal"] = None
    state["goal_since"] = None


def set_present(state: dict) -> None:
    """Presence present; `afk_since` is kept for the recap window."""
    state["presence"] = "present"


def _touch(table: dict, pid: str, value: Any) -> None:
    table.pop(pid, None)  # a touched key moves to the end
    table[pid] = value


def mark_recap(state: dict, trigger: str, pid: str) -> None:
    """`recaps[pid] = trigger`, last write wins."""
    if trigger not in RECAP_TRIGGERS:
        raise ValueError(f"unknown recap trigger '{trigger}'")
    _touch(state["recaps"], check_prompt_id(pid), trigger)


def bump_nudge(state: dict, pid: str) -> int:
    """Count one more nudge for `pid`; returns the new count."""
    check_prompt_id(pid)
    count = state["nudges"].get(pid, 0) + 1
    _touch(state["nudges"], pid, count)
    return count


def add_notice(state: dict, cause: str) -> bool:
    """Record a cause once per session; True when it was new."""
    if cause in state["notices"]:
        return False
    state["notices"].append(cause)
    return True


def prune(state: dict) -> None:
    """Keep the KEEP_PROMPTS most recently touched pids of `recaps` and `nudges`."""
    for key in ("recaps", "nudges"):
        table = state[key]
        for pid in list(table)[: max(0, len(table) - KEEP_PROMPTS)]:
            del table[pid]


# ------------------------------------------------------------------ CLI effects

def state_get(sid: str, env: Mapping[str, str]) -> dict:
    exists, doc = _load(sid, env)
    return {"exists": exists, "state": doc}


def state_set(
    sid: str,
    target: str,
    by: str,
    goal: str | None,
    behaviors: Mapping[str, bool] | None,
    env: Mapping[str, str],
    now: datetime,
) -> dict:
    """`presence.py state set` effects; returns the section-2 stdout object.

    `behaviors` (the resolved config) is consulted only for `--by model` on
    afk/autonomous; `--by user` never needs config, so callers may pass None.
    """
    if target not in SET_TARGETS:
        raise ValueError(f"unknown state set target '{target}'")
    if by not in SET_BYS:
        raise ValueError(f"unknown --by value '{by}'")
    check_session_id(sid)
    if target == "normal":
        by = "user"
    else:
        if by == "model":
            if behaviors is None:
                raise ValueError("--by model needs the resolved behaviors")
            gate = GATE_IDS[target]
            if not behaviors[gate]:
                return {"skipped": gate, "reason": "off"}
        if target == "autonomous":
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("autonomous needs a non-blank goal")
            goal = goal.strip()
    stamp = format_timestamp(now)

    def apply(doc: dict) -> None:
        if target == "normal":
            set_normal(doc)
            return
        enter_afk(doc, stamp)
        if target == "autonomous":
            enter_autonomous(doc, goal, stamp)

    return {"written": True, "by": by, "state": update(sid, apply, env)}


def recap_done(
    sid: str, trigger: str, pid: str, env: Mapping[str, str], now: datetime
) -> dict:
    """`presence.py recap-done` effects (one locked update, then prune)."""
    if trigger not in RECAP_TRIGGERS:
        raise ValueError(f"unknown recap trigger '{trigger}'")
    check_session_id(sid)
    check_prompt_id(pid)

    def apply(doc: dict) -> None:
        mark_recap(doc, trigger, pid)
        if trigger == "return":
            set_present(doc)
            doc["greeted"] = False
            doc["unresolved"] = []
            doc["pending_return"] = None
        elif trigger == "autonomous-done":
            doc["goal"] = None
            doc["goal_since"] = None
        prune(doc)

    return {"recorded": trigger, "prompt_id": pid, "state": update(sid, apply, env)}
