"""Shared unified-diff parsing for mechanical review checks."""

from __future__ import annotations

import re
from typing import Any

_HUNK_HEADER_RE = re.compile(r"^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _HunkParseError(Exception):
    """Raised when diff hunks cannot be parsed or applied."""


class _DiffMismatchError(Exception):
    """Raised when a context or deletion line disagrees with the pre-image."""


def _parse_hunks(diff_section_text: str) -> list[dict[str, Any]]:
    """Extract unified-diff hunks from one file's diff section."""
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in diff_section_text.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match:
            current = {
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2)) if match.group(2) is not None else 1,
                "new_start": int(match.group(3)),
                "ops": [],
            }
            hunks.append(current)
            continue
        if current is None:
            continue
        if line == "":
            current["ops"].append((" ", ""))
            continue
        op = line[0]
        if op == "\\":
            continue
        if op in (" ", "+", "-"):
            current["ops"].append((op, line[1:]))
        else:
            raise _HunkParseError(f"unexpected hunk body line: {line!r}")
    return hunks


def _reconstruct(
    pre_lines: list[str], hunks: list[dict[str, Any]]
) -> tuple[list[str], set[int], set[int]]:
    """Apply hunks and return the post-image and added/removed line indices."""
    post: list[str] = []
    pre_idx = 0
    added_post: set[int] = set()
    removed_pre: set[int] = set()
    for hunk in hunks:
        old_start = hunk["old_start"]
        old_count = hunk["old_count"]
        context_end = old_start if old_count == 0 else old_start - 1
        if context_end < pre_idx or context_end > len(pre_lines):
            raise _HunkParseError("hunk start out of range for pre-image")
        while pre_idx < context_end:
            post.append(pre_lines[pre_idx])
            pre_idx += 1
        for op, content in hunk["ops"]:
            if op == " ":
                if pre_idx >= len(pre_lines):
                    raise _HunkParseError("context past end of pre-image")
                if pre_lines[pre_idx] != content:
                    raise _DiffMismatchError("context does not match pre-image")
                post.append(pre_lines[pre_idx])
                pre_idx += 1
            elif op == "-":
                if pre_idx >= len(pre_lines):
                    raise _HunkParseError("remove past end of pre-image")
                if pre_lines[pre_idx] != content:
                    raise _DiffMismatchError("deletion does not match pre-image")
                removed_pre.add(pre_idx)
                pre_idx += 1
            else:
                added_post.add(len(post))
                post.append(content)
    while pre_idx < len(pre_lines):
        post.append(pre_lines[pre_idx])
        pre_idx += 1
    return post, added_post, removed_pre


def _added_lines_with_numbers(
    hunks: list[dict[str, Any]],
) -> list[tuple[int, str]]:
    """Return post-image line numbers and text for every added line."""
    out: list[tuple[int, str]] = []
    for hunk in hunks:
        lineno = hunk.get("new_start", 1)
        for op, content in hunk["ops"]:
            if op == "+":
                out.append((lineno, content))
                lineno += 1
            elif op == " ":
                lineno += 1
    return out


def _delta_lines(hunks: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Return the added and removed text from parsed hunks."""
    added = [content for hunk in hunks for op, content in hunk["ops"] if op == "+"]
    removed = [content for hunk in hunks for op, content in hunk["ops"] if op == "-"]
    return added, removed
