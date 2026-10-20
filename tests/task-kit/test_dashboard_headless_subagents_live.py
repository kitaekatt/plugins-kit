"""Live: the installed Claude Code against the characterization (real sessions; set TASK_KIT_LIVE_CLAUDE=1).

The sessions run the real ``claude`` with the real llm-kit worker argv, under the real HOME (so the CLI is logged
in), in a scratch dashboard root: nothing here touches the production dashboard or its record. Set
``TASK_KIT_LIVE_ENTRY`` to choose the model entry (default ``haiku``, the cheapest) and ``TASK_KIT_LIVE_ROOT`` to keep
the evidence in a directory of your choosing.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

import pytest
from dashboard_support import REPO_ROOT, SCRIPTS_DIR

from task_system.dashboard import subagents
from task_system.dashboard.config import load_config

LIVE = os.environ.get("TASK_KIT_LIVE_CLAUDE") == "1"
pytestmark = pytest.mark.skipif(not LIVE, reason="spends real Claude sessions; set TASK_KIT_LIVE_CLAUDE=1 to run")

HELPER_CLI = SCRIPTS_DIR / "task_system" / "dashboard" / "helper_cli.py"


def _edge(monkeypatch):
    import importlib

    assert shutil.which("claude"), "TASK_KIT_LIVE_CLAUDE=1 needs the claude CLI on PATH"
    monkeypatch.delenv("CLAUDECODE", raising=False)  # a nested claude must not believe it runs inside another session
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_TEST", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "plugins" / "llm-kit" / "lib"))
    importlib.invalidate_caches()
    from task_system.dashboard.jobs_edge import JobsEdge

    edge = JobsEdge(ttl_s=0.0)
    assert edge.status().available, edge.status().detail
    return edge


def _root(tmp_path: Path, name: str) -> Path:
    kept = os.environ.get("TASK_KIT_LIVE_ROOT")
    return (Path(kept) if kept else tmp_path) / name


def test_the_installed_claude_code_passes_every_check(tmp_path, monkeypatch):
    """R22. ``characterize`` passes every check on this machine; sub-agents would be ON."""
    out = io.StringIO()
    claude, decision = subagents.characterize(
        root=_root(tmp_path, "dashboard"), edge=_edge(monkeypatch), config=load_config(tmp_path / "none.yaml"), python=sys.executable,
        helper_cli=HELPER_CLI, scripts_dir=SCRIPTS_DIR, entry=os.environ.get("TASK_KIT_LIVE_ENTRY", "haiku"), out=out)
    print(out.getvalue())
    gating = {name: check["verdict"] for name, check in claude["checks"].items() if name not in subagents.INFORMATIONAL}
    assert set(gating.values()) == {"pass"}, claude["checks"]
    assert decision.on, decision.reason


def test_without_the_depth_limit_the_depth_check_fails(tmp_path, monkeypatch):
    """R22, RED, live: leg A without the depth variable lets a grandchild launch, so ``depth_cap`` fails and the verdict is OFF."""
    real_plans = subagents.leg_plans

    def without_depth(python, helper_cli):
        first = real_plans(python, helper_cli)[0]
        return (subagents.LegPlan(first.name, "NO depth limit (red check)", first.request, {}),)

    monkeypatch.setattr(subagents, "leg_plans", without_depth)
    out = io.StringIO()
    claude, decision = subagents.characterize(
        root=_root(tmp_path, "red"), edge=_edge(monkeypatch), config=load_config(tmp_path / "none.yaml"), python=sys.executable,
        helper_cli=HELPER_CLI, scripts_dir=SCRIPTS_DIR, entry=os.environ.get("TASK_KIT_LIVE_ENTRY", "haiku"), out=out)
    print(out.getvalue())
    assert claude["checks"]["depth_cap"]["verdict"] == "fail", claude["checks"]["depth_cap"]
    assert not decision.on
