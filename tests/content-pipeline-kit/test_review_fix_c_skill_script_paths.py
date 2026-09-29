"""Shipped Markdown must not tell a reader to launch a workflow script by a
repository-relative ``plugins/<name>/...`` path.

Such a path resolves only inside a plugins-kit checkout; a consumer project
has no such file, and the Workflow tool refuses a path inside the plugin
cache. The registered name is the invocation that works everywhere.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "content-pipeline-kit"

# A repo-relative plugin path ending in a workflow script or living under a
# workflows/ directory.
REPO_RELATIVE_SCRIPT = re.compile(
    r"plugins/[A-Za-z0-9_.-]+/(?:workflows/[^\s`'\")]*|[^\s`'\")]*\.(?:js|workflow\.ya?ml)\b)"
)
NAMED_INVOCATION = 'name: "content-pipeline-kit:run-ready-wave"'


def offending_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if REPO_RELATIVE_SCRIPT.search(ln)]


def shipped_markdown() -> list[Path]:
    return sorted(p for p in PLUGIN.rglob("*.md") if "node_modules" not in p.parts)


def test_no_shipped_markdown_uses_a_repo_relative_script_path():
    bad = {
        str(p.relative_to(REPO)): offending_lines(p.read_text(encoding="utf-8"))
        for p in shipped_markdown()
    }
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, bad


def test_workflow_skill_documents_the_named_invocation():
    text = (PLUGIN / "skills" / "workflow-pipeline" / "SKILL.md").read_text(encoding="utf-8")
    assert NAMED_INVOCATION in text


def test_guard_fires_on_the_text_before_the_fix():
    """The guard must fail on the committed pre-fix skill text, so a green
    run above is not vacuous."""
    rel = "plugins/content-pipeline-kit/skills/workflow-pipeline/SKILL.md"
    pre_fix = subprocess.run(
        ["git", "show", f"532a01d1:{rel}"], cwd=REPO, capture_output=True, text=True, check=False
    )
    if pre_fix.returncode != 0:
        pytest.skip("pre-fix revision not available")
    assert offending_lines(pre_fix.stdout)


@pytest.mark.parametrize(
    "line",
    [
        "input: plugins/content-pipeline-kit/workflows/run-ready-wave.js",
        "run `plugins/other-kit/workflows/x.workflow.yaml`",
    ],
)
def test_pattern_matches_repo_relative_script_paths(line):
    assert offending_lines(line)


@pytest.mark.parametrize(
    "line",
    [
        'Workflow({name: "content-pipeline-kit:run-ready-wave", args: x})',
        "the `run-ready-wave.js` script",
        "plugins/content-pipeline-kit/lib/content_pipeline/x.py",
    ],
)
def test_pattern_accepts_named_and_non_script_references(line):
    assert not offending_lines(line)
