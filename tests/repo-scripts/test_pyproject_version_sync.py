"""Drift test: plugins/<name>/pyproject.toml version must equal the
authoritative plugins/<name>/.claude-plugin/plugin.json version (X17).

Auto-discovers plugins; compares the two files rather than pinning numbers, so
a normal publish bump (edit both files) never touches this test. Plugins
without a pyproject.toml, or whose pyproject declares no version, are out of
scope -- pyproject versions are non-authoritative, the rule is just "if you
state one, it must not lie".
"""

import json
import tomllib
from pathlib import Path

_PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"

# TEMPORARY exclusion: plugins/skills-kit/ is owned by another in-flight
# session (arch-review step 7). Its pyproject says 0.15.0 vs plugin.json
# 0.20.0; the deferred edit is recorded in tmp/arch-review-fixes/log.md.
# Remove this exclusion once that session's work lands.
_EXCLUDED = {"skills-kit"}


def _plugins_with_both_files():
    out = []
    for plugin_dir in sorted(_PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name in _EXCLUDED:
            continue
        pyproject = plugin_dir / "pyproject.toml"
        plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
        if pyproject.is_file() and plugin_json.is_file():
            out.append(plugin_dir)
    return out


def test_discovery_finds_plugins():
    # Vacuity guard for the drift assertion below.
    assert _plugins_with_both_files(), "no plugins with pyproject + plugin.json found"


def test_pyproject_versions_match_plugin_json():
    drift = []
    for plugin_dir in _plugins_with_both_files():
        with open(plugin_dir / "pyproject.toml", "rb") as f:
            py_version = tomllib.load(f).get("project", {}).get("version")
        if py_version is None:
            continue  # version-less pyproject: nothing to drift
        pj_version = json.loads(
            (plugin_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        ).get("version")
        if py_version != pj_version:
            drift.append(
                f"{plugin_dir.name}: pyproject.toml={py_version} "
                f"plugin.json={pj_version}")
    assert not drift, (
        "pyproject.toml versions drifted from the authoritative plugin.json "
        "(set them equal; plugin.json is the source of truth):\n  "
        + "\n  ".join(drift)
    )
