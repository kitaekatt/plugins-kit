"""Complexity lane: crapper's cyclomatic complexity per function, without coverage.

crapper's EDN names each function by namespace and name and carries no file
path or line, so these findings have ``path=None``.
"""

from __future__ import annotations

from pathlib import Path

from ..edn import read_crap, read_file
from ..model import Finding, LaneContext

name = "complexity"
tool = "crapper"
metrics_file = Path(".metrics") / "crap.edn"


def executable(ctx: LaneContext) -> Path:
    from . import default_executable

    return default_executable(ctx, tool)


def argv(ctx: LaneContext) -> list[str]:
    out = ["--root", "."]
    for root in ctx.source_roots:
        out += ["--source-root", root]
    out.append("--no-coverage")
    return out


def parse(ctx: LaneContext) -> list[Finding]:
    entries = read_file(ctx.analysis_root / metrics_file, read_crap)
    ordered = sorted(entries, key=lambda e: (-e["complexity"], e["namespace"], e["name"]))
    return [
        Finding(lane=name, kind="complexity", path=None, namespace=e["namespace"], symbol=e["name"],
                start_line=None, end_line=None,
                metrics={"complexity": e["complexity"], "coverage": e["coverage"], "crap": e["crap"]},
                rank=rank)
        for rank, e in enumerate(ordered, start=1)
    ]
