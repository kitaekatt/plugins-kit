#!/usr/bin/env python3
"""Run and score the reviewer_b model bakeoff."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[2]
BAKEOFF = Path(__file__).resolve().parent
CORPUS = Path(os.environ.get("REVIEW_BAKEOFF_CORPUS", BAKEOFF / "corpus"))
RESULTS = Path(os.environ.get("REVIEW_BAKEOFF_RESULTS", BAKEOFF / "results"))
LANE = "reviewer_b_diff_only_bugs"
RUNNER = ROOT / "plugins" / "git-kit" / "scripts" / "run_review_lane.py"
LINE_RANGE = re.compile(r"(\d+)(?:-(\d+))?")


@dataclass(frozen=True)
class Planted:
    file: str
    lines: str


@dataclass(frozen=True)
class Case:
    case_id: str
    kind: str
    files: tuple[str, ...]
    planted: tuple[Planted, ...]


class BakeoffError(Exception):
    """A user-correctable bakeoff input or result error."""


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _yaml_value(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_unquote(part.strip()) for part in inner.split(",")]
    return _unquote(value)


def _read_case(path: Path) -> Case:
    """Read the documented case.yaml subset without a third-party parser."""
    values: dict[str, Any] = {}
    planted: list[Planted] = []
    current: dict[str, str] | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BakeoffError(f"cannot read {path}: {exc}") from exc
    for raw in lines:
        text = raw.split("#", 1)[0].rstrip()
        if not text.strip() or text.lstrip().startswith("|"):
            continue
        stripped = text.strip()
        if stripped.startswith("-") and ":" in stripped:
            if current is not None and "file" in current and "lines" in current:
                planted.append(Planted(current["file"], current["lines"]))
            current = {}
            stripped = stripped[1:].strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if current is not None and key in {"file", "lines"}:
            current[key] = str(_yaml_value(value))
        elif key in {"id", "kind", "files"}:
            values[key] = _yaml_value(value)
    if current is not None and "file" in current and "lines" in current:
        planted.append(Planted(current["file"], current["lines"]))
    case_id = str(values.get("id", path.parent.name))
    kind = str(values.get("kind", ""))
    files = tuple(str(item) for item in values.get("files", []))
    if case_id != path.parent.name:
        raise BakeoffError(f"{path}: id {case_id!r} does not match directory name")
    if kind not in {"positive", "decoy"}:
        raise BakeoffError(f"{path}: kind must be positive or decoy")
    if kind == "positive" and len(planted) != 1:
        raise BakeoffError(f"{path}: positive case must have exactly one planted bug")
    if kind == "decoy" and planted:
        raise BakeoffError(f"{path}: decoy case must have no planted bugs")
    return Case(case_id, kind, files, tuple(planted))


def load_cases() -> list[Case]:
    if not CORPUS.is_dir():
        return []
    cases: list[Case] = []
    for directory in sorted(item for item in CORPUS.iterdir() if item.is_dir()):
        case_file = directory / "case.yaml"
        chunk = directory / "chunk.diff"
        if not case_file.is_file() or not chunk.is_file():
            raise BakeoffError(f"case {directory.name}: need case.yaml and chunk.diff")
        cases.append(_read_case(case_file))
    return cases


def _issues_from_result(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BakeoffError(f"{path}: missing or unparseable results file: {exc}") from exc
    if isinstance(value, dict):
        if value.get("status") == "failed":
            raise BakeoffError(f"{path}: lane was recorded as failed: {value.get('stderr', '')}")
        value = value.get("issues")
    if not isinstance(value, list):
        raise BakeoffError(f"{path}: result must be an issue array or envelope with issues")
    for index, issue in enumerate(value):
        if not isinstance(issue, dict) or not isinstance(issue.get("file"), str) or not isinstance(issue.get("lines"), str):
            raise BakeoffError(f"{path}: issue[{index}] has no parseable file and lines")
    return value


def _range(value: str) -> tuple[int, int]:
    match = LINE_RANGE.fullmatch(value)
    if match is None:
        raise ValueError(f"unparseable line range {value!r}")
    start = int(match.group(1))
    end = int(match.group(2) or match.group(1))
    if end < start:
        raise ValueError(f"reversed line range {value!r}")
    return start, end


def _matches(issue: dict[str, Any], planted: Planted) -> bool:
    try:
        issue_range = _range(issue["lines"])
        planted_range = _range(planted.lines)
    except (KeyError, TypeError, ValueError):
        return False
    return issue.get("file") == planted.file and max(issue_range[0], planted_range[0]) <= min(issue_range[1], planted_range[1])


def _score_case(case: Case, issues: list[dict[str, Any]]) -> dict[str, Any]:
    matches = sum(1 for issue in issues if case.planted and _matches(issue, case.planted[0]))
    return {"case": case.case_id, "kind": case.kind, "issues": len(issues), "matching_issues": matches, "miss": case.kind == "positive" and matches == 0, "false_positive": case.kind == "decoy" and bool(issues), "issues_detail": issues}


def _summary(arm: str, cases: Sequence[Case], details: Sequence[dict[str, Any]]) -> dict[str, Any]:
    positives = [item for item in details if item["kind"] == "positive"]
    decoys = [item for item in details if item["kind"] == "decoy"]
    positive_issues = sum(item["issues"] for item in positives)
    all_issues = sum(item["issues"] for item in details)
    matching = sum(item["matching_issues"] for item in details)
    return {"arm": arm, "case_count": len(cases), "positive_cases": len(positives), "decoy_cases": len(decoys), "total_issues": all_issues, "matching_issues": matching, "recall": (sum(item["matching_issues"] > 0 for item in positives) / len(positives)) if positives else None, "precision": (matching / all_issues) if all_issues else None, "decoy_fp_rate": (sum(item["false_positive"] for item in decoys) / len(decoys)) if decoys else None, "positive_noise": (sum(item["issues"] - item["matching_issues"] for item in positives) / positive_issues) if positive_issues else None, "cases": list(details)}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run_arm(arm: str, selected: Sequence[str]) -> int:
    cases = load_cases()
    wanted = set(selected)
    cases = [case for case in cases if not wanted or case.case_id in wanted]
    if wanted and len(wanted) != len(cases):
        missing = sorted(wanted - {case.case_id for case in cases})
        raise BakeoffError(f"unknown case id(s): {', '.join(missing)}")
    for case in cases:
        command = [sys.executable, str(RUNNER), "--lane", LANE, "--model", arm, "--chunk", str(CORPUS / case.case_id / "chunk.diff"), "--description", case.case_id, "--project-root", str(ROOT)]
        for file in case.files:
            command.extend(["--file", file])
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        output = completed.stdout.strip()
        if completed.returncode == 0:
            try:
                envelope = json.loads(output)
            except json.JSONDecodeError as exc:
                envelope = {"status": "failed", "returncode": completed.returncode, "stderr": f"stdout was not JSON: {exc}", "stdout": output}
        else:
            envelope = {"status": "failed", "returncode": completed.returncode, "stderr": completed.stderr, "stdout": output}
        _write_json(RESULTS / arm / f"{case.case_id}.json", envelope)
        print(f"{arm}/{case.case_id}: {'ok' if completed.returncode == 0 else 'failed'}")
    return 0


def write_prompts(alias: str) -> int:
    from importlib.util import module_from_spec, spec_from_file_location
    prompt_path = ROOT / "plugins" / "bootstrap" / "bootstrap_lib" / "code_review" / "lane_prompts.py"
    spec = spec_from_file_location("bakeoff_lane_prompts", prompt_path)
    if spec is None or spec.loader is None:
        raise BakeoffError(f"cannot load {prompt_path}")
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cases = load_cases()
    index: dict[str, Any] = {}
    for case in cases:
        diff = (CORPUS / case.case_id / "chunk.diff").read_text(encoding="utf-8")
        user = module.build_user_message(LANE, diff_text=diff, files=case.files, description=case.case_id)
        prompt = module.LANE_PROMPTS[LANE].system + "\n\n" + user
        path = RESULTS / alias / "prompts" / f"{case.case_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        index[case.case_id] = {"prompt_path": str(path), "files": list(case.files)}
    _write_json(RESULTS / alias / "prompts" / "index.json", index)
    return 0


def ingest(alias: str, case_id: str, json_path: Path) -> int:
    cases = {case.case_id for case in load_cases()}
    if case_id not in cases:
        raise BakeoffError(f"unknown case id: {case_id}")
    try:
        value = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BakeoffError(f"cannot read raw issue array {json_path}: {exc}") from exc
    if not isinstance(value, list):
        raise BakeoffError(f"{json_path}: raw result must be a JSON issue array")
    _write_json(RESULTS / alias / f"{case_id}.json", value)
    return 0


def score(arms: Sequence[str]) -> int:
    cases = load_cases()
    if not cases:
        raise BakeoffError("corpus is empty; no cases to score")
    summaries: list[dict[str, Any]] = []
    for arm in arms:
        details = []
        excluded = []
        for case in cases:
            # A lane that failed, and a result file that is missing or corrupt, are
            # NOT "reported no issues". Conflating them would silently flatter an
            # arm whose lanes never ran, so they are excluded from every denominator
            # and reported separately -- and one bad case never aborts the arm.
            try:
                issues = _issues_from_result(RESULTS / arm / f"{case.case_id}.json")
            except BakeoffError as exc:
                excluded.append({"case": case.case_id, "kind": case.kind, "reason": str(exc)})
                continue
            details.append(_score_case(case, issues))
        scored = [case for case in cases if case.case_id not in {item["case"] for item in excluded}]
        summary = _summary(arm, scored, details)
        summary["excluded_cases"] = excluded
        _write_json(RESULTS / arm / "summary.json", summary)
        summaries.append(summary)
    def _fmt(value):
        return "n/a" if value is None else f"{value:.3f}"

    print("arm\trecall\tprecision\tdecoy_fp_rate\tpositive_noise\tscored\texcluded")
    for item in summaries:
        print(
            f"{item['arm']}\t{_fmt(item['recall'])}\t{_fmt(item['precision'])}"
            f"\t{_fmt(item['decoy_fp_rate'])}\t{_fmt(item['positive_noise'])}"
            f"\t{item['case_count']}\t{len(item['excluded_cases'])}"
        )
    for item in summaries:
        for skipped in item["excluded_cases"]:
            print(f"{item['arm']} EXCLUDED {skipped['case']}: {skipped['reason']}")
    for item in summaries:
        for detail in item["cases"]:
            if detail["miss"] or detail["false_positive"]:
                label = "MISS" if detail["miss"] else "FALSE POSITIVE"
                print(f"{item['arm']} {label} {detail['case']}: {json.dumps(detail['issues_detail'])}")
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Run and score the reviewer_b bakeoff.")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run an endpoint arm")
    run.add_argument("--arm", required=True)
    run.add_argument("--case", action="append", default=[])
    prompts = sub.add_parser("prompts", help="write Agent prompt files")
    prompts.add_argument("--arm", required=True)
    take = sub.add_parser("ingest", help="store an Agent issue array")
    take.add_argument("--arm", required=True)
    take.add_argument("--case", required=True)
    take.add_argument("--json", required=True, type=Path)
    scoring = sub.add_parser("score", help="score one or more arms")
    scoring.add_argument("--arm", required=True, action="append", nargs="+")
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            return run_arm(args.arm, args.case)
        if args.command == "prompts":
            return write_prompts(args.arm)
        if args.command == "ingest":
            return ingest(args.arm, args.case, args.json)
        return score([arm for group in args.arm for arm in group])
    except BakeoffError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
