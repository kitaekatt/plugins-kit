#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Plugin cache status checker — universal across all marketplaces.

Reads installed_plugins.json to discover all installed plugins and their
cache paths, then compares against local development directories.

Compares two locations per plugin:
  1. Source:       Local dev directory (plugins-kit auto-derived from this
                   script's repo; other marketplaces via --source NAME=PATH)
  2. Plugin cache: ~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/

Scope vs scripts/plugin-versions.sh: that script compares VERSION STRINGS
across local/marketplace/installed/cache; this one diffs FILE CONTENT between
the local source tree and the cache. A content diff while on the dev branch is
expected (the cache tracks published master), so the summary says "differs",
not "broken".

Usage:
    python scripts/cache_status.py                    # Summary (in sync / differs)
    python scripts/cache_status.py unreal-kit          # Specific plugin
    python scripts/cache_status.py --marketplace plugins-kit  # Filter by marketplace
    python scripts/cache_status.py --detailed          # Per-plugin file-level diffs
    python scripts/cache_status.py --json              # Machine-readable output
    python scripts/cache_status.py --source other-mkt=~/Dev/other-mkt
"""

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ==============================================================================
# Configuration — local dev directories per marketplace
# ==============================================================================
# plugins-kit's source tree is derived from this script's own location (it
# lives in <repo>/scripts/), so the mapping works on every machine. Other
# marketplaces are not hardcoded; map them per-invocation with
# --source NAME=PATH.

_REPO_ROOT = Path(__file__).resolve().parents[1]

MARKETPLACE_SOURCE_DIRS: dict[str, Path] = {
    "plugins-kit": _REPO_ROOT,
}

CLAUDE_HOME = Path.home() / ".claude"
INSTALLED_PLUGINS_PATH = CLAUDE_HOME / "plugins" / "installed_plugins.json"

# Patterns to skip when comparing
SKIP_DIRS = {"__pycache__", ".venv", ".local-data", "node_modules", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_FILES = {"uv.lock"}


# ==============================================================================
# Data Classes
# ==============================================================================

@dataclass
class CacheComparison:
    """Comparison result between source and cache."""
    status: str  # "IN_SYNC", "OUT_OF_SYNC", "MISSING", "NO_SOURCE"
    orphaned: list[str] = field(default_factory=list)
    divergent: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass
class PluginStatus:
    """Full sync status for a single plugin."""
    name: str
    marketplace: str
    version: str
    install_path: str
    git_commit_sha: str
    comparison: CacheComparison


# ==============================================================================
# File Hashing
# ==============================================================================

def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_files(directory: Path) -> dict[str, str]:
    """Collect all files under directory as {relative_path: hash}."""
    files: dict[str, str] = {}
    if not directory.exists():
        return files

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(directory).parts
        if any(part in SKIP_DIRS for part in parts):
            continue
        if path.suffix in SKIP_SUFFIXES:
            continue
        if path.name in SKIP_FILES:
            continue
        rel = str(path.relative_to(directory))
        files[rel] = file_hash(path)

    return files


# ==============================================================================
# Comparison Logic
# ==============================================================================

def compare_directories(source_dir: Path, cache_dir: Path) -> CacheComparison:
    if not source_dir.exists():
        return CacheComparison(status="NO_SOURCE")
    if not cache_dir.exists():
        return CacheComparison(status="MISSING")

    source_files = collect_files(source_dir)
    cache_files = collect_files(cache_dir)

    orphaned = sorted(f for f in cache_files if f not in source_files)
    missing = sorted(f for f in source_files if f not in cache_files)
    divergent = sorted(
        f for f in source_files
        if f in cache_files and source_files[f] != cache_files[f]
    )

    if orphaned or divergent or missing:
        return CacheComparison(
            status="OUT_OF_SYNC",
            orphaned=orphaned,
            divergent=divergent,
            missing=missing,
        )
    return CacheComparison(status="IN_SYNC")


# ==============================================================================
# Plugin Discovery
# ==============================================================================

def load_installed_plugins() -> list[dict]:
    """Read installed_plugins.json and return flat list of plugin entries."""
    if not INSTALLED_PLUGINS_PATH.exists():
        return []
    with open(INSTALLED_PLUGINS_PATH, encoding="utf-8") as f:
        data = json.load(f)

    plugins = []
    for key, entries in data.get("plugins", {}).items():
        # key format: "plugin-name@marketplace-name"
        if "@" not in key:
            continue
        name, marketplace = key.split("@", 1)
        for entry in entries:
            plugins.append({
                "name": name,
                "marketplace": marketplace,
                "version": entry.get("version", "unknown"),
                "installPath": entry.get("installPath", ""),
                "gitCommitSha": entry.get("gitCommitSha", ""),
            })
    return plugins


def find_source_dir(marketplace: str, plugin_name: str) -> Optional[Path]:
    """Find the local source directory for a plugin."""
    marketplace_root = MARKETPLACE_SOURCE_DIRS.get(marketplace)
    if marketplace_root is None:
        return None
    source = marketplace_root / "plugins" / plugin_name
    if source.exists():
        return source
    return None


# ==============================================================================
# Status Checking
# ==============================================================================

def check_plugin(entry: dict) -> PluginStatus:
    name = entry["name"]
    marketplace = entry["marketplace"]
    install_path = entry["installPath"]

    source_dir = find_source_dir(marketplace, name)
    cache_dir = Path(install_path) if install_path else Path("/nonexistent")

    if source_dir is None:
        comparison = CacheComparison(status="NO_SOURCE")
    else:
        comparison = compare_directories(source_dir, cache_dir)

    return PluginStatus(
        name=name,
        marketplace=marketplace,
        version=entry["version"],
        install_path=install_path,
        git_commit_sha=entry["gitCommitSha"],
        comparison=comparison,
    )


# ==============================================================================
# Output Formatting
# ==============================================================================

def print_summary_output(statuses: list[PluginStatus]) -> None:
    stale = [s for s in statuses if s.comparison.status not in ("IN_SYNC", "NO_SOURCE")]
    no_source = [s for s in statuses if s.comparison.status == "NO_SOURCE"]

    if stale:
        print(f"Plugin cache: differs from local source ({len(stale)} plugin(s))")
        for s in stale:
            print(f"  - {s.name}@{s.marketplace} [{s.comparison.status}]")
        print("Note: OUT_OF_SYNC is EXPECTED when the local source tree (e.g. the dev")
        print("branch) is ahead of the latest published version -- the cache tracks the")
        print("published master, not the working copy. It signals a problem only when")
        print("you believe this exact source state has been published.")
    else:
        print("Plugin cache: matches local source")

    if no_source:
        print(f"No local source: {len(no_source)} plugin(s) (marketplace not in MARKETPLACE_SOURCE_DIRS)")
        for s in no_source:
            print(f"  - {s.name}@{s.marketplace}")


def print_detailed_output(statuses: list[PluginStatus]) -> None:
    for status in statuses:
        comp = status.comparison
        if comp.status in ("IN_SYNC", "NO_SOURCE"):
            continue

        print(f"Plugin: {status.name}@{status.marketplace} (v{status.version})")
        print(f"  Cache: {status.install_path}")

        source_dir = find_source_dir(status.marketplace, status.name)
        if source_dir:
            print(f"  Source: {source_dir}")

        print(f"  Status: {comp.status}")

        if comp.orphaned:
            print("  Orphaned (in cache, not in source):")
            for f in comp.orphaned:
                print(f"    - {f}")
        if comp.divergent:
            print("  Divergent (content differs):")
            for f in comp.divergent:
                print(f"    - {f}")
        if comp.missing:
            print("  Missing (in source, not in cache):")
            for f in comp.missing:
                print(f"    - {f}")

        print(f"  -> Fix: rm -rf {status.install_path} && restart Claude Code")
        print()

    total = len(statuses)
    out_of_sync = len([s for s in statuses if s.comparison.status == "OUT_OF_SYNC"])
    missing = len([s for s in statuses if s.comparison.status == "MISSING"])
    print(f"Summary: {total} plugins checked, {out_of_sync} out of sync, {missing} missing")

    if out_of_sync == 0 and missing == 0:
        print("All plugins are up to date.")


def print_json_output(statuses: list[PluginStatus]) -> None:
    result = []
    for s in statuses:
        result.append({
            "name": s.name,
            "marketplace": s.marketplace,
            "version": s.version,
            "install_path": s.install_path,
            "git_commit_sha": s.git_commit_sha,
            "status": s.comparison.status,
            "orphaned": s.comparison.orphaned,
            "divergent": s.comparison.divergent,
            "missing": s.comparison.missing,
        })
    print(json.dumps(result, indent=2))


# ==============================================================================
# Main
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check plugin cache sync status by comparing local source to plugin cache."
    )
    parser.add_argument(
        "plugin",
        nargs="?",
        help="Specific plugin name to check (default: all installed plugins)",
    )
    parser.add_argument(
        "--marketplace", "-m",
        help="Filter to a specific marketplace (e.g., plugins-kit)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in JSON format",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Show per-plugin file-level diffs instead of summary",
    )
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Map a marketplace name to a local source tree (repeatable). "
             "plugins-kit is mapped automatically to this script's repo.",
    )
    args = parser.parse_args()

    for mapping in args.source:
        name, sep, path = mapping.partition("=")
        if not sep or not name or not path:
            print(f"Error: --source expects NAME=PATH, got '{mapping}'", file=sys.stderr)
            sys.exit(2)
        MARKETPLACE_SOURCE_DIRS[name] = Path(path).expanduser()

    entries = load_installed_plugins()
    if not entries:
        print("Error: No installed plugins found", file=sys.stderr)
        sys.exit(1)

    # Filter by marketplace
    if args.marketplace:
        entries = [e for e in entries if e["marketplace"] == args.marketplace]
        if not entries:
            print(f"Error: No plugins found for marketplace '{args.marketplace}'", file=sys.stderr)
            sys.exit(1)

    # Filter by plugin name
    if args.plugin:
        entries = [e for e in entries if e["name"] == args.plugin]
        if not entries:
            print(f"Error: Plugin '{args.plugin}' not found in installed plugins", file=sys.stderr)
            sys.exit(1)

    statuses = [check_plugin(e) for e in entries]

    if args.json:
        print_json_output(statuses)
    elif args.detailed:
        print_detailed_output(statuses)
    else:
        print_summary_output(statuses)


if __name__ == "__main__":
    main()
