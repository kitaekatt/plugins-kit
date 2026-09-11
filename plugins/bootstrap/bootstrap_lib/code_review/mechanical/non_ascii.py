"""Detect non-ASCII characters on added lines."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import MechanicalFinding, MechanicalSnapshot


def has_non_ascii(text: str) -> bool:
    """Return whether text contains a non-ASCII code point."""
    return any(ord(character) >= 128 for character in text)


def precondition(snapshot: MechanicalSnapshot) -> bool:
    """Return whether added lines were parsed from the review snapshot."""
    return snapshot.added_lines is not None


def scan(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Return one located finding per added line containing non-ASCII text."""
    assert snapshot.added_lines is not None
    findings: list[MechanicalFinding] = []
    for lineno, text in snapshot.added_lines:
        for character in text:
            if ord(character) >= 128:
                findings.append(
                    {
                        "check": "non_ascii",
                        "line": lineno,
                        "detail": (
                            f"U+{ord(character):04X} ({character!r}) in: "
                            f"{text.strip()[:120]}"
                        ),
                    }
                )
                break
    return tuple(findings)
