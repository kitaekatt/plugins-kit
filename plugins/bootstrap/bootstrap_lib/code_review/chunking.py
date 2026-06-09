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
    """
    idx = identifier.rfind("/")
    return identifier[:idx] if idx > 1 else identifier


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
        if not preamble:
            return []
        # Diff text that parsed into zero sections must not be silently
        # dropped -- a caller's splitter may have failed to recognize the
        # format (e.g. combined-diff headers). Surface the unattributed
        # text as one preamble-only chunk so reviewers still see it.
        print(
            "warning: diff parsed into 0 sections; emitting preamble-only chunk",
            file=sys.stderr,
        )
        return [
            {
                "text": preamble,
                "files": [],
                "bytes": len(preamble.encode("utf-8")),
            }
        ]
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
        if cur_files:
            would_be = cur_bytes + sb
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


def write_chunks(chunks: list[dict], bundle_dir: Path) -> list[dict]:
    """Write chunks to <bundle_dir>/chunks/ and return the JSON index.

    Stale chunks from a prior run on the same CL/ref are removed first so
    a re-run can't leave orphan chunk files behind when the new run
    produces fewer chunks.

    Each index entry: {"index": int, "path": "chunks/chunk-NNN.diff",
    "files": [identifier...], "bytes": int}. `path` is relative to
    `bundle_dir`.
    """
    chunks_dir = bundle_dir / "chunks"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir)
    chunks_dir.mkdir(parents=True)

    index: list[dict] = []
    for i, c in enumerate(chunks):
        rel = f"chunks/chunk-{i:03d}.diff"
        (bundle_dir / rel).write_text(c["text"], encoding="utf-8")
        index.append(
            {"index": i, "path": rel, "files": list(c["files"]), "bytes": c["bytes"]}
        )
    return index
