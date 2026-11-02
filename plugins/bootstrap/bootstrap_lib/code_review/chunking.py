"""Diff-section chunking. VCS-neutral: operates on a generic section list.

The Read tool in Claude Code refuses to ingest files past some unpublished
byte threshold (a 1.4 MB plain-text diff fails). When a code-review skill
hands a reviewer subagent a single giant diff bundle, the reviewer can't
Read it. The fix is to partition the diff at file boundaries into chunks
that comfortably fit, then fan out R reviewers per chunk so the work
parallelises instead of bottlenecking on one ingest.

Both p4-kit and git-kit produce per-file diff sections via their own
VCS adapter, then hand the list here. Sections are atomic (never split
mid-hunk); within that constraint we balance chunk sizes (K = ceil(total
/ max), target = total / K) and prefer to close chunks at "natural"
boundaries -- parent directory transitions by default, or whatever the
caller's group_by callable says.
"""

import math
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional, TypedDict


class DiffSection(TypedDict):
    """One file's diff: an opaque identifier (depot path, git path) and
    the full text of the section (header + hunks)."""
    identifier: str
    text: str


def _section_parent(identifier: str) -> str:
    """Default group key: everything before the last '/'.

    Works for both `//depot/foo/bar.cpp` (p4) and `src/foo/bar.py` (git).
    `idx > 0` (not `> 1`): a single-character top-level dir (`a/file.py`,
    idx == 1) must still group as "a"; idx <= 0 means no parent (no slash,
    or only a leading slash), so the identifier is its own group.
    """
    idx = identifier.rfind("/")
    return identifier[:idx] if idx > 0 else identifier


def partition_sections_into_chunks(
    sections: list[DiffSection],
    max_bytes: int,
    preamble: str = "",
    group_by: Optional[Callable[[str], str]] = None,
) -> list[dict]:
    """Partition diff sections into balanced chunks at natural boundaries.

    Each chunk contains one or more whole sections concatenated; sections
    are never split mid-hunk. `group_by(identifier)` returns the "natural
    boundary" key (default: parent dir by rfind('/')).

    Sizing: K = ceil(total_bytes / max_bytes), target = total_bytes / K.
    The walk closes the current chunk when bytes >= target AND the next
    section's group key differs from the current chunk's last group, OR
    when adding the next section would exceed max_bytes (hard cap). A
    single section larger than max_bytes lands alone in an oversized
    chunk -- we don't split inside a file.

    Returns [{"text": str, "files": [identifier...], "bytes": int}, ...].
    The `files` field carries identifiers in the order they appear in the
    chunk; callers stitch a chunk-index back onto their per-file records.
    """
    if not sections:
        return []
    if group_by is None:
        group_by = _section_parent

    section_bytes = [len(s["text"].encode("utf-8")) for s in sections]
    preamble_bytes = len(preamble.encode("utf-8"))
    total = sum(section_bytes) + preamble_bytes
    k = max(1, math.ceil(total / max_bytes))
    target = total / k if k > 0 else total

    chunks: list[dict] = []
    cur_text = preamble or ""
    cur_files: list[str] = []
    cur_bytes = preamble_bytes
    cur_last_group: Optional[str] = None

    for sec, sb in zip(sections, section_bytes):
        sec_group = group_by(sec["identifier"])
        would_be = cur_bytes + sb
        if cur_text and would_be > max_bytes and not cur_files:
            if cur_bytes > max_bytes:
                print(
                    "error: diff preamble exceeds max chunk size; emitting oversized preamble",
                    file=sys.stderr,
                )
            chunks.append(
                {"text": cur_text, "files": [], "bytes": cur_bytes}
            )
            cur_text = ""
            cur_bytes = 0
        if cur_files:
            hard_cap = would_be > max_bytes
            balanced = cur_bytes >= target and sec_group != cur_last_group
            if hard_cap or balanced:
                chunks.append(
                    {"text": cur_text, "files": cur_files, "bytes": cur_bytes}
                )
                cur_text = ""
                cur_files = []
                cur_bytes = 0
                cur_last_group = None

        cur_text += sec["text"]
        cur_files.append(sec["identifier"])
        cur_bytes += sb
        cur_last_group = sec_group

    if cur_files or cur_text:
        chunks.append({"text": cur_text, "files": cur_files, "bytes": cur_bytes})
    return chunks


# The Read tool refuses a page larger than about 25,000 tokens (roughly 100,000
# bytes) and a reader that does not know a chunk's line widths guesses a page
# size and fails. 800 lines is the ceiling for short lines.
#
# `page_lines` is one uniform page size, so a few very wide lines set it for the
# whole chunk: nine 20 KB run records in a 728-line chunk gave page_lines 2 and a
# 364-call read, and reviewers gave up partway. `pages` is the plan a reviewer
# follows instead: [offset, limit] pairs that tile the chunk greedily, so a page
# is as long as the bytes allow and a wide line only costs its own page.
READ_PAGE_MAX_LINES = 800
READ_PAGE_MAX_BYTES = 100_000


