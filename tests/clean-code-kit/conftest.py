"""Fixtures for clean-code-kit tests."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

# code_audit.py calls bootstrap_guard.reexec_under_plugin_venv(), which would
# os.execv the pytest process into the plugin venv on a provisioned machine and
# yield a false green. The skip variable makes the re-exec a no-op under tests;
# see plugins/CLAUDE.md, "Test gotcha".
os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")

from cck_support import CONFIG_TEXT, FIXTURES, SCRIPTS, git

if str(SCRIPTS) not in sys.path:
    sys.path.append(str(SCRIPTS))


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """A committed git repository with sources, tests, a manifest and the config."""
    repo = tmp_path / "target"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / ".claude" / "clean-code-kit").mkdir(parents=True)
    for name in ("a.py", "b.py"):
        shutil.copy(FIXTURES / "sample_project" / "src" / "pkg" / name, repo / "src" / "pkg" / name)
    (repo / "src" / "pkg" / "view.ts").write_text("export const x = 1;\n")
    (repo / "src" / "pkg" / "bootstrap_guard.py").write_text("def guard():\n    return 1\n")
    (repo / "src" / "pkg" / "package.json").write_text('{"name": "pkg"}\n')
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    (repo / ".gitignore").write_text(".local-data/\nignored.txt\n")
    (repo / ".claude" / "clean-code-kit" / "config.yaml").write_text(CONFIG_TEXT)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo
