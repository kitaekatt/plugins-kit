"""Which Claude Code harness session is calling: its session id, its process, and where its transcript is.

Claude Code exports two variables into the subprocesses it starts (Bash and PowerShell tool commands and
hook commands), documented at https://code.claude.com/docs/en/env-vars:

- ``CLAUDE_CODE_SESSION_ID`` -- the session id; it matches the hook input's ``session_id`` and is updated on
  ``/clear``.
- ``CLAUDE_PID`` -- the Claude Code process's own process id (Claude Code 2.1.214 or later).

The process is NOT found by looking for "claude" in the names of the caller's ancestors: a Bash tool command
runs under a short-lived ``zsh -c "source ~/.claude/shell-snapshots/..."`` shell whose command line contains
"claude", and that shell dies when the command returns. ``CLAUDE_PID`` is accepted only when it names a live
ancestor of the caller, so a stale or copied variable can never name an unrelated process.

Nothing here imports the dashboard package; ``task.py`` and every other ``task_system`` module may use it.
"""

from __future__ import annotations

import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import psutil

from . import procstamp

SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"
PID_ENV = "CLAUDE_PID"
AGENT_DEPTH_ENV = "TASK_KIT_AGENT_DEPTH"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
HARNESS = "claude-code"
_SESSION_ID_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


UNMEASURED_PLATFORMS = ("win32", "cygwin", "msys")
WINDOWS_REFUSAL = (
    "working-session registration is not supported on Windows: the CLAUDE_PID ancestry check from Git Bash and "
    "PowerShell, and the process creation-time stamp that decides running or not running, have not been "
    "measured there, so a record could name the wrong process. Nothing is registered; "
    "run /task work from macOS or Linux to register")


class IdentityError(RuntimeError):
    """The caller's harness process cannot be established; the message says why."""


@dataclass(frozen=True)
class Identity:
    session_id: str
    pid: int
    stamp: procstamp.Stamp
    harness: str = HARNESS


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def session_id(env: Mapping[str, str] | None = None) -> str | None:
    """The harness session id, or None when this is not a Claude Code subprocess."""
    value = _environ(env).get(SESSION_ID_ENV, "").strip()
    return value or None


def in_dashboard_run(env: Mapping[str, str] | None = None) -> bool:
    """True inside a dashboard run's command: such a process shows the dashboard's work and never registers."""
    return AGENT_DEPTH_ENV in _environ(env)


def claude_process(env: Mapping[str, str] | None = None, start_pid: int | None = None) -> psutil.Process:
    """The Claude Code process: ``CLAUDE_PID``, accepted only as a live ancestor of ``start_pid`` (default: this process)."""
    raw = _environ(env).get(PID_ENV, "").strip()
    if not raw:
        raise IdentityError(
            f"{PID_ENV} is not set, so the Claude Code process cannot be established "
            "(Claude Code 2.1.214 or later sets it in the subprocesses it starts)")
    if not raw.isdigit() or int(raw) <= 0:
        raise IdentityError(f"{PID_ENV} is {raw!r}, not a process id")
    pid = int(raw)
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            raise psutil.NoSuchProcess(pid)
    except psutil.NoSuchProcess:
        raise IdentityError(f"{PID_ENV} names process {pid}, which is not running") from None
    start = os.getpid() if start_pid is None else start_pid
    try:
        ancestors = {parent.pid for parent in psutil.Process(start).parents()}
    except psutil.NoSuchProcess:
        raise IdentityError(f"process {start} is not running, so its ancestry cannot be read") from None
    except psutil.AccessDenied as exc:
        raise IdentityError(f"the ancestry of process {start} cannot be read: {exc}") from exc
    if pid not in ancestors:
        raise IdentityError(
            f"{PID_ENV} names process {pid}, which is not an ancestor of process {start}; "
            "it is a stale or copied value, so it is not used")
    return process


def resolve_identity(env: Mapping[str, str] | None = None, start_pid: int | None = None, *,
                     platform: str | None = None) -> Identity:
    """The caller's session id, harness process and process stamp; raises :class:`IdentityError` when any is missing.

    Refuses on Windows (:data:`WINDOWS_REFUSAL`): the process verification below was never measured there.
    ``platform`` exists so a test can compose the refusal on any host.
    """
    if (sys.platform if platform is None else platform) in UNMEASURED_PLATFORMS:
        raise IdentityError(WINDOWS_REFUSAL)
    env = _environ(env)
    sid = session_id(env)
    if sid is None:
        raise IdentityError(f"{SESSION_ID_ENV} is not set: this is not a Claude Code subprocess")
    process = claude_process(env, start_pid)
    try:
        stamp = procstamp.stamp_of(process.pid)
    except procstamp.StampError as exc:
        raise IdentityError(str(exc)) from exc
    if stamp is None:
        raise IdentityError(f"{PID_ENV} names process {process.pid}, which stopped running")
    return Identity(sid, process.pid, stamp)


@dataclass(frozen=True)
class TranscriptLookup:
    """``path`` is home-relative (``~/...``) when exactly one transcript was found under the home directory."""

    path: str | None
    note: str | None


def find_transcript(session: str, env: Mapping[str, str] | None = None, home: Path | None = None) -> TranscriptLookup:
    """The one file ``${CLAUDE_CONFIG_DIR:-~/.claude}/projects/*/<session>.jsonl``, home-relative; else a note why not."""
    env = _environ(env)
    if not _SESSION_ID_SAFE.match(session):
        return TranscriptLookup(None, f"the session id {session!r} cannot name a transcript")
    home_dir = Path(os.path.expanduser("~")) if home is None else Path(home)
    configured = env.get(CONFIG_DIR_ENV, "").strip()
    config_dir = Path(configured) if configured else home_dir / ".claude"
    pattern = os.path.join(glob.escape(str(config_dir)), "projects", "*", f"{session}.jsonl")
    matches = sorted(glob.glob(pattern))
    if not matches:
        return TranscriptLookup(None, f"no transcript for session {session[:8]} under {config_dir / 'projects'}")
    if len(matches) > 1:
        return TranscriptLookup(None, f"{len(matches)} transcripts match session {session[:8]} under "
                                      f"{config_dir / 'projects'}; none is recorded")
    found = Path(matches[0])
    for base in (home_dir, home_dir.resolve()):
        for candidate in (found, found.resolve()):
            try:
                relative = candidate.relative_to(base)
            except ValueError:
                continue
            return TranscriptLookup("~/" + relative.as_posix(), None)
    return TranscriptLookup(None, f"the transcript for session {session[:8]} is not under the home directory; "
                                  "it is not recorded")
