"""Lane registry and the one place a lane's tool is run.

A lane module exposes:

* ``name`` -- the lane name, also its key under ``lanes:`` in the config;
* ``tool`` -- the distribution and console-script name of its metric tool;
* ``metrics_file`` -- where the tool writes its EDN, relative to the analysis root;
* ``executable(ctx)`` -- the tool to run, by default the venv's console script;
* ``argv(ctx)`` -- the arguments after the executable;
* ``parse(ctx)`` -- the ranked findings read from ``metrics_file``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .. import parsers
from ..environment import tool_path
from ..errors import ToolFailure
from ..model import LaneContext, LaneResult
from . import complexity, duplication

LANES = {module.name: module for module in (duplication, complexity)}

NO_SOURCE_FILES = "No source files to analyze."
PARSER_HINT = ("parsers are fetched on first use and need network once "
               "(tree-sitter-language-pack, cached in {cache})")


def default_executable(ctx: LaneContext, tool: str) -> Path:
    return tool_path(ctx.venv_prefix, tool)


def run_tool(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def run_lane(lane, ctx: LaneContext, tool_commit: str) -> LaneResult:
    """Run one lane's tool in the analysis root and read its findings; ToolFailure on failure."""
    metrics = ctx.analysis_root / lane.metrics_file
    if metrics.exists():
        metrics.unlink()  # never read an EDN the tool did not write in this run
    argv = [str(lane.executable(ctx)), *lane.argv(ctx)]
    ctx.parser_cache.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{parsers.CACHE_ENV: str(ctx.parser_cache)})
    started = time.monotonic()
    proc = run_tool(argv, ctx.analysis_root, env)
    seconds = round(time.monotonic() - started, 3)
    if proc.returncode != 0:
        lines = [proc.stderr.rstrip("\n"),
                 f"clean-code-kit: {lane.name} lane: {lane.tool} exited {proc.returncode}"]
        if "DownloadError" in proc.stderr:
            lines.append("clean-code-kit: " + PARSER_HINT.format(cache=ctx.parser_cache))
        raise ToolFailure("\n".join(line for line in lines if line))
    if not metrics.exists():
        if NO_SOURCE_FILES in proc.stdout.splitlines():
            return LaneResult(lane.name, lane.tool, tool_commit, argv, proc.returncode, seconds,
                              "no-source-files", [])
        raise ToolFailure(
            f"clean-code-kit: {lane.name} lane: {lane.tool} exited 0 but wrote no {lane.metrics_file}"
            f"\n{proc.stdout}{proc.stderr}".rstrip())
    return LaneResult(lane.name, lane.tool, tool_commit, argv, proc.returncode, seconds, "ok",
                      lane.parse(ctx))
