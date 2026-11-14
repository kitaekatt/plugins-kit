"""Duplication lane: dryer's structural duplicate candidates, grouped sets first."""

from __future__ import annotations

from pathlib import Path

from ..edn import read_dry, read_file
from ..model import Finding, LaneContext

name = "duplication"
tool = "dryer"
metrics_file = Path(".metrics") / "dry.edn"


def executable(ctx: LaneContext) -> Path:
    from . import default_executable

    return default_executable(ctx, tool)


def argv(ctx: LaneContext) -> list[str]:
    settings = ctx.settings
    out = ["--root", "."]
    for root in ctx.source_roots:
        out += ["--source-root", root]
    out += ["--threshold", str(settings["threshold"]), "--min-lines", str(settings["min_lines"])]
    if settings["group_identical"]:
        out.append("--group-identical")
    return out


def parse(ctx: LaneContext) -> list[Finding]:
    candidates = read_file(ctx.analysis_root / metrics_file, read_dry)
    ordered = sorted(
        candidates,
        key=lambda c: (-len(c["spans"]), -c["score"], c["spans"][0]["path"], c["spans"][0]["start_line"]))
    findings = []
    for rank, cand in enumerate(ordered, start=1):
        first = cand["spans"][0]
        findings.append(Finding(
            lane=name, kind="duplicate", path=first["path"], namespace=None, symbol=None,
            start_line=first["start_line"], end_line=first["end_line"],
            metrics={"score": cand["score"], "copies": len(cand["spans"]),
                     "language": cand["language"], "left_nodes": cand["left_nodes"],
                     "right_nodes": cand["right_nodes"]},
            rank=rank, group=[dict(span) for span in cand["spans"]]))
    return findings
