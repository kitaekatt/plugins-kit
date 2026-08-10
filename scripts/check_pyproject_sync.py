#!/usr/bin/env python3
"""Block commits where plugins/<name>/pyproject.toml states a version that
disagrees with the authoritative plugins/<name>/.claude-plugin/plugin.json.

Why this is a pre-commit hook and not only a test
-------------------------------------------------
It was only a test (tests/repo-scripts/test_pyproject_version_sync.py), and the
rule lost anyway -- five times. pyproject stopped being bumped at bootstrap
0.39.0; 0.40 through 0.43 each drifted further; a sync commit set them equal at
0.43.0; the VERY NEXT commit (bootstrap 0.44.0) drifted it again. The test
caught none of that at the time, because catching it required someone to run
the full suite -- which CLAUDE.md explicitly tells you not to do routinely, and
which publish.py does not run either. So the drift shipped to consumers and was
found later by a full run that had nothing to do with the release.

The failure is structural, not careless: check-staged-version-bump.sh REQUIRES
a plugin.json bump whenever you touch plugins/<name>/, and nothing pulls
pyproject along with it. The enforced rule was manufacturing the drift the
unenforced rule was supposed to catch. This closes that at the moment the bump
is staged, which is the only moment the author is thinking about versions --
the same reasoning as check-staged-version-bump.sh's own header.

Scope: plugin.json is the source of truth. pyproject versions are
non-authoritative, so a pyproject with no version (or no pyproject at all) is
out of scope -- the rule is just "if you state one, it must not lie".

Escape hatch:  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or --no-verify)

Called by scripts/pre-commit-version-check.sh; also importable, so
tests/repo-scripts/test_pyproject_version_sync.py asserts against this exact
rule rather than a second copy of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

# Index-awareness helpers, mirroring scripts/generate_orchestration.py's
# staged_paths / index_blob so the two commit gates read staged state the same
# way. Both return None when Git cannot answer, and every caller falls back to
# the working tree in that case.


def is_git_repo(repo_root: Path) -> bool:
    return (repo_root / ".git").exists()


def staged_paths(repo_root: Path) -> list[str] | None:
    """Repo-relative staged paths, or None when Git does not answer."""

    if not is_git_repo(repo_root):
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace")
    return [
        line.strip().replace("\\", "/")
        for line in text.splitlines()
        if line.strip()
    ]


def index_blob(repo_root: Path, rel_path: str) -> str | None:
    """Return a staged blob as text, or None when Git cannot provide it."""

    try:
        proc = subprocess.run(
            ["git", "show", f":{rel_path}"],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def plugins_with_both_files(plugins_dir: Path | None = None) -> list[Path]:
    """Plugin dirs declaring both a pyproject.toml and a plugin.json."""
    root = PLUGINS_DIR if plugins_dir is None else plugins_dir
    out = []
    for plugin_dir in sorted(root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        if (plugin_dir / "pyproject.toml").is_file() and (
            plugin_dir / ".claude-plugin" / "plugin.json"
        ).is_file():
            out.append(plugin_dir)
    return out


def _read_pair(
    plugin_dir: Path,
    repo_root: Path | None,
    from_index: bool,
) -> tuple[str, str]:
    """The pyproject and plugin.json text this check should judge.

    Index blobs when anything is staged, else the working tree. A file Git
    cannot provide (untracked, or no Git at all) falls back to the working tree
    individually, matching generate_orchestration.py's fallback.
    """
    py_path = plugin_dir / "pyproject.toml"
    pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
    py_text = pj_text = None
    if from_index and repo_root is not None:
        rel = plugin_dir.relative_to(repo_root).as_posix()
        py_text = index_blob(repo_root, f"{rel}/pyproject.toml")
        pj_text = index_blob(repo_root, f"{rel}/.claude-plugin/plugin.json")
    if py_text is None:
        py_text = py_path.read_text(encoding="utf-8")
    if pj_text is None:
        pj_text = pj_path.read_text(encoding="utf-8")
    return py_text, pj_text


def find_drift(
    plugins_dir: Path | None = None,
    staged: Sequence[str] | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    """Human-readable drift lines, empty when every stated version agrees.

    Judges the INDEX when anything is staged, because the index is what the
    commit will contain. Reading the working tree instead left a hole this
    check's own header describes without naming: the sanctioned fix could be
    made in the working tree, never staged, and the gate would pass while the
    commit still carried the drift into HEAD. That is how bootstrap's stated
    version drifted across five releases -- each "fixed" commit shipped the old
    pyproject anyway. ``staged`` is the test injection seam; unavailable Git
    data falls back to the working tree.
    """
    root = PLUGINS_DIR if plugins_dir is None else plugins_dir
    if repo_root is None:
        repo_root = root.parent
    staged_set = staged_paths(repo_root) if staged is None else list(staged)
    from_index = bool(staged_set) and is_git_repo(repo_root)

    drift = []
    for plugin_dir in plugins_with_both_files(plugins_dir):
        py_text, pj_text = _read_pair(plugin_dir, repo_root, from_index)
        py_version = tomllib.loads(py_text).get("project", {}).get("version")
        if py_version is None:
            continue  # version-less pyproject: nothing to drift
        pj_version = json.loads(pj_text).get("version")
        if py_version != pj_version:
            drift.append(
                f"{plugin_dir.name}: pyproject.toml={py_version} "
                f"plugin.json={pj_version}")
    return drift


def main(argv: list[str]) -> int:
    if os.environ.get("PLUGINS_KIT_SKIP_BUMP_CHECK") == "1":
        return 0
    drift = find_drift()
    if not drift:
        return 0
    print(
        "pyproject.toml versions drifted from the authoritative plugin.json:",
        file=sys.stderr)
    for line in drift:
        print(f"  {line}", file=sys.stderr)
    print(
        "\nplugin.json is the source of truth -- set each pyproject.toml "
        "version equal to it and stage the result.\n"
        "Versions above are read from the index (what the commit will "
        "contain), so an edit you have not staged yet will not show here:\n"
        "  git add plugins/<name>/pyproject.toml\n"
        "(Intentional dev commit? PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...)",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
