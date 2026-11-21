"""Shared remote identity, OS locks, and guarded legacy pool entry points.

worktree_lifecycle owns mutation and records in git-kit's user-scoped data home.
Legacy namespace readers remain available for inventory during migration.
"""

from __future__ import annotations

import json
import errno
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import worktrees as wt

POOL_DIR = "ready"
SPARE_PREFIX = "spare-"
BUILD_ENV = "GIT_KIT_POOL_BUILD"
EXIT_EMPTY = 4
KEEP_FAILED = 5


class PoolEmpty(Exception):
    """The hard block: no ready worktree. The message names the reasons."""


def remote_url(repo: Path) -> str:
    remotes = wt.git(repo, "remote").splitlines()
    name = "origin" if "origin" in remotes else (remotes[0] if len(remotes) == 1 else None)
    if name is None:
        raise ValueError(f"{repo}: the pool needs a remote named origin or exactly one remote")
    return wt.git(repo, "remote", "get-url", "--", name)


def repo_identity(url: str) -> str:
    name = re.split(r"[/:\\]", url.rstrip("/\\"))[-1].removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name.endswith("."):
        raise ValueError(f"Cannot name a pool folder from remote URL: {url}")
    return name


def pool_dir(settings: wt.WorktreeSettings, url: str) -> Path:
    return settings.root / repo_identity(url) / POOL_DIR


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def pool_source(pool: Path, repo: Path, url: str) -> Path:
    """Legacy metadata requires migration before lifecycle mutation."""
    raise ValueError("Legacy pool source metadata requires migration; use the configured lifecycle")


def spares(pool: Path) -> list[Path]:
    if not pool.is_dir():
        return []
    return sorted(p for p in pool.iterdir() if p.name.startswith(SPARE_PREFIX) and p.is_dir())


@contextmanager
def _file_lock(path: Path, blocking: bool) -> Iterator[bool]:
    """Yield True when this process holds the lock; the OS frees it at exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if os.name == "nt" and path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except OSError as exc:
            if blocking or exc.errno not in {errno.EAGAIN, errno.EACCES}:
                raise
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def refill_lock(pool: Path):
    """Held by the one running refill; a second refill stands down."""
    return _file_lock(pool / ".refill.lock", blocking=False)


def fresh_upstream(pool: Path, source: Path) -> tuple[str, str]:
    """Fetch the source clone (one fetch at a time: parallel fetches race on ref
    locks) and return (upstream ref, its commit)."""
    upstream = wt.main_upstream(source)
    with _file_lock(pool / ".fetch.lock", blocking=True):
        wt.refresh(source)
    return upstream, wt.git(source, "rev-parse", "--verify", f"{upstream}^{{commit}}")


def refill(repo: Path, settings: wt.WorktreeSettings) -> dict[str, Any]:
    """Compatibility entry point; the lifecycle owns all mutation gates."""
    from worktree_lifecycle import Lifecycle
    return Lifecycle(repo).maintain()


def start_refill(repo: Path, script: Path) -> None:
    from worktree_lifecycle import Lifecycle
    result = Lifecycle(repo).request_maintenance()
    if result["outcome"] == "failed":
        raise ValueError(f"Maintenance launch failed: {result['reason']}")


def status(repo: Path, settings: wt.WorktreeSettings) -> dict[str, Any]:
    from worktree_lifecycle import Lifecycle
    return Lifecycle(repo).status()


def claim(pool: Path, destination: Path) -> Path | None:
    raise ValueError("Raw claim requires migration; use take --agent --session after configure")


def take(repo: Path, settings: wt.WorktreeSettings, task: str, script: Path) -> dict[str, Any]:
    raise ValueError("Task-named claims require configure/migration; use take --agent --session")


def build_one(pool: Path, source: Path, steps: list[list[str]]) -> Path:
    raise ValueError("Raw builds are unavailable; configure the repository and request maintenance")
