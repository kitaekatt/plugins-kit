"""Write ``findings.json`` and ``report.md`` under the target's ephemeral project data.

The report directory is ``<repo>/.local-data/plugins-kit/clean-code-kit/reports/
<UTC yyyymmddThhmmssZ>-<head8>/``. It must be git-ignored in the target before
anything is written; this is the only place the runner writes inside the target.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import MARKETPLACE, PLUGIN
from .errors import Refusal
from .model import LaneResult

SCHEMA = "clean-code-kit.findings/v1"
REPORTS_REL = PurePosixPath(".local-data") / MARKETPLACE / PLUGIN / "reports"


def report_dir(repo: Path, head: str, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return repo / Path(REPORTS_REL) / f"{stamp}-{head[:8]}"


def collision(directory: Path) -> Refusal:
    return Refusal(
        f"report directory {directory} already exists: a run in the same UTC second on the same "
        "HEAD wrote it; retry after a second")


def ensure_new(directory: Path) -> None:
    """Refuse when ``directory`` exists; a report is never overwritten or merged."""
    if directory.exists():
        raise collision(directory)


def ensure_ignored(repo: Path, directory: Path) -> None:
    """Refuse unless git ignores ``directory`` in ``repo``."""
    rel = directory.relative_to(repo).as_posix() + "/"
    proc = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", rel],
                          capture_output=True, text=True)
    if proc.returncode == 1:
        raise Refusal(
            f"{repo / Path(REPORTS_REL)} is not git-ignored in {repo}; add `.local-data/` to "
            f"{repo / '.git' / 'info' / 'exclude'} or to {repo / '.gitignore'}, then retry")
    if proc.returncode != 0:
        raise Refusal(f"git check-ignore failed in {repo}: {proc.stderr.strip()}")


def findings_document(target: dict[str, Any], results: list[LaneResult]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "target": target,
        "lanes": [r.record() for r in results],
        "findings": [f.to_dict() for r in results for f in r.findings],
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _location(span: dict[str, Any]) -> str:
    return f"`{_cell(span['path'])}:{span['start_line']}-{span['end_line']}`"


def _duplication_section(result: LaneResult) -> list[str]:
    groups = sum(1 for f in result.findings if f.metrics["copies"] > 2)
    lines = [f"Candidates: {len(result.findings)} ({groups} with three or more copies, "
             f"{len(result.findings) - groups} pairs). Ranked by copies, then score.", ""]
    if not result.findings:
        return lines
    lines += ["| Rank | Copies | Score | Language | Locations |", "|---:|---:|---:|---|---|"]
    for f in result.findings:
        where = "<br>".join(_location(span) for span in f.group or [])
        lines.append(f"| {f.rank} | {f.metrics['copies']} | {f.metrics['score']:.2f} | "
                     f"{_cell(f.metrics['language'])} | {where} |")
    lines.append("")
    return lines


def _complexity_section(result: LaneResult, top: int) -> list[str]:
    shown = result.findings[:top]
    lines = [f"Functions scored: {len(result.findings)}. Showing the top {len(shown)} by "
             "cyclomatic complexity. Coverage was not measured, so CRAP is not computed. "
             "Location is the file and the function's start line.", ""]
    if not shown:
        return lines
    lines += ["| Rank | Complexity | Namespace | Function | Location |", "|---:|---:|---|---|---|"]
    for f in shown:
        lines.append(f"| {f.rank} | {f.metrics['complexity']} | `{_cell(f.namespace)}` | "
                     f"`{_cell(f.symbol)}` | `{_cell(f.path)}:{f.start_line}` |")
    lines.append("")
    return lines


def markdown(target: dict[str, Any], results: list[LaneResult], top: int) -> str:
    lines = [
        "# Code audit report", "",
        f"- Repository: `{target['repo']}`",
        f"- HEAD: `{target['head']}`",
        f"- Snapshot: {target['snapshot']}",
        f"- Dirty paths in the working tree at run time: {target['dirty_paths']}",
        "",
    ]
    for result in results:
        lines += [f"## {result.name} ({result.tool} {result.tool_commit[:8]})", "",
                  f"Status: {result.status}. Tool exit {result.exit} in {result.seconds:.1f} s.", ""]
        if result.status == "no-source-files":
            lines += ["The tool found no source files to analyze in the pruned snapshot.", ""]
            continue
        if result.name == "duplication":
            lines += _duplication_section(result)
        elif result.name == "complexity":
            lines += _complexity_section(result, top)
        else:
            raise ValueError(f"no report section for lane {result.name!r}")
    return "\n".join(lines).rstrip("\n") + "\n"


def write(directory: Path, document: dict[str, Any], text: str) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir()
    except FileExistsError as exc:
        raise collision(directory) from exc
    (directory / "findings.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    (directory / "report.md").write_text(text, encoding="utf-8")
