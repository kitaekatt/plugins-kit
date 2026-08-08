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

Escape hatch:  PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...   (or --no-verify)

Also importable: tests/repo-scripts/test_bootstrap_dependency.py asserts
against this exact rule rather than a second copy of it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"

BOOTSTRAP_PLUGIN_NAME = "bootstrap"


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


def find_outliers(plugins_dir: Path | None = None) -> list[str]:
    """Human-readable outlier lines; empty when the invariant holds.

    An outlier is any plugin dir (other than bootstrap itself) whose
    plugin.json is missing, unparseable, or lacks the bootstrap dependency.
    """
    root = PLUGINS_DIR if plugins_dir is None else plugins_dir
    outliers = []
    for plugin_dir in sorted(root.iterdir()):
        if not plugin_dir.is_dir():
            continue
        pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
        if not pj_path.is_file():
            outliers.append(
                f"{plugin_dir.name}: has no .claude-plugin/plugin.json")
            continue
        try:
            manifest = json.loads(pj_path.read_text(encoding="utf-8"))
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
    outliers = find_outliers()
    if not outliers:
        return 0
    print(
        "plugins do not declare the bootstrap dependency:", file=sys.stderr)
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
