#!/usr/bin/env python3
"""Switch plugins-kit between cache-tree and dev-tree mode.

In dev-tree mode, every plugins-kit plugin's installPath in
`~/.claude/plugins/installed_plugins.json` is rewritten to point at this repo's
working copy (`plugins/<name>`). Subsequent claude sessions then load the
plugins' skills, hooks, AND `bootstrap.json` content from disk -- so you can
test changes to manifest content (new tools, download recipes, venv imports)
without publishing.

This is the missing piece between `--plugin-dir` (which loads skills/hooks
from disk but leaves the engine reading bootstrap.json from cache) and a real
publish.

Both modes are idempotent and back the original up to a `.dev-tree-backup`
sidecar next to `installed_plugins.json`. Restore is always lossless.

Usage:
    python scripts/dev-tree.py dev       # switch to dev-tree mode
    python scripts/dev-tree.py normal    # restore from backup
    python scripts/dev-tree.py status    # report current mode
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
INSTALLED_JSON = HOME / ".claude" / "plugins" / "installed_plugins.json"
BACKUP_PATH = INSTALLED_JSON.with_suffix(".json.dev-tree-backup")
COOLDOWNS_DIR = HOME / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap" / "cooldowns"

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE_NAME = "plugins-kit"


def _load_installed() -> dict:
    if not INSTALLED_JSON.is_file():
        print(f"error: {INSTALLED_JSON} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(INSTALLED_JSON.read_text(encoding="utf-8"))


def _save_installed(data: dict) -> None:
    INSTALLED_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _dev_tree_path_for(plugin_name: str) -> Path | None:
    """Return the dev-tree installPath for a given plugin, or None if not on disk."""
    candidate = PLUGINS_DIR / plugin_name
    return candidate if candidate.is_dir() else None


def _clear_cooldowns() -> int:
    """Delete every project cooldown stamp so the next session bootstrap actually fires."""
    if not COOLDOWNS_DIR.is_dir():
        return 0
    n = 0
    for p in COOLDOWNS_DIR.glob("last_run_epoch.*"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _dev_plugin_version(plugin_name: str) -> str:
    """Read the version from a plugin's dev-tree plugin.json. Empty string on miss."""
    pj = PLUGINS_DIR / plugin_name / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return ""
    try:
        return json.loads(pj.read_text(encoding="utf-8")).get("version", "")
    except (json.JSONDecodeError, OSError):
        return ""


