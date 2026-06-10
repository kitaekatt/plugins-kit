"""The global ``current`` pointer (spec section 2.6).

A single user-global pointer naming the one task currently being worked.
Storage is one small file, default:

    ~/.claude/plugins/data/plugins-kit/awesome-kit/current

Content: the ABSOLUTE path of the task folder (one line, resolved at write
time). The pointer file is user-global while canonical task ids are
project-relative -- a stored relative path would be ambiguous across projects
(set in project A, it would read stale from project B and be destructively
cleared), so the stored representation is absolute. The task id remains the
canonical project-relative path everywhere else. Empty or absent file means
nothing is current. The pointer path is parameter-injectable everywhere --
tests never touch the real one.

Stale handling (spec 2.6): the pointer is stale when the stored absolute
path's folder is missing (or the content is not a derivable absolute task
path at all) -- the ``current`` operation clears it and reports "none", no
error. Staleness detection lives in the callers (scripts/task.py and
state_ops.py); this module is pure storage: read / write / clear.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_POINTER_PATH = (
    Path.home()
    / ".claude"
    / "plugins"
    / "data"
    / "plugins-kit"
    / "awesome-kit"
    / "current"
)


def read_current(pointer_path: Path) -> str | None:
    """The stored line (an absolute task-folder path by contract), or None
    (empty/absent file). No staleness check here -- callers own that."""
    try:
        text = pointer_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    stripped = text.strip()
    if not stripped:
        return None
    # Content is one line by contract; tolerate trailing noise by taking the
    # first non-blank line.
    return stripped.splitlines()[0].strip()


def write_current(pointer_path: Path, folder: Path | str) -> None:
    """Point the pointer at the task ``folder``, stored as an absolute path
    (resolved at write time; creates parent dirs)."""
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = Path(folder).resolve()
    pointer_path.write_text(str(resolved) + "\n", encoding="utf-8")


def clear_current(pointer_path: Path) -> None:
    """Blank the pointer (spec 7.1 "clear the pointer"). Absent stays absent."""
    if pointer_path.exists():
        pointer_path.write_text("", encoding="utf-8")
