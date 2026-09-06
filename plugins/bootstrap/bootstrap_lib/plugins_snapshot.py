#!/usr/bin/env python3
"""Snapshot of the installed/enabled plugin SET, for the mid-session install
relaunch (the third UserPromptSubmit trigger in bootstrap_lib/harvest.py,
alongside the version harvest and the import-crash retry).

Problem this solves: a plugin installed mid-session (`/plugin` +
/reload-plugins) loads its skills, but its venv is never provisioned -- the
SessionStart hook already ran and only re-fires on a fresh session, so every
command of the new plugin fails until a full restart. UserPromptSubmit DOES
re-fire, so harvest.py compares this snapshot against the stored stamp on every
prompt and relaunches session-bootstrap.sh when the plugin set changed.

The hash covers BOTH install signals (verified empirically 2026-07-20 by
uninstalling/reinstalling a plugin under a live session; macOS, registry v2
populated):

- installed_plugins.json "plugins" map -- rewritten on install/uninstall/update
  on machines where the registry is populated;
- settings "enabledPlugins" maps -- user settings plus active-project settings
  and project-local settings -- written on install/uninstall on ALL machines,
  including the registry-v2-EMPTY variant where installed_plugins.json stays
  {"plugins": {}} forever (the registry_v2_empty insight). Content-hashed
  rather than mtime-compared because settings mtime moves for unrelated reasons
  (statusline rewrite, model changes).

Seeding/ownership: the ENGINE writes the stamp at pass completion (engine.py,
next to engine_ran_version) -- a completed pass absorbs the state it just
provisioned, so bootstrap-authored registry writes never self-trigger a
relaunch, and the first pass after this version ships seeds the stamp.
harvest.py only READS the stamp; an empty stamp means "no pass has absorbed
state yet" and never triggers a launch.

Stdlib-only (runs on every prompt via the UserPromptSubmit hook).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

# Global stamp holding the hash a completed pass absorbed.
STATE_STAMP = "plugins_state_hash"
# Once-per-change launch dedup marker (mirrors harvest_launched_version):
# written before the launch attempt, cleared by the engine on completion.
LAUNCHED_STAMP = "plugins_relaunch_hash"


def _claude_home() -> str:
    """``~/.claude`` using the same HOME resolution as the rest of bootstrap
    (HOME preferred, ``~`` expansion as fallback)."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".claude")


def default_registry_path() -> str:
    return os.path.join(_claude_home(), "plugins", "installed_plugins.json")


def default_settings_path() -> str:
    return os.path.join(_claude_home(), "settings.json")


def _load_json_dict(path: str) -> dict:
    """Parse ``path`` as a JSON object; ``{}`` on any miss (absent, unreadable,
    malformed, non-object). Missing/broken inputs must hash STABLY, never raise."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def plugins_state_hash(
    registry_path: str = "", settings_path: str = "", project_dir: str = ""
) -> str:
    """Stable content hash of the installed/enabled plugin set.

    Canonical-JSON sha256 over the registry's "plugins" map plus every
    enabledPlugins map read by load_enabled_refs: user settings and, when a
    project is supplied, project settings and project settings.local.json. No
    field filtering: a version bump, scope change, enable/disable flip, or
    same-version reinstall (lastUpdated) all count as state changes -- each is
    a legitimate reason to re-run a provisioning pass, and a spurious pass is
    cheap (its checks all hit clean).
    """
    registry = _load_json_dict(registry_path or default_registry_path()).get("plugins", {})
    if not isinstance(registry, dict):
        registry = {}
    settings_paths = [settings_path or default_settings_path()]
    if project_dir:
        project_claude = os.path.join(project_dir, ".claude")
        settings_paths.extend([
            os.path.join(project_claude, "settings.json"),
            os.path.join(project_claude, "settings.local.json"),
        ])
    enabled = {}
    for path in settings_paths:
        if not os.path.isfile(path):
            continue
        source = _load_json_dict(path).get("enabledPlugins", {})
        if isinstance(source, dict):
            enabled.update(source)
    payload = json.dumps(
        {"plugins": registry, "enabledPlugins": enabled},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stamp_plugins_state(
    data_dir: str, registry_path: str = "", settings_path: str = "", project_dir: str = ""
) -> None:
    """Engine-side absorb at pass completion: stamp the CURRENT state hash and
    clear the launch-dedup marker. After this, the relaunch trigger stays quiet
    until the plugin set genuinely changes again."""
    from .stamps import global_stamp

    # engine.py supplies no project argument at this completion site. Its
    # session-bootstrap child runs with the active project as its cwd, so use
    # that cwd to keep the completion snapshot aligned with the relaunch read.
    active_project = project_dir or str(Path.cwd())
    global_stamp(data_dir, STATE_STAMP).write(
        plugins_state_hash(registry_path, settings_path, active_project)
    )
    global_stamp(data_dir, LAUNCHED_STAMP).clear()