def cmd_dev() -> int:
    """Rewrite plugins-kit plugin installPaths to point at the dev tree.

    Collapses each plugins-kit plugin's entries down to a single canonical entry
    pointing at the dev tree, with the version taken from the dev plugin.json.
    Multi-entry registry state (e.g. residual historical cache versions) would
    otherwise make the engine process the same plugin twice.
    """
    print(f"switching to dev-tree mode (repo: {REPO_ROOT})")

    if not BACKUP_PATH.exists():
        shutil.copy2(INSTALLED_JSON, BACKUP_PATH)
        print(f"  backed up: {INSTALLED_JSON.name} -> {BACKUP_PATH.name}")
    else:
        print(f"  backup already exists at {BACKUP_PATH.name} (preserved)")

    data = _load_installed()
    plugins = data.get("plugins", {})
    rewritten = 0
    collapsed = 0
    skipped: list[str] = []

    for ref, entries in plugins.items():
        # ref is "<plugin>@<marketplace>"; filter to our marketplace.
        if "@" not in ref:
            continue
        plugin_name, marketplace = ref.split("@", 1)
        if marketplace != MARKETPLACE_NAME:
            continue
        dev_path = _dev_tree_path_for(plugin_name)
        if dev_path is None:
            skipped.append(f"{plugin_name} (no dev tree at {PLUGINS_DIR / plugin_name})")
            continue
        dev_version = _dev_plugin_version(plugin_name)
        target = str(dev_path).replace("/", os.sep)
        # Use the first entry as the template (preserves scope/installedAt etc.)
        # then overwrite the version + installPath. Drop any additional entries.
        if not entries:
            continue
        template = dict(entries[0])
        template["installPath"] = target
        if dev_version:
            template["version"] = dev_version
        if len(entries) > 1:
            collapsed += len(entries) - 1
        plugins[ref] = [template]
        rewritten += 1

    # Registry-v2 fallback: newer Claude Code keeps installed_plugins.json at
    # {"plugins": {}} for marketplace installs, so the rewrite loop above finds
    # nothing to repoint and dev-tree mode silently no-ops (this is how the
    # 0.47.0 publish shipped an EMPTY index.html -- generate.py crawled the
    # empty registry). Synthesize an entry for every repo plugin the registry
    # does not record, pointing at the dev tree. Restore stays lossless: the
    # backup taken above snapshots the (empty) original.
    synthesized = 0
    for plugin_dir in sorted(PLUGINS_DIR.iterdir()):
        if not plugin_dir.is_dir():
            continue
        name = plugin_dir.name
        ref = f"{name}@{MARKETPLACE_NAME}"
        if plugins.get(ref):
            continue  # real (or rewritten) entry takes precedence
        if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
            continue
        plugins[ref] = [{
            "installPath": str(plugin_dir).replace("/", os.sep),
            "version": _dev_plugin_version(name),
        }]
        synthesized += 1
    data["plugins"] = plugins

    _save_installed(data)
    cleared = _clear_cooldowns()

    print(f"  rewrote {rewritten} plugin entries (collapsed {collapsed} duplicate version entries)")
    if synthesized:
        print(f"  synthesized {synthesized} entries missing from the registry (registry v2)")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")
    print(f"  cleared {cleared} cooldown stamps")
    print()
    print("dev-tree mode ACTIVE.")
    print("  Next `claude` session loads plugin manifests + skills from this repo.")
    print("  Restore with:  python scripts/dev-tree.py normal")
    return 0


def cmd_normal() -> int:
    """Restore installed_plugins.json from backup."""
    print("switching to normal mode (cache-tree)")
    if not BACKUP_PATH.exists():
        print(f"  no backup at {BACKUP_PATH.name} -- already normal, or backup was removed manually.")
        return 1
    shutil.copy2(BACKUP_PATH, INSTALLED_JSON)
    BACKUP_PATH.unlink()
    cleared = _clear_cooldowns()
    print(f"  restored {INSTALLED_JSON.name} from backup")
    print(f"  removed backup")
    print(f"  cleared {cleared} cooldown stamps")
    print()
    print("normal mode ACTIVE.")
    return 0


def cmd_status() -> int:
    """Report whether dev-tree mode is active."""
    backup_present = BACKUP_PATH.exists()
    data = _load_installed()
    plugins = data.get("plugins", {})

    dev_count = 0
    cache_count = 0
    other_count = 0
    sample: list[str] = []
    dev_tree_str = str(PLUGINS_DIR).replace("/", os.sep)
    for ref, entries in plugins.items():
        if "@" not in ref:
            continue
        plugin_name, marketplace = ref.split("@", 1)
        if marketplace != MARKETPLACE_NAME:
            continue
        for entry in entries:
            ip = entry.get("installPath", "")
            if ip.startswith(dev_tree_str):
                dev_count += 1
                if len(sample) < 3:
                    sample.append(f"{plugin_name} -> dev")
            elif "cache" in ip:
                cache_count += 1
                if len(sample) < 3:
                    sample.append(f"{plugin_name} -> cache")
            else:
                other_count += 1

    mode = "DEV-TREE" if backup_present and dev_count > 0 else "NORMAL"
    print(f"plugins-kit mode: {mode}")
    print(f"  backup present     : {backup_present}")
    print(f"  installPaths @ dev : {dev_count}")
    print(f"  installPaths @ cache: {cache_count}")
    if other_count:
        print(f"  installPaths other : {other_count}")
    if sample:
        print(f"  sample             : {', '.join(sample)}")
    return 0


COMMANDS = {"dev": cmd_dev, "normal": cmd_normal, "status": cmd_status}


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return COMMANDS[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
