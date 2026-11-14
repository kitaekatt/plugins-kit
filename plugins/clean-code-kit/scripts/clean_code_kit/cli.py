"""Command line: ``run`` writes a report; ``lanes`` lists the registered lanes."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import config as config_mod
from . import coverage, environment, init, report, snapshot
from .errors import EXIT_REFUSED, EXIT_TOOL_FAILED, Refusal, ToolFailure
from .lanes import LANES, run_lane
from .model import LaneContext, LaneResult

MIN_COMPARABLE_FILES = 2  # a duplicate is a pair of forms, so dryer needs two files to compare


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="code_audit.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="audit a git repository and write a report")
    run.add_argument("--target", type=Path, default=None,
                     help="a directory inside the repository to audit (default: the current directory)")
    run.add_argument("--lane", action="append", choices=list(LANES), default=None,
                     help="run this lane; repeatable (default: every lane the config enables)")
    run.add_argument("--snapshot", choices=list(config_mod.SNAPSHOT_MODES), default=None,
                     help="what to analyze: the HEAD commit or the working tree (default: from config)")
    ini = sub.add_parser("init", help="print a proposed config.yaml for a repository; writes nothing")
    ini.add_argument("--target", type=Path, default=None,
                     help="a directory inside the repository (default: the current directory)")
    sub.add_parser("lanes", help="print the registered lane names, one per line")
    return parser


def _selected_lanes(requested: list[str] | None, cfg: config_mod.Config) -> list[str]:
    if requested:
        return [name for name in LANES if name in requested]
    enabled = cfg.enabled_lanes()
    if not enabled:
        raise Refusal(f"{cfg.path}: no lane is enabled; enable one or pass --lane")
    return [name for name in LANES if name in enabled]


def _preflight(args: argparse.Namespace, repo: Path) -> tuple[config_mod.Config, list[str], dict, str, Path]:
    """Check every precondition and raise one Refusal naming all that failed."""
    problems: list[str] = []

    def attempt(check, *check_args):
        try:
            return check(*check_args)
        except Refusal as exc:
            problems.append(str(exc))
            return None

    cfg = attempt(config_mod.load, repo)
    names = attempt(_selected_lanes, args.lane, cfg) if cfg else [n for n in LANES if n in (args.lane or LANES)]
    tools: dict = {}
    for name in names or []:
        found = attempt(environment.gate, [LANES[name].tool])
        if found:
            tools.update(found)
    head = attempt(snapshot.head_sha, repo)
    out_dir = report.report_dir(repo, head) if head else None
    if out_dir is not None:
        attempt(report.ensure_ignored, repo, out_dir)
        attempt(report.ensure_new, out_dir)
    if len(problems) == 1:
        raise Refusal(problems[0])
    if problems:
        raise Refusal(f"{len(problems)} preconditions failed:\n" + "\n".join(f"- {p}" for p in problems))
    return cfg, names, tools, head, out_dir


def _comparable_unreadable(lane_name: str, tool_name: str, commit: str) -> LaneResult:
    return LaneResult(lane_name, tool_name, commit, [], 0, 0.0, "no-comparable-source", [])


def run(args: argparse.Namespace) -> Path:
    repo = snapshot.repo_root((args.target or Path.cwd()).resolve())
    cfg, names, tools, head, out_dir = _preflight(args, repo)
    mode = args.snapshot or cfg.snapshot
    dirty = snapshot.dirty_paths(repo, ignore=config_mod.CONFIG_REL.as_posix())
    target = {"repo": str(repo), "head": head, "snapshot": mode,
              "dirty_paths": len(dirty), "dirty_path_names": dirty}

    scratch = Path(tempfile.mkdtemp(prefix="clean-code-kit-"))
    try:
        snapshot.take(repo, mode, scratch)
        snapshot.require_roots(scratch, cfg.source_roots, "source_roots", cfg.path, mode)
        snapshot.require_roots(scratch, cfg.test_roots, "test_roots", cfg.path, mode)
        snapshot.prune(scratch, cfg.exclude, cfg.languages)
        selection = coverage.select(scratch, cfg.source_roots, list(LANES))
        target["coverage"] = coverage.table(snapshot.tracked_files(repo, mode),
                                            {n: selection[n] for n in names})
        results = []
        for name in names:
            lane = LANES[name]
            has_source = any(selection.values())  # an empty selection is the tool's no-source-files, not this
            if name == "duplication" and has_source and len(selection[name]) < MIN_COMPARABLE_FILES:
                result = _comparable_unreadable(name, lane.tool, tools[lane.tool].commit)
                result.detail["comparable_files"] = len(selection[name])
            else:
                ctx = LaneContext(
                    repo=repo, analysis_root=scratch, exec_env=None, settings=cfg.lanes[name],
                    source_roots=cfg.source_roots, test_roots=cfg.test_roots,
                    languages=cfg.languages, venv_prefix=environment.venv_prefix(),
                    parser_cache=environment.parser_cache_dir())
                result = run_lane(lane, ctx, tools[lane.tool].commit)
                if name == "duplication":
                    result.detail["comparable_files"] = len(selection[name])
                elif name == "complexity" and result.status == "ok":
                    coverage.annotate_complexity(result, selection[name])
            if name == "duplication":
                result.detail["ignored_extensions"] = coverage.ignored_by(name, [
                    p for paths in selection.values() for p in paths])
            print(f"{name}: {result.status}, {len(result.findings)} findings, {result.seconds:.1f} s")
            results.append(result)
    finally:
        shutil.rmtree(scratch)

    report.write(out_dir, report.findings_document(target, results),
                 report.markdown(target, results, cfg.lanes["complexity"]))
    return out_dir


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "lanes":
        for name in LANES:
            print(name)
        return 0
    try:
        if args.command == "init":
            print(init.propose((args.target or Path.cwd()).resolve()), end="")
            return 0
        out_dir = run(args)
    except Refusal as exc:
        print(f"clean-code-kit: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ToolFailure as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_TOOL_FAILED
    print(f"REPORT {out_dir}")
    return 0
