"""Safe folder relocation and removal for the task system.

``shutil.move`` and ``shutil.rmtree`` are not safe for task folders. On
Windows, ``os.rename`` of a directory can fail with PermissionError and
``shutil.move`` then falls back to copy-then-delete; a read-only file
(Perforce checks files out read-only) makes the delete fail after the copy
completed, leaving the task split across two folders.

This module is the one place that relocates or removes a task folder:

- ``relocate_tree`` renames; on failure it copies, VERIFIES the copy
  byte-for-byte, and only then removes the source. A copy that fails or does
  not verify is removed and the source stays the single complete folder.
- ``remove_tree`` clears the read-only bit and retries instead of failing
  (never ``ignore_errors``, which leaves silent litter).
- A failure raises ``RelocationError`` carrying ``authoritative``, the one
  folder that is complete, so the caller can name it plainly.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import stat
import sys
from pathlib import Path
from typing import Callable


class RelocationError(Exception):
    """A relocation or removal failed.

    ``authoritative`` is the complete folder that survives (None when the
    operation was a removal and nothing survives by design). ``leftover`` is
    a partial folder the failed cleanup could not remove, if any."""

    def __init__(
        self,
        message: str,
        *,
        authoritative: Path | None = None,
        leftover: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.authoritative = authoritative
        self.leftover = leftover


def _clear_readonly_and_retry(
    func: Callable[..., object], path: str, _exc: object
) -> None:
    """rmtree error handler: make the entry (and its parent) writable, then
    retry the failed call once. A second failure propagates."""
    target = Path(path)
    for p in (target, target.parent):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
    func(path)


def remove_tree(folder: Path) -> None:
    """Remove a folder tree, clearing read-only bits. Raises
    ``RelocationError`` if it still cannot be removed."""
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(folder, onexc=_clear_readonly_and_retry)
        else:
            shutil.rmtree(
                folder, onerror=lambda f, p, e: _clear_readonly_and_retry(f, p, e)
            )
    except OSError as exc:
        raise RelocationError(
            f"could not remove {folder}: {exc}", leftover=folder
        ) from exc


def _files(root: Path) -> dict[str, Path]:
    return {
        p.relative_to(root).as_posix(): p
        for p in root.rglob("*")
        if p.is_file()
    }


def _dirs(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()}


def differing_paths(src: Path, copy: Path) -> list[str]:
    """Relative paths of files in ``src`` that are missing from ``copy`` or
    differ from it byte for byte. Empty means every file in ``src`` is safely
    held by ``copy``. The one comparison used both to verify a fresh copy and
    to decide whether a leftover source may be removed."""
    theirs = _files(copy)
    bad: list[str] = []
    for rel, path in sorted(_files(src).items()):
        other = theirs.get(rel)
        if other is None or not filecmp.cmp(path, other, shallow=False):
            bad.append(rel)
    return bad


def _verify_copy(src: Path, dst: Path) -> str | None:
    """None when ``dst`` is a complete copy of ``src``; else the reason."""
    bad = differing_paths(src, dst)
    if bad:
        return f"copy is missing or differs at {bad[:3]}"
    if _files(src).keys() != _files(dst).keys() or _dirs(src) != _dirs(dst):
        return "copy has a different file or directory set"
    return None


def absorb_leftover_source(src: Path, copy: Path) -> list[str]:
    """Repair a split state: ``src`` and ``copy`` both exist. When every file
    in ``src`` is present in ``copy`` with identical bytes, ``copy`` is
    authoritative and ``src`` is removed (read-only aware); returns []. When
    any file differs or is missing, NOTHING is changed and the differing
    paths are returned so a human decides. Never removes the only copy of
    any file. ``RelocationError`` if the removal itself fails."""
    bad = differing_paths(src, copy)
    if bad:
        return bad
    remove_tree(src)
    return []


def relocate_tree(src: Path, dst: Path) -> None:
    """Move folder ``src`` to the (absent) ``dst``.

    On return ``dst`` is the one complete folder and ``src`` is gone. On
    ``RelocationError``, ``authoritative`` names the complete folder (``src``
    when nothing was lost; ``dst`` when the copy verified but the source
    could not be fully removed, in which case ``leftover`` names the partial
    source)."""
    if dst.exists():
        raise RelocationError(
            f"destination {dst} already exists; nothing was moved",
            authoritative=src,
        )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RelocationError(
            f"cannot create {dst.parent}: {exc}; nothing was moved",
            authoritative=src,
        ) from exc
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass
    # Rename failed (e.g. WinError 5 on Windows): copy, verify, then remove.
    try:
        shutil.copytree(src, dst)
        problem = _verify_copy(src, dst)
    except OSError as exc:
        problem = f"copy failed: {exc}"
    if problem is not None:
        cleanup = ""
        try:
            if dst.exists():
                remove_tree(dst)
        except RelocationError:
            cleanup = f" (a partial copy remains at {dst}; delete it)"
        raise RelocationError(
            f"could not move {src} to {dst}: {problem}; the complete "
            f"folder is still at {src}{cleanup}",
            authoritative=src,
            leftover=dst if cleanup else None,
        )
    try:
        remove_tree(src)
    except RelocationError as exc:
        raise RelocationError(
            f"moved {src} to {dst} but could not remove the source "
            f"({exc}); the complete folder is {dst}; {src} is a partial "
            "leftover -- delete it",
            authoritative=dst,
            leftover=src,
        ) from exc
