"""Script answerers: deterministic answers computed on read, never stored (summary-on-commit-plan.md, D2).

``answer(name, ctx)`` returns the answer mapping, or None when the script cannot answer (the next tier then
runs). A script is reproducible from git and the clock, so nothing here writes to disk.

``read_repo_log`` runs one ``git log --topo-order --name-only`` for a repository and groups it by folder
(``RepoLog``), so a snapshot of many tasks costs one git process.
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from .answers_types import CommitInfo, RepoLog, ScriptContext
from .question_inputs import activity_entries

GIT_TIMEOUT_S = 60.0
TASK_COMMITS_LIMIT = 10
_COMMIT_MARK = "\x01"
_FIELD_MARK = "\x1f"
_LOG_FORMAT = "--format=%x01%H%x1f%cI%x1f%s"
# Settings that would add text to, or reshape, the machine-read log.
_GIT_FIXED = ("-c", "core.quotepath=off", "-c", "log.showSignature=false")


class AnswerScriptError(RuntimeError):
    """A script answerer or the repository log could not run."""


def _git(top: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(top), *_GIT_FIXED, *args], capture_output=True, text=True,
            encoding="utf-8", timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AnswerScriptError(f"git {' '.join(args)} could not run in {top}: {exc}") from exc


def repo_top_of(folder: Path) -> Path | None:
    """The top of the work tree holding ``folder``; None when it is not inside one."""
    proc = _git(Path(folder), "rev-parse", "--show-toplevel")
    return Path(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None


def read_repo_log(top: Path) -> RepoLog:
    """The repository's topological log up to HEAD, grouped by folder."""
    top = Path(top)
    head_proc = _git(top, "rev-parse", "--verify", "-q", "HEAD")
    if head_proc.returncode == 1:  # a repository whose HEAD names no commit yet
        return RepoLog.build(top, None, (), ())
    if head_proc.returncode != 0:
        raise AnswerScriptError(f"git rev-parse HEAD failed in {top}: {head_proc.stderr.strip()}")
    head = head_proc.stdout.strip()
    proc = _git(top, "log", "--topo-order", "--no-renames", "--name-only", _LOG_FORMAT, "HEAD")
    if proc.returncode != 0:
        raise AnswerScriptError(f"git log failed in {top}: {proc.stderr.strip()}")
    commits: list[CommitInfo] = []
    changes: list[list[str]] = []
    for chunk in proc.stdout.split(_COMMIT_MARK)[1:]:
        header, _, names = chunk.partition("\n")
        sha, date, subject = header.split(_FIELD_MARK)
        commits.append(CommitInfo(sha=sha, date=date, subject=subject))
        changes.append([line for line in names.split("\n") if line])
    return RepoLog.build(top, head, commits, changes)


def _activity(ctx: ScriptContext) -> Mapping[str, Any] | None:
    entries = activity_entries(ctx.folder)
    if not entries:
        return None
    last = entries[0]
    days = (ctx.now.date() - datetime.date.fromisoformat(last.date)).days
    return {
        "days_since": max(0, days),  # a stamp written ahead of this clock reads as today
        "last_at": f"{last.date} {last.time}" if last.time else last.date,
        "last_entry": last.detail,
    }


def _task_commits(ctx: ScriptContext) -> Mapping[str, Any] | None:
    if ctx.log is None or ctx.log.head is None:
        return None
    commits = ctx.log.touching(ctx.rel_folder)[:TASK_COMMITS_LIMIT]
    return {"commits": [{"sha": c.sha, "date": c.date, "subject": c.subject} for c in commits]}


SCRIPTS: Mapping[str, Callable[[ScriptContext], Mapping[str, Any] | None]] = {
    "activity": _activity,
    "task_commits": _task_commits,
}


def script_names() -> tuple[str, ...]:
    return tuple(SCRIPTS)


def answer(name: str, ctx: ScriptContext) -> Mapping[str, Any] | None:
    """Run the named script; None when it cannot answer."""
    script = SCRIPTS.get(name)
    if script is None:
        raise AnswerScriptError(f"unknown script {name!r}; known scripts: {list(SCRIPTS)}")
    return script(ctx)
