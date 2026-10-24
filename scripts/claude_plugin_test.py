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

This launcher closes the gap without rewriting the machine-global registry.
Two mechanisms carry most of the isolation story:

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
the version-bump -> cache -> auto-update delivery path. A green run here means
"my plugin works", never "my plugin ships correctly".

THE CONTAINMENT IS PARTIAL. Standalone shared-library broadcasts are suppressed
under redirection; guarded consumers refuse a missing redirected interpreter.
Remaining marketplace, shell-write and exported-plugin-root escapes are described
in docs/reference/testing.md, section "claudx containment mechanics". That
reference distinguishes the reproduced missing-venv mechanism from the earlier
invocation whose exact import path was not captured.

PACKAGE-BACKED LIBRARIES. Plugin venvs install libraries from PyPI at the
targets in data/package-targets.json. Library source lives in katsoft-libs.

ENABLEMENT FILTERING. Without `--all`, only plugins ENABLED for `--project-dir`
are loaded; the rest are reported on stderr as "not enabled, skipping". Smoke-
testing a plugin that is disabled for that project yields a green run in which
it was never loaded.

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


# The two loop-guard variables of bootstrap_guard.reexec_under_plugin_venv. Both
# live in the launching process's environment when claudx is started from inside
# a plugin's re-exec'd process (or a test run), and an inherited value would let
# the session's guarded entry points skip the redirected-interpreter check.
GUARD_LOOP_FLAG_ENV = "_BOOTSTRAP_GUARD_VENV_REEXEC"
GUARD_SKIP_ENV = "_BOOTSTRAP_GUARD_SKIP_REEXEC"
# The loaded plugin directories, os.pathsep-joined absolute paths, in the order of
# the --plugin-dir arguments. A nested claude child (bootstrap_lib.claudx) rebuilds
# its own --plugin-dir pairs from it and refuses when it differs from the parent's.
PLUGIN_DIRS_ENV = "CLAUDE_PLUGIN_TEST_DIRS"


def _plugin_dirs_value(plugin_dirs) -> str:
    """The CLAUDE_PLUGIN_TEST_DIRS value for ``plugin_dirs``; raises on a bad entry."""
    dirs = [str(d) for d in plugin_dirs]
    if not dirs:
        raise ValueError(f"{PLUGIN_DIRS_ENV}: no plugin directories to export")
    for d in dirs:
        if not os.path.isabs(d):
            raise ValueError(f"{PLUGIN_DIRS_ENV}: not an absolute path: {d!r}")
        if os.pathsep in d:
            raise ValueError(f"{PLUGIN_DIRS_ENV}: path contains {os.pathsep!r}: {d!r}")
    return os.pathsep.join(dirs)


def _session_env(env: dict, data_root: Path, marketplace: str,
                 plugin_dirs=None, tree_plugin_names=None) -> dict:
    """The environment of the launched session, layered over ``env``.

    CLAUDE_PLUGIN_TEST_MARKETPLACE names the marketplace this session loads from
    the working copy. Bootstrap refuses a <PLUGIN>_ROOT inside that marketplace's
    plugin cache (CLAUDE_PLUGIN_TEST) and keeps one under any other marketplace.

    Inherited state that would name the PUBLISHED plugins or defeat the
    interpreter guard is removed first: the <PLUGIN>_ROOT of every plugin in the
    working copy (``tree_plugin_names``, default every plugin under plugins/),
    which the launching shell may carry from a normal session and which points at
    a cached release; and both guard loop variables. A <PLUGIN>_ROOT of a plugin
    this tree does not contain belongs to another marketplace and is kept.

    ``plugin_dirs``, the directories passed as --plugin-dir, are exported as
    CLAUDE_PLUGIN_TEST_DIRS. It is None only for a caller that loads no plugins.
    """
    from bootstrap_lib.env_var_check import plugin_root_env_var_name

    names = tree_plugin_names
    if names is None:
        names = [name for name, _version, _dir in _tree_plugins()]
    for var in [plugin_root_env_var_name(n) for n in names] + [GUARD_LOOP_FLAG_ENV, GUARD_SKIP_ENV]:
        env.pop(var, None)

    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = str(data_root)
    env["CLAUDE_PLUGIN_TEST"] = "1"
    env["CLAUDE_PLUGIN_TEST_MARKETPLACE"] = marketplace
    if plugin_dirs is None:
        env.pop(PLUGIN_DIRS_ENV, None)
    else:
        env[PLUGIN_DIRS_ENV] = _plugin_dirs_value(plugin_dirs)
    return env


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

    # --print is READ-ONLY, and must stay that way: it is what a caller uses to
    # confirm where a run would write BEFORE letting it write there. Returning
    # after the --fresh delete / mkdir / registry write made the preview itself
    # destructive, so the check could not be trusted to answer its own question.
    if args.print_only:
        cmd = ["claude"]
        for _, _, directory in selected:
            cmd += ["--plugin-dir", str(directory)]
        cmd += args.claude_args
        print(f"plugins: {len(selected)} from {PLUGINS_DIR}", file=sys.stderr)
        print(f"data root: {data_root}", file=sys.stderr)
        print(" ".join(cmd))
        return 0

    if args.fresh and data_root.exists():
        print(f"--fresh: removing {data_root}", file=sys.stderr)
        shutil.rmtree(data_root)
    data_root.mkdir(parents=True, exist_ok=True)

    _write_registry(selected, marketplace)

    cmd = ["claude"]
    for _, _, directory in selected:
        cmd += ["--plugin-dir", str(directory)]
    cmd += args.claude_args

    env = _session_env(dict(os.environ), data_root, marketplace,
                       plugin_dirs=[directory for _, _, directory in selected],
                       tree_plugin_names=[name for name, _, _ in _tree_plugins()])

    print(f"plugins: {len(selected)} from {PLUGINS_DIR}", file=sys.stderr)
    print(f"data root: {data_root}", file=sys.stderr)

    try:
        return subprocess.call(cmd, env=env)
    except FileNotFoundError:
        sys.exit("error: `claude` not found on PATH")


if __name__ == "__main__":
    sys.exit(main())
