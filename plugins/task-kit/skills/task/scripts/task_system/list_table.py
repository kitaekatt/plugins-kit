"""The ``task.py list`` table: staleness, claiming session and summary, freshest task first."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

_SUMMARY_WIDTH = 70
_TAIL_BYTES = 2_000_000
_TITLE_RE = re.compile(rb'"customTitle":"([^"]*)"')
HEADER = ("Stale", "Pri", "Session", "Task", "Summary")


def newest_mtime(folder: Path) -> float | None:
    """Newest file mtime under ``folder``; ``answers/`` is skipped because the system regenerates it."""
    if not folder.is_dir():
        return None
    newest = 0.0
    for current, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "answers"]
        for name in files:
            newest = max(newest, os.path.getmtime(os.path.join(current, name)))
    return newest or None


def stale_label(mtime: float | None, now: float) -> str:
    """Hours under a day (``0h``..``23h``), whole days from 24 hours; ``-`` without a folder."""
    if mtime is None:
        return "-"
    hours = max(0.0, now - mtime) / 3600
    return f"{int(hours)}h" if hours < 24 else f"{int(hours // 24)}d"


def _last_titles(transcript: Path) -> list[bytes]:
    """customTitle values of a transcript: the last 2 MB first (the title is re-emitted), the whole file if none."""
    size = transcript.stat().st_size
    with transcript.open("rb") as handle:
        if size > _TAIL_BYTES:
            handle.seek(size - _TAIL_BYTES)
            found = _TITLE_RE.findall(handle.read())
            if found:
                return found
            handle.seek(0)
        return _TITLE_RE.findall(handle.read())


def session_name(record) -> str:
    """The session's name: the last ``customTitle`` of its transcript, else the first 8 characters of its id."""
    if record.transcript_path and record.owned:
        transcript = Path(record.transcript_path).expanduser()
        if transcript.is_file():
            found = _last_titles(transcript)
            if found:
                return found[-1].decode("utf-8")
    return record.session_id[:8]


def _cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ")


def _row(view, now: float) -> tuple[float, tuple[str, ...]]:
    mtime = newest_mtime(view.project_root / view.id) if view.status != "remote" else None
    ident = f"{view.id} @{view.host}" if view.status == "remote" and view.host else view.id
    if view.hidden:
        ident += f" (hidden until {view.hidden_until})"
    names = sorted({session_name(r) for r in view.sessions})
    summary = view.summary or view.title or "-"
    if len(summary) > _SUMMARY_WIDTH:
        summary = summary[: _SUMMARY_WIDTH - 3] + "..."
    cells = (
        stale_label(mtime, now),
        view.priority or "-",
        ", ".join(names) or "-",
        ident,
        summary,
    )
    return (mtime if mtime is not None else 0.0), tuple(_cell(c) for c in cells)


def format_table(views, now: float | None = None) -> list[str]:
    """Markdown table lines for ``views``, freshest first; tasks without a local folder come last."""
    now = time.time() if now is None else now
    rows = sorted((_row(v, now) for v in views), key=lambda pair: -pair[0])
    lines = ["| " + " | ".join(HEADER) + " |", "|" + "---|" * len(HEADER)]
    lines += ["| " + " | ".join(cells) + " |" for _, cells in rows]
    return lines
