"""Tests for plugin_resolve.py -- plugin path resolution from registry."""

import json
import os

import pytest

from bootstrap_lib.plugin_resolve import (
    PluginInfo,
    _version_sort_key,
    list_enabled_plugins,
    load_enabled_refs,
    parse_plugin_ref,
)


class TestParsePluginRef:
    def test_colon_format(self):
        """Colon format: marketplace:plugin (used in bootstrap.json)."""
        marketplace, name = parse_plugin_ref("plugins-kit:bootstrap")
        assert marketplace == "plugins-kit"
        assert name == "bootstrap"

    def test_at_format(self):
        """At format: plugin@marketplace (used in installed_plugins.json)."""
        marketplace, name = parse_plugin_ref("bootstrap@plugins-kit")
        assert marketplace == "plugins-kit"
        assert name == "bootstrap"

    def test_no_separator(self):
        """No separator returns empty marketplace."""
        marketplace, name = parse_plugin_ref("standalone")
        assert marketplace == ""
        assert name == "standalone"

    def test_colon_takes_precedence(self):
        """If both : and @ are present, colon wins (unlikely but deterministic)."""
        marketplace, name = parse_plugin_ref("mk:plug@extra")
        assert marketplace == "mk"
        assert name == "plug@extra"


class TestListEnabledPlugins:
    def _make_plugin(self, tmp_path, ref, has_bootstrap=True):
        """Create a minimal plugin directory at tmp_path/<name>."""
        _, name = ref.split(":", 1)
        plugin_dir = tmp_path / name
        plugin_dir.mkdir()
        if has_bootstrap:
            (plugin_dir / "bootstrap.json").write_text("{}")
        return plugin_dir

    def _make_registry(self, tmp_path, plugins):
        """Write installed_plugins.json with {ref: [{installPath, version}]}."""
        registry = {
            "plugins": {
                ref: [{"installPath": f"./{name}", "version": "1.0.0"}]
                for ref, name in plugins
            }
        }
        reg_path = tmp_path / "installed_plugins.json"
        reg_path.write_text(json.dumps(registry))
        return str(reg_path)

    def test_no_bootstrap_skips_plugin(self, tmp_path):
        """Plugin in no_bootstrap is skipped without filesystem check."""
        self._make_plugin(tmp_path, "kit:a")
        reg_path = self._make_registry(tmp_path, [("kit:a", "a")])
        config = {"no_bootstrap": ["kit:a"], "bootstrap_cache": []}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert results == []
        assert not cache_changed

    def test_cached_plugin_with_bootstrap_json(self, tmp_path):
        """Plugin in bootstrap_cache with bootstrap.json present is included."""
        self._make_plugin(tmp_path, "kit:a")
        reg_path = self._make_registry(tmp_path, [("kit:a", "a")])
        config = {"no_bootstrap": [], "bootstrap_cache": ["kit:a"]}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert len(results) == 1
        assert results[0].name == "a"
        assert not cache_changed

    def test_cached_plugin_without_bootstrap_json(self, tmp_path):
        """Plugin in bootstrap_cache with missing bootstrap.json is excluded and removed from cache."""
        self._make_plugin(tmp_path, "kit:a", has_bootstrap=False)
        reg_path = self._make_registry(tmp_path, [("kit:a", "a")])
        config = {"no_bootstrap": [], "bootstrap_cache": ["kit:a"]}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert results == []
        assert cache_changed
        assert "kit:a" not in config["bootstrap_cache"]

    def test_uncached_plugin_with_bootstrap_json(self, tmp_path):
        """Plugin not in cache with bootstrap.json is included and added to cache."""
        self._make_plugin(tmp_path, "kit:a")
        reg_path = self._make_registry(tmp_path, [("kit:a", "a")])
        config = {"no_bootstrap": [], "bootstrap_cache": []}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert len(results) == 1
        assert results[0].name == "a"
        assert cache_changed
        assert "kit:a" in config["bootstrap_cache"]

    def test_uncached_plugin_without_bootstrap_json(self, tmp_path):
        """Plugin not in cache without bootstrap.json is excluded; cache unchanged."""
        self._make_plugin(tmp_path, "kit:a", has_bootstrap=False)
        reg_path = self._make_registry(tmp_path, [("kit:a", "a")])
        config = {"no_bootstrap": [], "bootstrap_cache": []}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert results == []
        assert not cache_changed
        assert config["bootstrap_cache"] == []

    def test_stale_cache_entry_purged(self, tmp_path):
        """Cache entry for plugin no longer in registry is purged."""
        reg_path = self._make_registry(tmp_path, [])  # empty registry
        config = {"no_bootstrap": [], "bootstrap_cache": ["kit:gone"]}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert results == []
        assert cache_changed
        assert config["bootstrap_cache"] == []

    def test_at_format_refs_parsed_correctly(self, tmp_path):
        """Plugin refs in @ format (from installed_plugins.json) are parsed correctly."""
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "bootstrap.json").write_text("{}")

        registry = {"plugins": {"my-plugin@my-marketplace": [{"installPath": f"./{plugin_dir.name}", "version": "1.0.0"}]}}
        reg_path = tmp_path / "installed_plugins.json"
        reg_path.write_text(json.dumps(registry))
        config = {"no_bootstrap": [], "bootstrap_cache": []}

        results, cache_changed = list_enabled_plugins(config, str(reg_path), str(tmp_path))

        assert len(results) == 1
        assert results[0].name == "my-plugin"
        assert results[0].marketplace == "my-marketplace"

    def test_empty_registry(self, tmp_path):
        """Empty registry returns empty results."""
        reg_path = self._make_registry(tmp_path, [])
        config = {"no_bootstrap": [], "bootstrap_cache": []}

        results, cache_changed = list_enabled_plugins(config, reg_path, str(tmp_path))

        assert results == []
        assert not cache_changed


