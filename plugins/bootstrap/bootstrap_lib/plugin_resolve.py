"""Plugin path resolution from installed_plugins.json registry.

Registry v2 caveat: newer Claude Code versions keep installed_plugins.json at
{"version": 2, "plugins": {}} for marketplace installs -- enablement lives in
settings enabledPlugins and the code lives in the cache layout
(~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/). Discovery
therefore falls back to scanning that layout for enabled refs the registry
does not record; registry entries, when present, always take precedence.
"""

import json
import os
import re
from typing import List, NamedTuple, Optional


class PluginInfo(NamedTuple):
    name: str
    install_path: str  # Absolute path
    version: str
    marketplace: str = ""


def parse_plugin_ref(plugin_ref: str) -> tuple:
    """Parse a plugin ref into (marketplace, plugin_name).

    Supports two formats:
    - 'marketplace:plugin' (e.g. 'plugins-kit:bootstrap') — used in bootstrap.json
    - 'plugin@marketplace' (e.g. 'bootstrap@plugins-kit') — used in installed_plugins.json

    Returns ('', plugin_ref) if no separator found.
    """
    if ":" in plugin_ref:
        marketplace, plugin_name = plugin_ref.split(":", 1)
        return marketplace, plugin_name
    if "@" in plugin_ref:
        plugin_name, marketplace = plugin_ref.split("@", 1)
        return marketplace, plugin_name
    return "", plugin_ref


