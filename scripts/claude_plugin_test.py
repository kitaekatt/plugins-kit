#!/usr/bin/env python3
"""Launch a Claude Code session running plugins-kit from THIS working copy.

The problem this solves: `--plugin-dir` alone loads a plugin's skills, hooks and
engine CODE from disk, but the bootstrap engine reads each plugin's
`bootstrap.json` from its CACHED installPath -- so dev code runs against
PUBLISHED manifests, and a manifest change (a new tool, a download recipe, a new
venv import) is structurally invisible. `dev-tree.py` closes that gap by
rewriting the real `~/.claude/plugins/installed_plugins.json`, which is
machine-global: every other session, running or subsequent, sees the dev tree,
and a crash before the restore leaves the machine that way silently.

This launcher closes the gap WITHOUT touching anything another session reads.
Two mechanisms carry the whole isolation story:

1. DEV LAYOUT. The engine's `_find_plugins_dir` walks up from its own plugin
   root looking for `installed_plugins.json`. A synthetic one written into
   `<repo>/plugins/` is found ONLY by an engine running from this working copy
   -- i.e. only by a session that loaded bootstrap via `--plugin-dir`. The
   cached engine every other session runs walks up to the real registry and is
   unaffected. Nothing shared is written.

2. REDIRECTED DATA ROOT. `CLAUDE_BOOTSTRAP_DATA_ROOT` moves everything bootstrap
   owns -- venvs, `_shared_libs`, logs, stamps, cooldowns, config -- into a
   separate tree. The engine derives per-plugin dirs and the shared-lib root
   from the `--data-dir` it is handed (`engine._plugin_data_dir`), so the hooks
   honoring one variable redirects all of it with no engine change.

WHAT THIS DOES NOT TEST, by construction. Anything whose output IS the machine:
installing tools via a package manager, PATH/rc/registry writes, marketplace
clone refreshes, `claude plugin install/update`, env.json personalization, and
the version-bump -> cache -> auto-update delivery path. Those cannot be verified
without mutating shared state, which is the one thing this must not do. A green
run here means "my plugin works", never "my plugin ships correctly".

Usage:
    python scripts/claude_plugin_test.py [-- claude args...]
    python scripts/claude_plugin_test.py --fresh        # discard the dev data root first
    python scripts/claude_plugin_test.py --print        # show the command, launch nothing
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
SYNTHETIC_REGISTRY = PLUGINS_DIR / "installed_plugins.json"
DEFAULT_DATA_ROOT = Path(os.path.expanduser("~")) / ".claude" / "plugins" / "data-dev"

sys.path.insert(0, str(PLUGINS_DIR / "bootstrap"))


def _marketplace_name() -> str:
    try:
        return json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))["name"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        sys.exit(f"error: cannot read marketplace name from {MARKETPLACE_JSON}: {exc}")


def _tree_plugins() -> list[tuple[str, str, Path]]:
    """(name, version, dir) for every plugin in the working copy."""
    found = []
    for entry in sorted(PLUGINS_DIR.iterdir()) if PLUGINS_DIR.is_dir() else []:
        manifest = entry / ".claude-plugin" / "plugin.json"
        if not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            found.append((data["name"], data.get("version", "0.0.0"), entry))
        except (OSError, json.JSONDecodeError, KeyError):
            print(f"warning: skipping {entry.name}: unreadable plugin.json", file=sys.stderr)
    return found


def _selected(project_dir: str, marketplace: str, load_all: bool):
    plugins = _tree_plugins()
    if not plugins:
        sys.exit(f"error: no plugins found under {PLUGINS_DIR}")
    if load_all:
        return plugins, []

    try:
        from bootstrap_lib.plugin_resolve import load_enabled_refs
    except ImportError as exc:
        sys.exit(f"error: cannot import bootstrap_lib ({exc}); expected it at "
                 f"{PLUGINS_DIR / 'bootstrap'}")

    # include_registry=False: installed_plugins.json records what was INSTALLED
    # and is not pruned on uninstall, so unioning it in resurrects plugins the
    # user removed. For "what should I load", settings are authoritative.
    refs = load_enabled_refs(project_dir=project_dir, include_registry=False)
    if refs is None:
        # No readable settings at all. That is "cannot determine", NOT "nothing
        # enabled" -- fall back to the whole tree rather than hand back an empty
        # session, and say so.
        print("warning: no readable settings; loading every plugin in the tree", file=sys.stderr)
        return plugins, []

    keep = [p for p in plugins if f"{p[0]}@{marketplace}" in refs]
    skipped = sorted(p[0] for p in plugins if p not in keep)
    if not keep:
        sys.exit(f"error: no enabled {marketplace} plugins for {project_dir}")
    return keep, skipped


def _write_registry(selected, marketplace: str) -> None:
    """Write the synthetic dev-layout registry the dev engine will discover.

    Gitignored, and read only by an engine running from this working copy.
    """
    plugins = {
        f"{name}@{marketplace}": [{"installPath": str(directory), "version": version}]
        for name, version, directory in selected
    }
    SYNTHETIC_REGISTRY.write_text(
        json.dumps({"version": 2, "plugins": plugins}, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project-dir", default=os.getcwd(),
                        help="Project whose scoped settings decide enablement (default: cwd)")
    parser.add_argument("--data-root", default=str(DEFAULT_DATA_ROOT),
                        help=f"Bootstrap data root for the session (default: {DEFAULT_DATA_ROOT})")
    parser.add_argument("--all", action="store_true",
                        help="Load every plugin in the tree, ignoring enablement")
    parser.add_argument("--fresh", action="store_true",
                        help="Delete the dev data root first. A reused root accretes packages, "
                             "so a dependency you removed from pyproject.toml still resolves; "
                             "this is the periodic honest check.")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Print the command and exit without launching")
    parser.add_argument("claude_args", nargs="*",
                        help="Arguments passed through to claude (use -- to separate)")
    args = parser.parse_args()

    marketplace = _marketplace_name()
    selected, skipped = _selected(args.project_dir, marketplace, args.all)
    if skipped:
        print(f"not enabled, skipping: {' '.join(skipped)}", file=sys.stderr)

    data_root = Path(args.data_root)
    if args.fresh and data_root.exists():
        print(f"--fresh: removing {data_root}", file=sys.stderr)
        shutil.rmtree(data_root)
    data_root.mkdir(parents=True, exist_ok=True)

    _write_registry(selected, marketplace)

    cmd = ["claude"]
    for _, _, directory in selected:
        cmd += ["--plugin-dir", str(directory)]
    cmd += args.claude_args

    env = dict(os.environ)
    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(data_root)
    env["CLAUDE_PLUGIN_TEST"] = "1"

    print(f"plugins: {len(selected)} from {PLUGINS_DIR}", file=sys.stderr)
    print(f"data root: {data_root}", file=sys.stderr)
    if args.print_only:
        print(" ".join(cmd))
        return 0

    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError:
        sys.exit("error: `claude` not found on PATH")


if __name__ == "__main__":
    sys.exit(main())
