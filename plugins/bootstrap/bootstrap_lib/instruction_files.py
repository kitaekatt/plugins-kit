"""Resolve a directory's agent instruction file.

Precedence: a directory's CLAUDE.md wins; AGENTS.md is read only
when that directory has no CLAUDE.md. The deferred-defects file keeps the
CLAUDE- prefix whichever instruction file it belongs to.

Stdlib-only. A byte-identical copy lives at
plugins/skills-kit/skills_kit_lib/instruction_files.py (drift-tested).
"""

from __future__ import annotations

from pathlib import Path

INSTRUCTION_FILE_NAMES = ("CLAUDE.md", "AGENTS.md")  # priority order
DEFECTS_FILE_NAME = "CLAUDE-potential-defects.md"


def resolve_instruction_file(directory: Path) -> Path | None:
    """Return the instruction file Claude would read in *directory*, or None."""
    for name in INSTRUCTION_FILE_NAMES:
        candidate = Path(directory) / name
        if candidate.is_file():
            return candidate
    return None


def is_instruction_file_name(name: str) -> bool:
    """True when *name* is CLAUDE.md or AGENTS.md (case-insensitive)."""
    return name.lower() in {n.lower() for n in INSTRUCTION_FILE_NAMES}


def is_active_instruction_file(path: Path) -> bool:
    """True when *path* is the file its directory resolves to.

    An AGENTS.md beside a CLAUDE.md is shadowed and returns False.
    """
    path = Path(path)
    if not is_instruction_file_name(path.name):
        return False
    resolved = resolve_instruction_file(path.parent)
    return resolved is not None and resolved.name.lower() == path.name.lower()
