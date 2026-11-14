"""How much of the target the audit measured, and what each lane's selection held.

``select`` lists, per lane, the files of the pruned snapshot that sit under a
source root and carry an extension the lane's tool reads. ``table`` compares
that with git's file list of the whole target, by extension. Neither claims to
know what a tool skipped on its own (its test-file and directory rules); the
counts are the files the runner handed over.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .languages import lane_extensions, supported_extensions
from .model import LaneResult

NO_EXTENSION = "(none)"
DISTRIBUTION_BUCKETS = (("1", 1, 1), ("2-5", 2, 5), ("6-10", 6, 10), ("11-20", 11, 20),
                        ("21-50", 21, 50), (">50", 51, None))


def extension(path: str) -> str:
    return PurePosixPath(path).suffix or NO_EXTENSION


def select(root: Path, source_roots: list[str], lanes: list[str]) -> dict[str, list[str]]:
    """Per lane, the sorted posix paths under ``source_roots`` in ``root`` the lane's tool reads."""
    exts = {lane: lane_extensions(lane) for lane in lanes}
    found: dict[str, list[str]] = {lane: [] for lane in lanes}
    for top, _dirs, names in os.walk(root, followlinks=False):
        for name in names:
            rel = (Path(top) / name).relative_to(root).as_posix()
            if not any(rel == r or rel.startswith(r.rstrip("/") + "/") for r in source_roots):
                continue
            for lane in lanes:
                if PurePosixPath(rel).suffix in exts[lane]:
                    found[lane].append(rel)
    return {lane: sorted(paths) for lane, paths in found.items()}


def table(tracked: list[str], selection: dict[str, list[str]]) -> dict[str, Any]:
    """``{"lanes": [...], "by_extension": {ext: {tracked, supported, measured: {lane: n}}}}``."""
    supported = supported_extensions()
    counts = Counter(extension(p) for p in tracked)
    measured = {lane: Counter(extension(p) for p in paths) for lane, paths in selection.items()}
    by_ext = {
        ext: {"tracked": counts[ext], "supported": ext in supported,
              "measured": {lane: measured[lane][ext] for lane in selection}}
        for ext in sorted(counts, key=lambda e: (-counts[e], e))
    }
    return {"lanes": list(selection), "by_extension": by_ext}


def ignored_by(lane: str, snapshot_files: list[str]) -> dict[str, int]:
    """Extensions some lane reads but ``lane``'s tool ignores, with their file counts."""
    own = lane_extensions(lane)
    other = supported_extensions() - own
    counts = Counter(extension(p) for p in snapshot_files if extension(p) in other)
    return dict(sorted(counts.items()))


def distribution(complexities: list[int]) -> dict[str, int]:
    out = {label: 0 for label, _lo, _hi in DISTRIBUTION_BUCKETS}
    for value in complexities:
        for label, low, high in DISTRIBUTION_BUCKETS:
            if value >= low and (high is None or value <= high):
                out[label] += 1
                break
        else:
            raise ValueError(f"complexity {value!r} is below 1")
    return out


def annotate_complexity(result: LaneResult, selected: list[str]) -> None:
    scored = {f.path for f in result.findings}
    result.detail["files_selected"] = len(selected)
    result.detail["files_without_scored_functions"] = [p for p in selected if p not in scored]
    result.detail["complexity_distribution"] = distribution(
        [f.metrics["complexity"] for f in result.findings])