def _max_window_lines(widths: list[int], cap_bytes: int, cap_lines: int) -> int:
    """Largest N <= cap_lines such that every N consecutive widths sum to at most cap_bytes; at least 1."""

    def fits(n: int) -> bool:
        total = sum(widths[:n])
        if total > cap_bytes:
            return False
        for i in range(n, len(widths)):
            total += widths[i] - widths[i - n]
            if total > cap_bytes:
                return False
        return True

    # A window longer than the chunk is the whole chunk, so only N up to len(widths) needs testing.
    hi = min(cap_lines, len(widths))
    if hi == 0 or fits(hi):
        return cap_lines
    lo = 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _split_lines(data: bytes) -> list[bytes]:
    """Lines of `data` without their newline; a trailing terminator adds no empty line."""
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return parts


def _line_widths(data: bytes) -> list[int]:
    """Per-line byte widths, one terminator counted per line, as Read pages them."""
    return [len(p) + 1 for p in _split_lines(data)]


def plan_read_pages(widths: list[int], cap_bytes: int, cap_lines: int) -> list[list[int]]:
    """Tile lines 1..len(widths) into [offset, limit] Read pages, greedily.

    `widths` are line widths including terminators. A page starts at the next
    unread line and grows while its bytes stay <= cap_bytes and its line count
    <= cap_lines; a single line wider than cap_bytes gets a page of its own
    (limit 1). `offset` is 1-based, as the Read tool takes it. Empty -> [].
    """
    pages: list[list[int]] = []
    i = 0
    while i < len(widths):
        total = widths[i]
        n = 1
        while i + n < len(widths) and n < cap_lines and total + widths[i + n] <= cap_bytes:
            total += widths[i + n]
            n += 1
        pages.append([i + 1, n])
        i += n
    return pages


def chunk_read_geometry(data: bytes) -> tuple[int, int, int]:
    """Return (lines, longest_line_bytes, page_lines) for one written chunk.

    `lines` counts a final line that has no terminator. `longest_line_bytes`
    excludes the line terminator. `page_lines` is the largest `limit` for a
    Read call such that no `page_lines` consecutive lines, terminators
    included, exceed READ_PAGE_MAX_BYTES, capped at READ_PAGE_MAX_LINES and
    never below 1. Bounding the window rather than the widest line keeps one
    outlier line from shrinking the page of its whole chunk. A uniform size is
    still set by the widest run of lines; `plan_read_pages` gives the tighter
    per-page plan that write_chunks records as `pages`.
    """
    parts = _split_lines(data)
    longest = max((len(p.rstrip(b"\r")) for p in parts), default=0)
    page = _max_window_lines([len(p) + 1 for p in parts], READ_PAGE_MAX_BYTES, READ_PAGE_MAX_LINES)
    return len(parts), longest, page


def write_chunks(chunks: list[dict], bundle_dir: Path) -> list[dict]:
    """Write chunks to <bundle_dir>/chunks/ and return the JSON index.

    Stale chunks from a prior run on the same CL/ref are removed first so
    a re-run can't leave orphan chunk files behind when the new run
    produces fewer chunks.

    Each index entry: {"index": int, "path": "chunks/chunk-NNN.diff",
    "files": [identifier...], "bytes": int, "lines": int,
    "longest_line_bytes": int, "page_lines": int, "pages": [[offset, limit]...]}.
    `path` is relative to `bundle_dir`. The last four are measured on the
    written file. `pages` tiles lines 1..`lines` in order with no gap or
    overlap, each page within the Read byte and line caps, so a reader calls
    Read once per pair; `page_lines` is the older uniform page size, kept for
    older consumers.
    """
    chunks_dir = bundle_dir / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
    chunks_dir.mkdir(parents=True)

    index: list[dict] = []
    for i, c in enumerate(chunks):
        rel = f"chunks/chunk-{i:03d}.diff"
        (bundle_dir / rel).write_text(c["text"], encoding="utf-8")
        data = (bundle_dir / rel).read_bytes()
        lines, longest, page = chunk_read_geometry(data)
        index.append(
            {
                "index": i,
                "path": rel,
                "files": list(c["files"]),
                "bytes": c["bytes"],
                "lines": lines,
                "longest_line_bytes": longest,
                "page_lines": page,
                "pages": plan_read_pages(_line_widths(data), READ_PAGE_MAX_BYTES, READ_PAGE_MAX_LINES),
            }
        )
    return index
