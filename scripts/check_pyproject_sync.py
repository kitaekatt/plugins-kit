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

Usage:
  python scripts/check_pyproject_sync.py            # full working-tree sweep
  python scripts/check_pyproject_sync.py --staged   # the pre-commit gate:
      index-aware AND scoped to the plugins this commit actually stages

Escape hatch:  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or --no-verify)

Called by scripts/pre-commit-version-check.sh; also importable, so
tests/repo-scripts/test_pyproject_version_sync.py asserts against this exact
rule rather than a second copy of it.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from collections.abc import Collection, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gitindex  # noqa: E402  (shared Git-index helpers; stdlib-only)

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

# Index-awareness comes from scripts/_gitindex.py, which is the single
# implementation shared by every commit gate here (it used to be a near-copy
# per script, with divergent timeouts and fallbacks). These thin wrappers keep
# this module's own names importable.

is_git_repo = _gitindex.is_git_repo


def staged_paths(repo_root: Path) -> list[str] | None:
    """Repo-relative staged paths, or None when Git does not answer."""

    return _gitindex.staged_paths(repo_root)


def index_blob(repo_root: Path, rel_path: str) -> str | None:
    """Return a staged blob as text, or None when Git cannot provide it."""

    return _gitindex.index_text(repo_root, rel_path)


def is_derivation_input(path: str) -> bool:
    """Does this repo-relative path participate in the version-sync rule?"""

    return path.startswith("plugins/") and (
        path.endswith("/pyproject.toml")
        or path.endswith("/.claude-plugin/plugin.json")
    )


def staged_plugin_names(staged: Sequence[str]) -> set[str]:
    """Plugin dir names whose pyproject/plugin.json this commit stages."""

    names = set()
    for path in staged:
        if not is_derivation_input(path):
            continue
        parts = path.split("/")
        if len(parts) >= 3:
            names.add(parts[1])
    return names


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
    only: Collection[str] | None = None,
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

    ``only`` narrows the sweep to the named plugin dirs. It is how the
    ``--staged`` commit gate scopes itself; the default (None) is the FULL
    sweep, which publish.py and the drift test depend on.
    """
    root = PLUGINS_DIR if plugins_dir is None else plugins_dir
    if repo_root is None:
        repo_root = root.parent
    staged_set = staged_paths(repo_root) if staged is None else list(staged)
    from_index = bool(staged_set) and is_git_repo(repo_root)

    drift = []
    for plugin_dir in plugins_with_both_files(plugins_dir):
        if only is not None and plugin_dir.name not in only:
            continue
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

    # --staged is the pre-commit gate: judge the COMMIT, not the shared working
    # tree. Index-awareness alone was not enough -- this check judged EVERY
    # plugin whenever ANYTHING was staged, so a concurrent session's in-flight
    # pyproject drift in an unrelated plugin blocked every other session's
    # commit. A commit that stages neither file for a plugin cannot change that
    # plugin's version agreement, so it has nothing to answer for; the drift
    # belongs to whichever commit introduces it, and publish.py's preflight
    # runs the FULL sweep (find_drift() with no scoping) before anything can
    # reach master.
    where = "working-tree"
    if "--staged" in argv:
        repo_root = PLUGINS_DIR.parent
        verdict, staged = _gitindex.classify_scope(
            repo_root, is_derivation_input)
        if verdict == _gitindex.SCOPE_SKIP:
            return 0
        if verdict == _gitindex.SCOPE_WORKTREE:
            # A check that silently passes when its input is unavailable is not
            # a check: fall back to the full working-tree sweep, loudly.
            print(
                "check_pyproject_sync: could not read the index; "
                "checking the working tree instead.",
                file=sys.stderr)
            drift = find_drift()
        else:
            where = "staged"
            drift = find_drift(
                staged=staged, only=staged_plugin_names(staged))
    else:
        drift = find_drift()

    if not drift:
        return 0
    print(
        f"pyproject.toml versions drifted from the authoritative plugin.json "
        f"({where} inputs):",
        file=sys.stderr)
    for line in drift:
        print(f"  {line}", file=sys.stderr)
    print(
        "\nplugin.json is the source of truth -- set each pyproject.toml "
        "version equal to it and stage the result.\n"
        f"Versions above are read from the {where} inputs"
        + (" (what the commit will contain), so an edit you have not staged "
           "yet will not show here" if where == "staged" else "")
        + ":\n  git add plugins/<name>/pyproject.toml\n"
        "(Intentional dev commit? PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...)",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
