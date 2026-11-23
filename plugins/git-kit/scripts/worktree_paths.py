"""Filesystem identities for worktree discovery and duplicate retirement."""

from __future__ import annotations

import os
from pathlib import Path


def case_insensitive(parent: Path) -> bool:
    """Observe case aliases without writing a probe into a repository."""
    for path in (parent, *parent.parents):
        alternate = path.with_name(path.name.swapcase()) if path.name else path
        if str(alternate) == str(path):
            continue
        try:
            return alternate.samefile(path)
        except FileNotFoundError:
            return False
    return False


def path_identity(path: Path) -> tuple:
    """Use device/inode, or an existing parent's identity and missing suffix."""
    path = path.absolute()
    suffix = []
    parent = path
    while True:
        try:
            info = parent.stat()
            break
        except FileNotFoundError:
            suffix.append(parent.name)
            parent = parent.parent
    if not suffix:
        return ("inode", info.st_dev, info.st_ino)
    missing = str(Path(*reversed(suffix)))
    missing = os.path.normcase(missing).casefold() if case_insensitive(parent) else missing
    return ("missing", info.st_dev, info.st_ino, missing)


def registration_matches(registration: dict[str, str], path: Path) -> bool:
    """Match a Git registration by filesystem identity, independently of locks."""
    return path_identity(Path(registration["worktree"])) == path_identity(path)
