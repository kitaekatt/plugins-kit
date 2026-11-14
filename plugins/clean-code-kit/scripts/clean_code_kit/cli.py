"""Command line: ``run`` writes a report; ``lanes`` lists the registered lanes."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from . import config as config_mod
from . import environment, report, snapshot
from .errors import EXIT_REFUSED, EXIT_TOOL_FAILED, Refusal, ToolFailure
from .lanes import LANES, run_lane
from .model import LaneContext


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
    sub.add_parser("lanes", help="print the registered lane names, one per line")
    return parser


def _selected_lanes(requested: list[str] | None, cfg: config_mod.Config) -> list[str]:
    if requested:
        return [name for name in LANES if name in requested]
    enabled = cfg.enabled_lanes()
    if not enabled:
        raise Refusal(f"{cfg.path}: no lane is enabled; enable one or pass --lane")
    return [name for name in LANES if name in enabled]


def run(args: argparse.Namespace) -> Path:
    repo = snapshot.repo_root((args.target or Path.cwd()).resolve())
    cfg = config_mod.load(repo)
    names = _selected_lanes(args.lane, cfg)
    tools = environment.gate([LANES[name].tool for name in names])
    mode = args.snapshot or cfg.snapshot
    head = snapshot.head_sha(repo)
    target = {"repo": str(repo), "head": head, "snapshot": mode,
              "dirty_paths": snapshot.dirty_paths(repo)}
    out_dir = report.report_dir(repo, head)
    report.ensure_ignored(repo, out_dir)
    report.ensure_new(out_dir)

    scratch = Path(tempfile.mkdtemp(prefix="clean-code-kit-"))
    try:
        snapshot.take(repo, mode, scratch)
        snapshot.require_roots(scratch, cfg.source_roots, "source_roots", cfg.path, mode)
        snapshot.require_roots(scratch, cfg.test_roots, "test_roots", cfg.path, mode)
        snapshot.prune(scratch, cfg.exclude, cfg.languages)
        results = []
        for name in names:
            lane = LANES[name]
            ctx = LaneContext(
                repo=repo, analysis_root=scratch, exec_env=None, settings=cfg.lanes[name],
                source_roots=cfg.source_roots, test_roots=cfg.test_roots,
                languages=cfg.languages, venv_prefix=environment.venv_prefix(),
                parser_cache=environment.parser_cache_dir())
            result = run_lane(lane, ctx, tools[lane.tool].commit)
            print(f"{name}: {result.status}, {len(result.findings)} findings, {result.seconds:.1f} s")
            results.append(result)
    finally:
        shutil.rmtree(scratch)

    report.write(out_dir, report.findings_document(target, results),
                 report.markdown(target, results, cfg.lanes["complexity"]["top"]))
    return out_dir


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "lanes":
        for name in LANES:
            print(name)
        return 0
    try:
        out_dir = run(args)
    except Refusal as exc:
        print(f"clean-code-kit: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except ToolFailure as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_TOOL_FAILED
    print(f"REPORT {out_dir}")
    return 0
