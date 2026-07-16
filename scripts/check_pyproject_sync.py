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
import sys
import tomllib
from pathlib import Path

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugins"


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


def find_drift(plugins_dir: Path | None = None) -> list[str]:
    """Human-readable drift lines, empty when every stated version agrees."""
    drift = []
    for plugin_dir in plugins_with_both_files(plugins_dir):
        with open(plugin_dir / "pyproject.toml", "rb") as fh:
            py_version = tomllib.load(fh).get("project", {}).get("version")
        if py_version is None:
            continue  # version-less pyproject: nothing to drift
        pj_version = json.loads(
            (plugin_dir / ".claude-plugin" / "plugin.json").read_text(
                encoding="utf-8")
        ).get("version")
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
        "(Intentional dev commit? PLUGINS_KIT_SKIP_BUMP_CHECK=1 git commit ...)",
        file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
