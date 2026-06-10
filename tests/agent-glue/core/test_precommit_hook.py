"""Smoke tests for the pre-commit consistency hook (facade verification only)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "agent-glue"
HOOK = PLUGIN_ROOT / "scripts" / "precommit_consistency.py"


def run_hook(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK), *extra],
        capture_output=True,
        text=True,
        cwd=PLUGIN_ROOT,
    )


def test_hook_accepts_shipped_kit():
    result = run_hook()
    assert result.returncode == 0, result.stderr
    assert "agent-glue kit OK" in result.stdout


def test_hook_rejects_broken_instance(tmp_path):
    bad_dir = tmp_path / "broken"
    (bad_dir).mkdir()
    (bad_dir / "bad.yaml").write_text(
        textwrap.dedent("""
            type: Node
            components:
              name:
                name: load
              topology:
                in: LoadIn
              # missing required 'function' on Implementation; 'out' missing on Topology
              implementation:
                module: impl
        """).lstrip(),
        encoding="utf-8",
    )
    result = run_hook("--instances", str(bad_dir))
    assert result.returncode == 1
    assert "missing required fields" in result.stderr


def test_hook_accepts_valid_instance(tmp_path):
    good = tmp_path / "good"
    good.mkdir()
    (good / "load.yaml").write_text(
        textwrap.dedent("""
            type: Node
            components:
              name:
                name: load
              topology:
                in: LoadIn
                out: LoadResult
              implementation:
                module: impl
                function: execute
        """).lstrip(),
        encoding="utf-8",
    )
    result = run_hook("--instances", str(good))
    assert result.returncode == 0, result.stderr
    assert "1 instances" in result.stdout