class TestCacheFallbackDiscovery:
    """Registry-v2 fallback: Claude Code keeps installed_plugins.json at
    {"version": 2, "plugins": {}} for marketplace installs (observed live
    2026-07-16 after wiping ~/.claude/plugins: all plugins re-synced to the
    cache and ran, registry stayed empty, and the engine provisioned nothing
    but bootstrap itself). Discovery must fall back to the cache layout,
    filtered by enabledPlugins, with registry entries keeping precedence."""

    def _scaffold(self, tmp_path, plugins=None, registry_plugins=None):
        """Build <tmp>/plugins-root with a registry and a cache tree.

        plugins: {"name@mkt": ["1.0.0", ...]} version dirs to create in the
        cache (each gets a bootstrap.json inside).
        """
        root = tmp_path / "plugins-root"
        root.mkdir()
        reg_path = root / "installed_plugins.json"
        reg_path.write_text(
            json.dumps({"version": 2, "plugins": registry_plugins or {}}), encoding="utf-8"
        )
        for ref, versions in (plugins or {}).items():
            name, _, mkt = ref.partition("@")
            for v in versions:
                vdir = root / "cache" / mkt / name / v
                vdir.mkdir(parents=True)
                (vdir / "bootstrap.json").write_text("{}", encoding="utf-8")
        return root, str(reg_path)

    def test_empty_registry_falls_back_to_cache(self, tmp_path):
        root, reg = self._scaffold(tmp_path, plugins={"pluga@mkt": ["1.2.0"]})
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert [p.name for p in results] == ["pluga"]
        assert results[0].version == "1.2.0"
        assert results[0].marketplace == "mkt"
        assert results[0].install_path == os.path.normpath(
            str(root / "cache" / "mkt" / "pluga" / "1.2.0")
        )

    def test_registry_entry_takes_precedence(self, tmp_path):
        reg_install = tmp_path / "elsewhere" / "pluga"
        reg_install.mkdir(parents=True)
        (reg_install / "bootstrap.json").write_text("{}", encoding="utf-8")
        root, reg = self._scaffold(
            tmp_path,
            plugins={"pluga@mkt": ["9.9.9"]},
            registry_plugins={"pluga@mkt": [{"installPath": str(reg_install), "version": "1.0.0"}]},
        )
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert len(results) == 1
        assert results[0].version == "1.0.0"
        assert results[0].install_path == os.path.normpath(str(reg_install))

    def test_disabled_plugin_not_discovered(self, tmp_path):
        root, reg = self._scaffold(
            tmp_path, plugins={"pluga@mkt": ["1.0.0"], "plugb@mkt": ["1.0.0"]}
        )
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert [p.name for p in results] == ["pluga"]

    def test_none_fallback_refs_disables_fallback(self, tmp_path):
        root, reg = self._scaffold(tmp_path, plugins={"pluga@mkt": ["1.0.0"]})
        results, _ = list_enabled_plugins({}, reg, str(root), fallback_enabled_refs=None)
        assert results == []

    def test_highest_version_dir_wins(self, tmp_path):
        # Numeric compare, not string compare: 0.10.0 > 0.9.0, and a git SHA
        # like cache name is below a semver.
        root, reg = self._scaffold(
            tmp_path, plugins={"pluga@mkt": ["0.9.0", "0.10.0", "deadbeef9"]}
        )
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert results[0].version == "0.10.0"

    def test_four_component_semver_wins_numerically(self, tmp_path):
        root, reg = self._scaffold(
            tmp_path, plugins={"pluga@mkt": ["1.2.3.4", "1.2.3.5"]}
        )
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert results[0].version == "1.2.3.5"

    def test_cache_plugin_without_bootstrap_json_excluded(self, tmp_path):
        root, reg = self._scaffold(tmp_path, plugins={"pluga@mkt": ["1.0.0"]})
        os.remove(root / "cache" / "mkt" / "pluga" / "1.0.0" / "bootstrap.json")
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert results == []

    def test_fallback_refs_not_purged_from_bootstrap_cache(self, tmp_path):
        # A fallback-discovered ref must not be treated as uninstalled by the
        # stale-cache purge (it is absent from the registry by construction).
        root, reg = self._scaffold(tmp_path, plugins={"pluga@mkt": ["1.0.0"]})
        config = {"bootstrap_cache": ["pluga@mkt"]}
        results, cache_changed = list_enabled_plugins(
            config, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert [p.name for p in results] == ["pluga"]
        assert "pluga@mkt" in config["bootstrap_cache"]

    def test_missing_registry_file_still_discovers_cache(self, tmp_path):
        root, reg = self._scaffold(tmp_path, plugins={"pluga@mkt": ["1.0.0"]})
        os.remove(reg)
        results, _ = list_enabled_plugins(
            {}, reg, str(root), fallback_enabled_refs={"pluga@mkt"}
        )
        assert [p.name for p in results] == ["pluga"]


class TestPickRegistryRecord:
    """Deliberate record pick (claude-code#79892): never entries[0]."""

    def _pick(self, entry):
        from bootstrap_lib.plugin_resolve import pick_registry_record
        return pick_registry_record(entry)

    def test_dict_passthrough(self):
        rec = {"version": "1.0.0", "installPath": "/p"}
        assert self._pick(rec) is rec

    def test_prefers_record_without_projectpath(self):
        stale = {"version": "0.45.0", "installPath": "/old", "projectPath": "D:/dev/x"}
        healthy = {"version": "0.52.0", "installPath": "/new"}
        assert self._pick([stale, healthy]) is healthy

    def test_healthy_wins_even_when_older(self):
        stale = {"version": "9.0.0", "projectPath": "/proj"}
        healthy = {"version": "0.1.0"}
        assert self._pick([stale, healthy]) is healthy

    def test_newest_among_healthy(self):
        a = {"version": "0.9.0"}
        b = {"version": "0.10.0"}
        assert self._pick([a, b]) is b

    def test_junk_entries_ignored(self):
        healthy = {"version": "1.0.0"}
        assert self._pick(["junk", None, healthy]) is healthy

    def test_empty_and_wrong_shapes_return_none(self):
        assert self._pick([]) is None
        assert self._pick(None) is None
        assert self._pick("nope") is None



class TestVersionSortKey:
    def test_git_sha_like_name_is_below_semver(self):
        assert max(("1.2.0", "deadbeef9"), key=_version_sort_key) == "1.2.0"

    def test_any_number_of_numeric_components_is_semver(self):
        assert max(("1.2.3.4", "1.2.3.5"), key=_version_sort_key) == "1.2.3.5"


class TestLoadEnabledRefs:
    def test_user_level_settings_local_is_not_read(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"enabledPlugins": {}}), encoding="utf-8"
        )
        (claude / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": {"stale@mkt": True}}), encoding="utf-8"
        )

        assert load_enabled_refs(
            home=str(tmp_path), include_registry=False
        ) == set()

    def test_project_settings_and_local_are_read(self, tmp_path):
        claude = tmp_path / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"project@mkt": True}}), encoding="utf-8"
        )
        (claude / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": {"local@mkt": True}}), encoding="utf-8"
        )

        assert load_enabled_refs(
            project_dir=str(tmp_path), home=str(tmp_path), include_registry=False
        ) == {"project@mkt", "local@mkt"}
