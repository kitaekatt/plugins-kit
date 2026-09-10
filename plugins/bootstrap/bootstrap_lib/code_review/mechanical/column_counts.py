"""Detect inconsistent CSV and TSV row widths."""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import MechanicalFinding, MechanicalSnapshot


def _delimiter(file: str) -> str | None:
    suffix = file.lower().rsplit(".", 1)[-1] if "." in file else ""
    return {"csv": ",", "tsv": "\t"}.get(suffix)


def precondition(snapshot: MechanicalSnapshot) -> bool:
    """Run only when a CSV/TSV extension establishes dialect and header."""
    if snapshot.post_image_text is None or _delimiter(snapshot.file) is None:
        return False
    first = next((line for line in snapshot.post_image_text.splitlines() if line.strip()), "")
    return bool(first)


def scan(snapshot: MechanicalSnapshot) -> tuple[MechanicalFinding, ...]:
    """Return row-width diagnostics located on added physical lines."""
    assert snapshot.post_image_text is not None
    delimiter = _delimiter(snapshot.file)
    assert delimiter is not None
    reader = csv.reader(
        io.StringIO(snapshot.post_image_text), delimiter=delimiter, strict=True
    )
    # `reader.line_num` counts PHYSICAL lines consumed, which is what an added
    # line number means. A row ordinal is not the same thing: a quoted field
    # may contain newlines, after which every later row's ordinal is short of
    # its real position and a finding is attributed to the wrong line -- or,
    # worse, to a line that happens to be in the added set.
    try:
        rows: list[tuple[int, list[str]]] = []
        for row in reader:
            rows.append((reader.line_num, row))
    except csv.Error:
        return ()
    populated = [(line_no, row) for line_no, row in rows if row]
    if not populated:
        return ()
    header_width = len(populated[0][1])
    added = {line for line, _ in snapshot.added_lines or ()}
    findings: list[MechanicalFinding] = []
    for line_no, row in populated:
        if len(row) != header_width and line_no in added:
            findings.append(
                {
                    "check": "column_counts",
                    "line": line_no,
                    "detail": (
                        f"row has {len(row)} columns; header has {header_width}"
                    ),
                }
            )
    return tuple(findings)
