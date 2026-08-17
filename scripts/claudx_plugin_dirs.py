#!/usr/bin/env python3
"""Print the plugins-kit dev-tree directories a `claudx` session should load.

`claudx` launches Claude Code with one `--plugin-dir` per plugins-kit plugin, so
the session runs each plugin's skills/hooks/engine CODE from this working copy
instead of the cache. Passing every plugin in `plugins/` would also activate
plugins the machine does not actually have installed -- so this script emits only
the ones whose enablement says yes, resolved for the CURRENT directory (a project
that scopes its own `enabledPlugins` therefore gets exactly its set).

Enablement comes from `bootstrap_lib.plugin_resolve.load_enabled_refs`, the same
precedence walk the bootstrap engine uses, called with `include_registry=False`.
That flag matters: `installed_plugins.json` records what was INSTALLED and is not
pruned on uninstall, so unioning it in resurrects plugins the user removed. For a
"what should I LOAD" question, settings are authoritative.

Requires the bootstrap plugin (for bootstrap_lib), which every plugins-kit plugin
depends on anyway. Reads only; writes nothing.

Usage:
    python scripts/claudx_plugin_dirs.py [--project-dir DIR] [--all]

Output: one absolute plugin directory per line, on stdout. Diagnostics go to
stderr so the caller can consume stdout directly.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

sys.path.insert(0, str(PLUGINS_DIR / "bootstrap"))


def _marketplace_name() -> str:
    """This repo's marketplace name, read from its own manifest rather than assumed."""
    try:
        return json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))["name"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        sys.exit(f"error: cannot read marketplace name from {MARKETPLACE_JSON}: {exc}")


def _tree_plugins() -> list[tuple[str, Path]]:
    """(plugin name, directory) for every plugin in the working copy, name from its manifest."""
    found = []
    for entry in sorted(PLUGINS_DIR.iterdir()) if PLUGINS_DIR.is_dir() else []:
        manifest = entry / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            name = json.loads(manifest.read_text(encoding="utf-8"))["name"]
        except (OSError, json.JSONDecodeError, KeyError):
            print(f"warning: skipping {entry.name}: unreadable plugin.json", file=sys.stderr)
            continue
        found.append((name, entry))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", default=os.getcwd(),
                        help="Project whose scoped settings participate (default: cwd)")
    parser.add_argument("--all", action="store_true",
                        help="Skip the enablement filter and print every plugin in the tree")
    args = parser.parse_args()

    plugins = _tree_plugins()
    if not plugins:
        print(f"error: no plugins found under {PLUGINS_DIR}", file=sys.stderr)
        return 1

    if args.all:
        for _, directory in plugins:
            print(directory)
        return 0

    try:
        from bootstrap_lib.plugin_resolve import load_enabled_refs
    except ImportError as exc:
        print(f"error: cannot import bootstrap_lib ({exc}); is the bootstrap plugin present "
              f"at {PLUGINS_DIR / 'bootstrap'}?", file=sys.stderr)
        return 1

    refs = load_enabled_refs(project_dir=args.project_dir, include_registry=False)

    marketplace = _marketplace_name()
    if refs is None:
        # No settings file was readable at all. That is "cannot determine", NOT
        # "nothing enabled" -- fall back to the whole tree rather than hand back
        # an empty session, and say so.
        print("warning: no readable settings; loading every plugin in the tree", file=sys.stderr)
        selected = plugins
    else:
        selected = [(n, d) for n, d in plugins if f"{n}@{marketplace}" in refs]

    if not selected:
        print(f"error: no enabled {marketplace} plugins for {args.project_dir}", file=sys.stderr)
        return 1

    selected_names = {n for n, _ in selected}
    skipped = sorted(n for n, _ in plugins if n not in selected_names)
    if skipped:
        print(f"claudx: skipping not-enabled: {' '.join(skipped)}", file=sys.stderr)

    for _, directory in selected:
        print(directory)
    return 0


if __name__ == "__main__":
    sys.exit(main())
