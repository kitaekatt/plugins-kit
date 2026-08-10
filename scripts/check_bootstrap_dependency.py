#!/usr/bin/env python3
"""Block commits where a plugin does not declare the bootstrap plugin as a
dependency in its .claude-plugin/plugin.json.

Why this invariant matters
--------------------------
EVERY plugin in this marketplace depends on bootstrap. CLAUDE.md ("Plugin
dependencies on bootstrap") makes that edge explicit via the Claude Code
plugin spec:

    "dependencies": ["bootstrap"]

as a BARE STRING (same-marketplace dep -- no "marketplace" field, no version
constraint; both break installs, see CLAUDE.md). Without the edge, a user can
install the plugin without bootstrap: any bootstrap.json it ships is never
processed (no venv, no tools, no auto-update) and the plugin fails at first
use with a raw traceback instead of never getting into that state.

Exempt: the bootstrap plugin itself (a self-dependency is meaningless). There
is no other exemption -- the rule is universal by design, so that anything
built on "every plugin can rely on bootstrap being present" holds without a
per-plugin check. A plugin that ships no bootstrap.json still declares the
edge; the fleet-wide user posture bootstrap owns is readable from every
plugin precisely because of that (docs/reference/first-run-experience.md).

Superseded: this check previously scoped itself to plugins that ship a
bootstrap.json, and CLAUDE.md told plugins without one NOT to declare the
edge. That carve-out is retired -- agent-glue, its only occupant, now
declares the dependency like everything else.

Enforced at pre-commit (chained from scripts/pre-commit-version-check.sh),
not only as a test: this repo's history shows suite-only invariants lose
(see scripts/check_pyproject_sync.py's header for the five-release drift
that motivated the pattern).

Usage:
  python scripts/check_bootstrap_dependency.py            # full worktree sweep
  python scripts/check_bootstrap_dependency.py --staged   # the pre-commit gate:
      index-aware AND scoped to the plugins this commit actually stages

Escape hatch:  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or --no-verify)

Also importable: tests/repo-scripts/test_bootstrap_dependency.py asserts
against this exact rule rather than a second copy of it.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Collection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _gitindex  # noqa: E402  (shared Git-index helpers; stdlib-only)

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

BOOTSTRAP_PLUGIN_NAME = "bootstrap"


def is_derivation_input(path: str) -> bool:
    """Does this repo-relative path participate in the dependency rule?

    bootstrap.json counts even though the rule no longer reads it: staging one
    is how a new plugin arrives, and that is precisely the commit that must be
    made to declare the edge.
    """
    return path.startswith("plugins/") and (
        path.endswith("/.claude-plugin/plugin.json")
        or path.endswith("/bootstrap.json")
    )


def staged_plugin_names(staged: Collection[str]) -> set[str]:
    """Plugin dir names whose manifests this commit stages."""
    names = set()
    for path in staged:
        if not is_derivation_input(path):
            continue
        parts = path.split("/")
        if len(parts) >= 3:
            names.add(parts[1])
    return names


def _declares_bootstrap(manifest: dict) -> bool:
    """True when plugin.json's dependencies include the bootstrap plugin.

    Canonical form is the bare string "bootstrap"; a dict entry with
    name == "bootstrap" is tolerated (the spec allows it) but the bare
    string is what this repo uses.
    """
    for dep in manifest.get("dependencies") or []:
        if isinstance(dep, str) and dep == BOOTSTRAP_PLUGIN_NAME:
            return True
        if isinstance(dep, dict) and dep.get("name") == BOOTSTRAP_PLUGIN_NAME:
            return True
    return False


def _plugin_dir_names(
    root: Path, repo_root: Path | None, from_index: bool
) -> list[str]:
    """Plugin dir names in the snapshot being judged.

    Under from_index the names come from the INDEX, so a directory that exists
    only in the shared working tree -- another session scaffolding a plugin --
    is not part of this commit and is not judged by it.
    """
    if from_index and repo_root is not None:
        rels = _gitindex.index_files(repo_root, "plugins/*")
        if rels is not None:
            return sorted({
                parts[1] for parts in (r.split("/") for r in rels)
                if len(parts) >= 3 and parts[0] == "plugins"
            })
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def _manifest_text(
    plugin_dir: Path, repo_root: Path | None, from_index: bool
) -> str | None:
    """plugin.json text from the index or the working tree; None when absent.

    Deliberately no per-file worktree fallback under from_index: a plugin.json
    absent from the index is absent from the COMMIT, and mixing the two sources
    produces a snapshot that is neither.
    """
    pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if from_index and repo_root is not None:
        rel = pj_path.relative_to(repo_root).as_posix()
        return _gitindex.index_text(repo_root, rel)
    if not pj_path.is_file():
        return None
    try:
        return pj_path.read_text(encoding="utf-8")
    except OSError:
        return None


def find_outliers(
    plugins_dir: Path | None = None,
    *,
    repo_root: Path | None = None,
    from_index: bool = False,
    only: Collection[str] | None = None,
) -> list[str]:
    """Human-readable outlier lines; empty when the invariant holds.

    An outlier is any plugin dir (other than bootstrap itself) whose
    plugin.json is missing, unparseable, or lacks the bootstrap dependency.

    Defaults are the FULL working-tree sweep that publish.py and the invariant
    test depend on. ``from_index`` / ``only`` are how the ``--staged`` commit
    gate narrows itself to the commit at hand.
    """
    root = PLUGINS_DIR if plugins_dir is None else plugins_dir
    if from_index and repo_root is None:
        repo_root = root.parent
    outliers = []
    for name in _plugin_dir_names(root, repo_root, from_index):
        if only is not None and name not in only:
            continue
        plugin_dir = root / name
        if not from_index and not plugin_dir.is_dir():
            continue
        text = _manifest_text(plugin_dir, repo_root, from_index)
        if text is None:
            outliers.append(
                f"{plugin_dir.name}: has no .claude-plugin/plugin.json")
            continue
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError as e:
            outliers.append(f"{plugin_dir.name}: plugin.json unparseable ({e})")
            continue
        if manifest.get("name") == BOOTSTRAP_PLUGIN_NAME:
            continue  # bootstrap itself: self-dependency is meaningless
        if not _declares_bootstrap(manifest):
            outliers.append(
                f"{plugin_dir.name}: plugin.json does not declare "
                '"bootstrap" in dependencies')
    return outliers


def main(argv: list[str]) -> int:
    if os.environ.get("PLUGINS_KIT_SKIP_BUMP_CHECK") == "1":
        return 0

    # --staged is the pre-commit gate. Worktree-wide and unscoped, this check
    # cross-contaminated the shared tree in the worst way available to it: one
    # session scaffolding plugins/<new>/ (plugin.json not written yet, or
    # written without the dependencies field) made EVERY commit by EVERY
    # session fail, on a plugin none of them were touching. A commit that
    # stages no plugin manifest cannot break the dependency edge; publish.py's
    # preflight runs the unscoped sweep before anything reaches master.
    where = "working-tree"
    if "--staged" in argv:
        repo_root = PLUGINS_DIR.parent
        verdict, staged = _gitindex.classify_scope(
            repo_root, is_derivation_input)
        if verdict == _gitindex.SCOPE_SKIP:
            return 0
        if verdict == _gitindex.SCOPE_WORKTREE:
            # Unavailable input must not read as a pass.
            print(
                "check_bootstrap_dependency: could not read the index; "
                "checking the working tree instead.",
                file=sys.stderr)
            outliers = find_outliers()
        else:
            where = "staged"
            outliers = find_outliers(
                repo_root=repo_root, from_index=True,
                only=staged_plugin_names(staged))
    else:
        outliers = find_outliers()

    if not outliers:
        return 0
    print(
        f"plugins do not declare the bootstrap dependency ({where} inputs):",
        file=sys.stderr)
    for line in outliers:
        print(f"  {line}", file=sys.stderr)
    print(
        '\nAdd "dependencies": ["bootstrap"] (bare string -- no marketplace '
        "field, no version)\nto each plugin's .claude-plugin/plugin.json and "
        "stage the result. A dependencies\nedit is a manifest change: bump "
        "the plugin version too (CLAUDE.md, 'Plugin\ndependencies on "
        "bootstrap').\n"
        "(Intentional dev commit? PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...)",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
