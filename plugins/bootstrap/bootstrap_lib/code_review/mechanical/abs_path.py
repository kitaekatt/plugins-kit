"""Detect absolute paths on added lines."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import MechanicalFinding, MechanicalSnapshot

_WIN_ABS_RE = re.compile(r"[A-Za-z]:[\\/]")
_POSIX_ABS_RE = re.compile(r"(?:^|[\s(\"'`])/[A-Za-z0-9._-]+/")


def find_abs_path(text: str) -> re.Match[str] | None:
    """Return the first absolute-path match in text, if one exists."""
    return _WIN_ABS_RE.search(text) or _POSIX_ABS_RE.search(text)


def precondition(snapshot: MechanicalSnapshot) -> bool:
    """Return whether added lines were parsed from the review snapshot."""
    return snapshot.added_lines is not None


def scan(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Return one located finding per added line containing an absolute path."""
    assert snapshot.added_lines is not None
    findings: list[MechanicalFinding] = []
    for lineno, text in snapshot.added_lines:
        match = find_abs_path(text)
        if match:
            findings.append(
                {
                    "check": "abs_path",
                    "line": lineno,
                    "detail": f"{match.group(0).strip()!r} in: {text.strip()[:120]}",
                }
            )
    return tuple(findings)
