"""Find an item's actionable section in a task folder's plan.md.

An item section is an ATX heading at level 2 through 4 whose first token is
the exact item id (normally ``### <id> -- <title>``). Its body ends at the next
heading of equal or higher level and must contain non-whitespace text. Titles
are never matched fuzzily. Headings inside fenced code blocks are not headings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})[ \t]+(\S+)(.*)$")
_FENCE = re.compile(r"^[ \t]{0,3}(```+|~~~+)")

FOUND = "found"
MISSING = "missing"
DUPLICATE = "duplicate"
EMPTY = "empty"


@dataclass(frozen=True)
class PlanSection:
    """Outcome of looking for one item's section."""

    status: str  # found | missing | duplicate | empty
    heading: str | None = None
    body: str = ""


def _headings(lines: list[str]) -> list[tuple[int, int, str, str]]:
    """(line index, level, first token, heading text) outside code fences."""
    found = []
    fence: str | None = None
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading:
            found.append((index, len(heading.group(1)), heading.group(2), line.strip()))
    return found


def _headings_and_open_fence(lines: list[str]) -> tuple[list[tuple[int, int, str, str]], bool]:
    fence: str | None = None
    found = []
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if match:
            marker = match.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        heading = _HEADING.match(line)
        if heading:
            found.append((index, len(heading.group(1)), heading.group(2), line.strip()))
    return found, fence is not None


def has_unclosed_fence(text: str) -> bool:
    """Whether ``text`` ends inside a fenced code block (so everything after it would be code)."""
    return _headings_and_open_fence(text.splitlines())[1]


def headings_outside_fences(text: str, max_level: int) -> list[tuple[int, str]]:
    """``(level, heading line)`` of every ATX heading of level ``max_level`` or shallower outside code fences."""
    return [(level, line) for _i, level, _t, line in _headings(text.splitlines()) if level <= max_level]


def section_insert_offset(plan_text: str, after_offset: int) -> int:
    """Where a new item section belongs: the start of the first level 1-2 heading (outside code
    fences) that begins at or after ``after_offset``, else the end of the text.

    ``after_offset`` is the end of the task_items fence, so the section lands at the end of the
    ``##`` region that holds the fence.
    """
    lines = plan_text.splitlines(keepends=True)
    starts = []
    position = 0
    for line in lines:
        starts.append(position)
        position += len(line)
    for index, level, _token, _text in _headings([line.rstrip("\r\n") for line in lines]):
        if level <= 2 and starts[index] >= after_offset:
            return starts[index]
    return len(plan_text)


def find_item_section(plan_text: str, item_id: str) -> PlanSection:
    lines = plan_text.splitlines()
    headings = _headings(lines)
    matches = [h for h in headings if 2 <= h[1] <= 4 and h[2] == item_id]
    if not matches:
        return PlanSection(MISSING)
    if len(matches) > 1:
        return PlanSection(DUPLICATE, heading=matches[0][3])
    index, level, _token, text = matches[0]
    end = len(lines)
    for other_index, other_level, _t, _x in headings:
        if other_index > index and other_level <= level:
            end = other_index
            break
    body = "\n".join(lines[index + 1 : end]).strip()
    if not body:
        return PlanSection(EMPTY, heading=text)
    return PlanSection(FOUND, heading=text, body=body)
