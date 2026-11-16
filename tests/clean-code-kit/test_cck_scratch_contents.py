"""The scratch directory holds only the files the audit reads, in both snapshot modes.

Writing every tracked file of the target to scratch only to prune most of it
afterwards fails on Windows when a tracked file outside the source roots has a
path that does not fit MAX_PATH once the scratch prefix is added. These tests
assert the property (what is in scratch when the tool runs), not the crash, so
they fail on any OS.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from cck_support import git
from test_cck_run import fake_venv  # noqa: F401  (fixture reused from the run tests)

from clean_code_kit import cli
from clean_code_kit import lanes as lanes_mod

DEEP = "/".join(["deeply-nested-directory-outside-the-roots"] * 3)
OUT_OF_ROOT = f"docs/{DEEP}/notes.py"


@pytest.fixture
def scratch_listing(monkeypatch):
    """Record the files under the analysis root each time a tool is run."""
    seen: list[list[str]] = []

    def run_tool(argv, cwd, env):
        root = Path(cwd)
        seen.append(sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()))
        return subprocess.CompletedProcess(argv, 0, "No source files to analyze.\n", "")

    monkeypatch.setattr(lanes_mod, "run_tool", run_tool)
    return seen


@pytest.mark.parametrize("mode", ["head", "working-tree"])
def test_scratch_holds_only_in_root_files_that_survive_exclude_and_languages(
        target_repo, fake_venv, scratch_listing, mode):  # noqa: F811
    (target_repo / OUT_OF_ROOT).parent.mkdir(parents=True)
    (target_repo / OUT_OF_ROOT).write_text("def elsewhere():\n    return 1\n")
    (target_repo / "README.md").write_text("outside every root\n")
    git(target_repo, "add", "-A")
    git(target_repo, "commit", "-q", "-m", "add files outside the roots")

    assert cli.main(["run", "--target", str(target_repo), "--snapshot", mode, "--lane", "complexity"]) == 0

    # in src or tests, python (the configured language), not excluded
    assert scratch_listing == [["src/pkg/a.py", "src/pkg/b.py", "src/pkg/package.json", "tests/test_a.py"]]
    # not copied: out of every root, excluded by glob, or of an unconfigured language
    listing = scratch_listing[0]
    assert OUT_OF_ROOT not in listing
    assert "README.md" not in listing and ".gitignore" not in listing
    assert "src/pkg/bootstrap_guard.py" not in listing
    assert "src/pkg/view.ts" not in listing
