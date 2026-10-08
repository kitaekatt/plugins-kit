"""Atomic ASCII JSON files for small run-state stores. Standard library only.

Shared by :mod:`content_pipeline.execution.deadlines` and
:mod:`content_pipeline.execution.failure_cache`. A write goes to a temporary
file in the same directory, is fsynced, and replaces the target, so a reader
sees the old file or the new one, never a torn one. Callers serialize their
own read-modify-write cycles; two processes writing one file are
last-writer-wins.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional, Tuple


def read(
    path: Path, empty: dict, validate: Callable[[object], None], label: str
) -> Tuple[dict, Optional[str]]:
    """Return ``(payload, report)``.

    A missing file reads as a copy of ``empty`` with no report. A file that
    cannot be read, decoded or validated also reads as ``empty``, and the
    report names the problem.
    """
    if not path.exists():
        return copy.deepcopy(empty), None
    try:
        with path.open("r", encoding="ascii") as stream:
            payload = json.load(stream)
        validate(payload)
        return payload, None
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return copy.deepcopy(empty), "%s ignored: %s" % (label, exc)


def write(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically as compact, sorted ASCII JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
