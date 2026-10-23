"""Filesystem receipts for a ``workspace`` call: what a run left in its directory.

A workspace call hands an agent a directory and lets it write there. The model's
text says nothing reliable about what it wrote, so the caller needs a receipt
taken from the filesystem itself. This module takes it:

* :func:`scan` walks the directory and records every regular file as
  ``(size, sha256)`` keyed by its relative POSIX path. It is taken immediately
  before the call and immediately after.
* :func:`diff` returns the files that are new or whose content changed, in
  stable path order, as ``{"path", "size", "sha256"}`` records. A deleted file
  is not a created or modified file and is not listed.
* :func:`check_expected` names the ``expect_files`` entries that are not regular
  files after the call.

Only regular files count. A symbolic link is neither followed nor listed (so the
scan cannot be led outside the directory), and a directory contributes through
the files beneath it. Both scans hash every file's bytes: a size-and-mtime
shortcut would miss a same-size rewrite inside one timestamp tick, and a
receipt that can miss a modification is not a receipt. The cost is one read of
the directory's content per scan, which a caller who points a workspace at a
large tree should expect.

Nothing here infers what a file IS: a ``.png`` that is not a PNG is listed like
any other file. Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Tuple

_CHUNK = 1 << 20


@dataclass(frozen=True)
class FileRecord:
    """One regular file: its relative POSIX path, size in bytes and SHA-256 hex digest."""

    path: str
    size: int
    sha256: str

    def to_json(self) -> Dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def _digest(path: str) -> Tuple[int, str]:
    sha = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            sha.update(chunk)
    return size, sha.hexdigest()


def scan(root: Path) -> Dict[str, FileRecord]:
    """Every regular file beneath ``root``, keyed by relative POSIX path.

    Raises ``OSError`` when ``root`` is not a directory or a file cannot be
    read: a receipt that skipped an unreadable file would be silently
    incomplete.
    """
    base = os.fspath(root)
    if not os.path.isdir(base):
        raise NotADirectoryError(f"workspace directory does not exist: {base}")
    found: Dict[str, FileRecord] = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(current, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            size, sha = _digest(full)
            relative = PurePosixPath(*Path(os.path.relpath(full, base)).parts).as_posix()
            found[relative] = FileRecord(relative, size, sha)
    return found


def diff(before: Mapping[str, FileRecord], after: Mapping[str, FileRecord]) -> List[FileRecord]:
    """Files in ``after`` that are absent from ``before`` or whose digest differs, by path."""
    changed = [
        record
        for path, record in after.items()
        if path not in before or before[path].sha256 != record.sha256
    ]
    return sorted(changed, key=lambda record: record.path)


def confine_relative(name: object, *, what: str) -> str:
    """``name`` as a normalized relative POSIX path, or ``ValueError``.

    Refuses a non-string, an empty or NUL-bearing name, an absolute path
    (POSIX, drive-lettered or UNC), a backslash separator, and any ``..``
    component, so a name can only point beneath the directory it is joined to.
    """
    if not isinstance(name, str) or not name or "\0" in name:
        raise ValueError(f"{what} must be a non-empty relative path string")
    if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ValueError(f"{what} {name!r} must be a relative POSIX path (no backslash, drive or leading slash)")
    parts = PurePosixPath(name).parts
    if not parts or any(part in ("..", "") for part in parts):
        raise ValueError(f"{what} {name!r} must stay beneath the working directory (no '..')")
    normalized = PurePosixPath(*[p for p in parts if p != "."]).as_posix()
    if normalized in ("", "."):
        raise ValueError(f"{what} {name!r} names the working directory itself, not a file")
    return normalized


def check_expected(root: Path, expected: Iterable[str]) -> List[str]:
    """The ``expected`` relative paths that are not regular files beneath ``root`` when it is called.

    A path that is a directory, a dangling or live symbolic link, or absent is
    reported; so is one whose parent chain passes through a symbolic link, which
    could otherwise point the check outside ``root``.
    """
    base = Path(os.fspath(root))
    missing: List[str] = []
    for name in expected:
        cursor = base
        ok = True
        for part in PurePosixPath(name).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                ok = False
                break
        if ok and cursor.is_file():
            continue
        missing.append(name)
    return missing


__all__ = ["FileRecord", "check_expected", "confine_relative", "diff", "scan"]
