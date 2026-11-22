"""Bind an explicit loss decision to the files and commits in one worktree."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import worktrees as wt
from worktree_config import relative_path
from worktree_safety import outputs


def file_fingerprint(root: Path, name: str) -> dict[str, Any]:
    relative_path(name)
    path = root / name
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return {"path": name, "link": os.readlink(path)}
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Ambiguous discard scope: non-file or nested repository at {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": name, "sha256": digest, "mode": stat.S_IMODE(info.st_mode)}


def inventory(record: dict[str, Any], upstream: str, declared: list[str], rebuildable: Sequence[str] = ()) -> dict[str, Any]:
    path = Path(record["path"])
    index = wt.git(path, "ls-files", "--stage", "-z")
    if any(line.startswith("160000 ") for line in index.split("\0")):
        raise ValueError("Ambiguous discard scope: submodules require separate preservation")
    flags = wt.git(path, "ls-files", "-v", "-z").split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in flags):
        raise ValueError("Ambiguous discard scope: assume-unchanged or skip-worktree index flags may hide work")
    untracked = sorted(p for p in wt.git(path, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p)
    reproducible, unique = outputs(path, declared, rebuildable)
    patch = wt.git(path, "diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv")
    return {"head": wt.git(path, "rev-parse", "HEAD"), "branch": wt.git(path, "branch", "--show-current"),
            "upstream": upstream, "commits": wt.git(path, "cherry", upstream, "HEAD").splitlines(),
            "tracked_status": wt.git(path, "status", "--porcelain=v1", "--untracked-files=no"),
            "tracked_patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "index_sha256": hashlib.sha256(index.encode()).hexdigest(),
            "untracked": untracked, "ignored": sorted(unique), "reproducible": sorted(reproducible),
            "files": [file_fingerprint(path, name) for name in sorted(set(untracked + unique))]}
