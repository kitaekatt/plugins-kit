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


UNSCORED_SHOWN = 20
COVERAGE_ROWS_SHOWN = 30


def _complexity_section(result: LaneResult, top: int, min_complexity: int) -> list[str]:
    eligible = [f for f in result.findings if f.metrics["complexity"] >= min_complexity]
    shown = eligible[:top]
    floor = f" with complexity >= {min_complexity}" if min_complexity > 1 else ""
    lines = [f"Functions scored: {len(result.findings)}. Showing the top {len(shown)} of "
             f"{len(eligible)} functions{floor}, by cyclomatic complexity. Coverage was not "
             "measured, so CRAP is not computed. Location is the file and the function's "
             "start line.", ""]
    dist = result.detail["complexity_distribution"]
    lines += ["Complexity distribution of every scored function:", "",
              "| Complexity | Functions |", "|---|---:|"]
    lines += [f"| {label} | {count} |" for label, count in dist.items()]
    lines.append("")
    if shown:
        lines += ["| Rank | Complexity | Namespace | Function | Location |", "|---:|---:|---|---|---|"]
        for f in shown:
            lines.append(f"| {f.rank} | {f.metrics['complexity']} | `{_cell(f.namespace)}` | "
                         f"`{_cell(f.symbol)}` | `{_cell(f.path)}:{f.start_line}` |")
        lines.append("")
    unscored = result.detail["files_without_scored_functions"]
    lines.append(f"Files with no scored functions: {len(unscored)} of "
                 f"{result.detail['files_selected']} files in the selection.")
    if unscored:
        lines += [""] + [f"- `{_cell(path)}`" for path in unscored[:UNSCORED_SHOWN]]
        if len(unscored) > UNSCORED_SHOWN:
            lines.append(f"- ... {len(unscored) - UNSCORED_SHOWN} more are in findings.json "
                         "(lanes[].detail.files_without_scored_functions)")
    lines.append("")
    return lines


def _ignored_note(result: LaneResult) -> list[str]:
    ignored = result.detail.get("ignored_extensions") or {}
    if not ignored:
        return []
    listing = ", ".join(f"{ext} ({n} file{'s' if n != 1 else ''})" for ext, n in ignored.items())
    return [f"{result.tool} ignores these extensions in the selection: {listing}.", ""]


def _coverage_section(coverage: dict[str, Any]) -> list[str]:
    lanes = coverage["lanes"]
    rows = list(coverage["by_extension"].items())
    lines = ["## Coverage", "",
             "Files tracked in the target (all of git's file list, not only the source roots) "
             "against files handed to each lane (under a source root, after exclude and language "
             "pruning, with an extension the lane's tool reads). 'Lane' is `no` when no lane "
             "reads the extension at all.", "",
             "| Extension | Tracked | " + " | ".join(lanes) + " | Lane |",
             "|---|---:|" + "---:|" * len(lanes) + "---|"]
    for ext, row in rows[:COVERAGE_ROWS_SHOWN]:
        cells = " | ".join(str(row["measured"][lane]) for lane in lanes)
        lines.append(f"| {_cell(ext)} | {row['tracked']} | {cells} | "
                     f"{'yes' if row['supported'] else 'no'} |")
    if len(rows) > COVERAGE_ROWS_SHOWN:
        lines.append("")
        lines.append(f"{len(rows) - COVERAGE_ROWS_SHOWN} more extensions are in findings.json "
                     "(target.coverage).")
    lines.append("")
    return lines


def markdown(target: dict[str, Any], results: list[LaneResult], complexity: dict[str, Any]) -> str:
    names = target["dirty_path_names"]
    dirty = f"{target['dirty_paths']}"
    if names:
        dirty += " (" + ", ".join(f"`{_cell(n)}`" for n in names) + ")"
    lines = [
        "# Code audit report", "",
        f"- Repository: `{target['repo']}`",
        f"- HEAD: `{target['head']}`",
        f"- Snapshot: {target['snapshot']}",
        f"- Dirty paths in the working tree at run time: {dirty}",
        "",
    ]
    lines += _coverage_section(target["coverage"])
    for result in results:
        lines += [f"## {result.name} ({result.tool} {result.tool_commit[:8]})", "",
                  f"Status: {result.status}. Tool exit {result.exit} in {result.seconds:.1f} s.", ""]
        if result.status == "no-source-files":
            lines += ["The tool found no source files to analyze in the pruned snapshot.", ""]
            continue
        if result.status == "no-comparable-source":
            lines += [f"The selection holds {result.detail['comparable_files']} file(s) {result.tool} "
                      "can read, and a duplicate needs two to compare, so the tool was not run. "
                      "Zero findings here is not evidence of no duplication.", ""]
            lines += _ignored_note(result)
            continue
        if result.name == "duplication":
            lines += _ignored_note(result)
            lines += _duplication_section(result)
        elif result.name == "complexity":
            lines += _complexity_section(result, complexity["top"], complexity["min_complexity"])
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
