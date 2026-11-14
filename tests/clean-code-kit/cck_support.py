"""Shared helpers for the clean-code-kit tests (imported by name; unique across the suite)."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "clean-code-kit"
SCRIPTS = PLUGIN_ROOT / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONFIG_TEXT = """\
schema: 1
snapshot: head
languages: [python]
source_roots: [src]
test_roots: [tests]
exclude:
  - "**/bootstrap_guard.py"
lanes:
  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}
  complexity:  {enabled: true, coverage: false, top: 50}
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        check=True, capture_output=True, text=True).stdout


def make_site(site: Path, commits: dict[str, str]) -> Path:
    """A site-packages directory holding uv-style dist-info records for ``commits``."""
    import json

    for dist, commit in commits.items():
        info = site / f"{dist}-0.1.0.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
        (info / "direct_url.json").write_text(json.dumps(
            {"url": f"https://github.com/kitaekatt/{dist}",
             "vcs_info": {"vcs": "git", "commit_id": commit, "requested_revision": commit}}))
    return site
