"""The statusline pointer: ``<data dir>/statusline/<session id>`` holds the absolute path of the task folder the
session works. ``task.py work`` writes it and ``sessions release`` removes it; the claude-ui-kit segment
(``plugins/task-kit/statusline/segment.sh``) reads it. A pointer is a machine-local cache, never a record.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DIRNAME = "statusline"


def pointer_path(data_dir: Path, session_id: str) -> Path:
    return Path(data_dir) / DIRNAME / session_id


def write(data_dir: Path, session_id: str, task_dir: Path) -> None:
    """Point ``session_id`` at ``task_dir``, atomically; a repeat with the same task rewrites the same bytes."""
    target = pointer_path(data_dir, session_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = os.path.realpath(task_dir) + "\n"
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".ptr-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def remove(data_dir: Path, session_id: str, task_dir: Path) -> bool:
    """Remove the pointer when it names ``task_dir`` (a session that moved on to another task keeps its pointer)."""
    target = pointer_path(data_dir, session_id)
    try:
        current = target.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return False
    if os.path.normcase(current) != os.path.normcase(os.path.realpath(task_dir)):
        return False
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