def resolve_plugin(registry_path: str, plugin_ref: str, base_dir: str) -> Optional[PluginInfo]:
    """Resolve a plugin reference to its install path.

    Args:
        registry_path: Path to installed_plugins.json
        plugin_ref: Plugin key (e.g. "plugins-kit:test-plugin")
        base_dir: Base directory for resolving relative paths (the plugins/ dir)

    Returns:
        PluginInfo if found, None otherwise
    """
    try:
        with open(registry_path, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    plugins = registry.get("plugins", {})
    entries = plugins.get(plugin_ref)
    if not entries or not isinstance(entries, list):
        return None

    entry = pick_registry_record(entries)
    if entry is None:
        return None
    install_path = entry.get("installPath", "")
    version = entry.get("version", "0.0.0")

    # Resolve relative paths against base_dir
    if install_path.startswith("./") or install_path.startswith("../"):
        install_path = os.path.normpath(os.path.join(base_dir, install_path))
    else:
        install_path = os.path.normpath(install_path)

    # Extract plugin name and marketplace from ref
    marketplace, name = parse_plugin_ref(plugin_ref)

    return PluginInfo(name=name, install_path=install_path, version=version, marketplace=marketplace)


def _version_sort_key(version: str):
    """Tolerant ordering key for cache version-dir names: numeric dot-parts
    compare numerically ("0.10.0" > "0.9.0"); non-numeric names (git-SHA cache
    keys) sort below any numeric version and tie-break lexically."""
    parts = re.findall(r"\d+", version)
    return (1 if parts else 0, tuple(int(p) for p in parts), version)


def pick_registry_record(entry):
    """The authoritative record for one installed_plugins.json ref.

    The registry can hold DUPLICATE records under one ref: Claude Code's
    trust/adoption flow writes a user-scope record that carries
    ``projectPath``, and a later ``--scope user`` install APPENDS a
    well-formed record instead of replacing it. A first-entry pick then
    reads the stale record forever -- the machine-wedge failure mode
    (2026-07-21 post-mortem; reported upstream as claude-code#79892).

    Prefer records WITHOUT ``projectPath`` (the shape the updater actually
    maintains), newest version among those. Accepts the registry's dict and
    list shapes; returns None when nothing usable exists.
    """
    if isinstance(entry, dict):
        return entry
    if not isinstance(entry, list):
        return None
    records = [e for e in entry if isinstance(e, dict)]
    if not records:
        return None
    return max(
        records,
        key=lambda e: (
            0 if e.get("projectPath") else 1,
            _version_sort_key(str(e.get("version", "") or "")),
        ),
    )


def discover_cache_plugins(plugins_root: str, enabled_refs) -> dict:
    """Registry-shaped fallback discovery from the plugin cache layout.

    Scans <plugins_root>/cache/<marketplace>/<plugin>/<version>/ and
    synthesizes {"<name>@<mkt>": [{"installPath", "version"}]} entries for
    ENABLED refs, picking the highest version dir per plugin -- that dir is the
    code Claude Code actually loads. ``enabled_refs`` None or empty means
    enablement is unknowable (or nothing is enabled): return {} rather than
    provision blindly.
    """
    if not enabled_refs:
        return {}
    cache_root = os.path.join(plugins_root, "cache")
    out = {}
    try:
        marketplaces = sorted(os.listdir(cache_root))
    except OSError:
        return {}
    for mkt in marketplaces:
        mkt_dir = os.path.join(cache_root, mkt)
        if not os.path.isdir(mkt_dir):
            continue
        try:
            names = sorted(os.listdir(mkt_dir))
        except OSError:
            continue
        for name in names:
            ref = f"{name}@{mkt}"
            if ref not in enabled_refs:
                continue
            plugin_dir = os.path.join(mkt_dir, name)
            try:
                versions = [
                    d for d in os.listdir(plugin_dir)
                    if os.path.isdir(os.path.join(plugin_dir, d))
                ]
            except OSError:
                continue
            if not versions:
                continue
            version = max(versions, key=_version_sort_key)
            out[ref] = [{
                "installPath": os.path.join(plugin_dir, version),
                "version": version,
            }]
    return out


def list_enabled_plugins(config: dict, registry_path: str, base_dir: str, enabled_refs=None,
                         fallback_enabled_refs=None):
    """Auto-discover plugins that have bootstrap.json.

    Uses no_bootstrap for opt-out and bootstrap_cache to avoid repeated filesystem scans.

    Args:
        config: Bootstrap config dict (with "no_bootstrap" and "bootstrap_cache" lists)
        registry_path: Path to installed_plugins.json
        base_dir: Base directory for resolving relative paths
        enabled_refs: Optional set of normalized plugin refs (plugin@marketplace) to include.
            If None, all registry plugins are considered (production layout behavior).
            If provided, plugins not in the set are skipped (dev layout filter).
        fallback_enabled_refs: Optional set of normalized refs whose install may be
            discovered from the cache layout when the registry has no entry for them
            (Claude Code registry v2 records marketplace installs as an empty
            "plugins" map). Registry entries always take precedence. None disables
            the fallback.

    Returns:
        Tuple of (List[PluginInfo], cache_changed: bool)
    """
    try:
        with open(registry_path, "r") as f:
            registry = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        registry = {}

    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        plugins = {}

    # Registry-v2 fallback: synthesize entries from the cache for enabled
    # plugins the registry doesn't record. Merged BEFORE the purge below so
    # fallback-discovered refs are never treated as uninstalled.
    fallback = discover_cache_plugins(
        os.path.dirname(os.path.abspath(registry_path)), fallback_enabled_refs,
    )
    if fallback:
        plugins = dict(plugins)
        for ref, entries in fallback.items():
            if not plugins.get(ref):
                plugins[ref] = entries
    no_bootstrap = config.get("no_bootstrap", [])
    bootstrap_cache = config.setdefault("bootstrap_cache", [])

    cache_changed = False
    results = []

    # Purge stale cache entries for plugins no longer in the registry (uninstalled)
    current_refs = set(plugins.keys())
    stale = [ref for ref in bootstrap_cache if ref not in current_refs]
    for ref in stale:
        bootstrap_cache.remove(ref)
        cache_changed = True

    for ref, entries in plugins.items():
        if not entries or not isinstance(entries, list):
            continue

        # Skip plugins opted out of bootstrapping
        if ref in no_bootstrap:
            continue

        # Apply enabled_refs filter (dev layout only — skips plugins not enabled in Claude Code)
        marketplace, name = parse_plugin_ref(ref)
        if enabled_refs is not None:
            normalized = f"{name}@{marketplace}" if marketplace else name
            if normalized not in enabled_refs:
                continue

        # Resolve install path
        entry = pick_registry_record(entries)
        if entry is None:
            continue
        install_path = entry.get("installPath", "")
        version = entry.get("version", "0.0.0")
        if install_path.startswith("./") or install_path.startswith("../"):
            install_path = os.path.normpath(os.path.join(base_dir, install_path))
        else:
            install_path = os.path.normpath(install_path)
        plugin_info = PluginInfo(name=name, install_path=install_path, version=version, marketplace=marketplace)
        bootstrap_json = os.path.join(install_path, "bootstrap.json")

        if ref in bootstrap_cache:
            # Cached: verify bootstrap.json still exists (plugin may have been updated)
            if os.path.isfile(bootstrap_json):
                results.append(plugin_info)
            else:
                bootstrap_cache.remove(ref)
                cache_changed = True
        else:
            # Not cached: check filesystem
            if os.path.isfile(bootstrap_json):
                bootstrap_cache.append(ref)
                cache_changed = True
                results.append(plugin_info)

    return results, cache_changed
