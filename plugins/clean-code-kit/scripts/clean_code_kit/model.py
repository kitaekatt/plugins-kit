"""Data passed between the runner and the lanes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LaneContext:
    """What a lane may read.

    ``analysis_root`` is the pruned snapshot the metric tools scan.
    ``exec_env`` is the target's own test environment; it stays None until a
    lane that executes the target's tests exists, and must never be inferred
    from ``analysis_root``.
    """

    repo: Path
    analysis_root: Path
    exec_env: Any
    settings: dict[str, Any]
    source_roots: list[str]
    test_roots: list[str]
    languages: list[str]
    venv_prefix: Path
    parser_cache: Path


@dataclass
class Finding:
    lane: str
    kind: str
    path: str | None
    namespace: str | None
    symbol: str | None
    start_line: int | None
    end_line: int | None
    metrics: dict[str, Any]
    rank: int
    group: list[dict[str, Any]] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LaneResult:
    name: str
    tool: str
    tool_commit: str
    argv: list[str]
    exit: int
    seconds: float
    status: str
    findings: list[Finding]

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool": self.tool,
            "tool_commit": self.tool_commit,
            "argv": self.argv,
            "exit": self.exit,
            "seconds": self.seconds,
            "status": self.status,
        }
