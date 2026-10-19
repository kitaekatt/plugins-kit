"""Read-only git facts the dashboard records around a run.

Only read commands run here (``rev-parse``, ``symbolic-ref``, ``status``,
``merge-base --is-ancestor``). A project that is not a git repository yields
``GitFacts(vcs=False)``: the run still executes and the receipt says that the
branch and ancestry checks did not apply. (task-kit privileges git as the
automated VCS; see plugins/CLAUDE.md, the register.)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 60.0


class GitError(RuntimeError):
    """A git command failed in a way that is not 'not a repository'."""


@dataclass(frozen=True)
class GitFacts:
    vcs: bool
    head: str | None = None
    branch: str | None = None  # None on a detached HEAD
    status: tuple[str, ...] = ()


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)} could not run in {root}: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {root}: {proc.stderr.strip()}")
    return proc


def is_repository(root: Path) -> bool:
    return _git(root, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"


def facts(root: Path) -> GitFacts:
    root = Path(root)
    if not is_repository(root):
        return GitFacts(vcs=False)
    head_proc = _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False)
    head = head_proc.stdout.strip() or None  # an unborn branch has no HEAD yet
    branch_proc = _git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    branch = branch_proc.stdout.strip() or None
    status = tuple(line for line in _git(root, "status", "--porcelain").stdout.splitlines() if line)
    return GitFacts(vcs=True, head=head, branch=branch, status=status)


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    proc = _git(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise GitError(f"git merge-base --is-ancestor failed in {root}: {proc.stderr.strip()}")
