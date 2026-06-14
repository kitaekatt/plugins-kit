"""Tests for per-plugin data-dir namespacing by the plugin's OWN marketplace.

Regression guard for the cross-marketplace collision: when a fork is installed
alongside upstream, both ship same-named plugins (bootstrap, p4-kit, ...). The
per-plugin data dir (and the _shared_libs sync target derived from it) must be
keyed by each plugin's own marketplace, not the running engine's, or the two
copies last-writer-win and poison each other's shared libs.
"""

import json
import os

from unittest.mock import patch

from bootstrap_lib.engine import _plugin_data_dir, _shared_lib_convergence_sweep
from bootstrap_lib.plugin_resolve import PluginInfo
from bootstrap_lib.shared_lib import SharedLibResult


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


class TestPluginDataDir:
    # Engine's own data dir: <root>/data/<engine-mkt>/bootstrap
    ENGINE_DATA = os.path.join("X", "data", "plugins-kit", "bootstrap")

    def test_keys_by_plugins_own_marketplace(self):
        """A foreign-marketplace plugin lands under ITS marketplace, not the engine's."""
        pi = PluginInfo("p4-kit", "/cache/x", "0.12.1", "plugins-kit-sf")
        got = _plugin_data_dir(self.ENGINE_DATA, pi)
        assert _norm(got) == _norm(os.path.join("X", "data", "plugins-kit-sf", "p4-kit"))

    def test_same_name_two_marketplaces_dont_collide(self):
        a = _plugin_data_dir(self.ENGINE_DATA, PluginInfo("p4-kit", "/a", "0.13.0", "plugins-kit"))
        b = _plugin_data_dir(self.ENGINE_DATA, PluginInfo("p4-kit", "/b", "0.12.1", "plugins-kit-sf"))
        assert _norm(a) != _norm(b)

    def test_falls_back_to_engine_marketplace_when_unset(self):
        """--plugin-dir installs have no marketplace; fall back to the engine's."""
        pi = PluginInfo("p4-kit", "/cache/x", "0.13.0", "")
        got = _plugin_data_dir(self.ENGINE_DATA, pi)
        assert _norm(got) == _norm(os.path.join("X", "data", "plugins-kit", "p4-kit"))

    def test_single_marketplace_is_unchanged_from_legacy(self):
        """When the plugin's marketplace == the engine's, the path matches the
        old `dirname(data_dir)/name` form -- a no-op for normal single-mkt users."""
        pi = PluginInfo("p4-kit", "/cache/x", "0.13.0", "plugins-kit")
        got = _plugin_data_dir(self.ENGINE_DATA, pi)
        legacy = os.path.join(os.path.dirname(self.ENGINE_DATA), pi.name)
        assert _norm(got) == _norm(legacy)


class TestConvergenceSweepNamespacing:
    def _plugin(self, tmp_path, name, marketplace, version):
        install = tmp_path / "cache" / marketplace / name / version
        install.mkdir(parents=True)
        (install / "bootstrap.json").write_text(json.dumps({"shared_lib_imports": ["bootstrap_lib"]}))
        return PluginInfo(name, str(install), version, marketplace)

    def test_same_named_consumers_link_into_own_marketplace_trees(self, tmp_path):
        """Two p4-kit installs (upstream + fork) must BOTH link, each into its own
        marketplace's _shared_libs -- not dedup to one, not share a tree."""
        engine_data = str(tmp_path / "data" / "plugins-kit" / "bootstrap")
        plugins = [
            self._plugin(tmp_path, "p4-kit", "plugins-kit", "0.13.0"),
            self._plugin(tmp_path, "p4-kit", "plugins-kit-sf", "0.12.1"),
        ]
        calls = []

        def fake_link(lib_name, python, shared_root):
            calls.append(shared_root)
            return SharedLibResult(lib_name, "cached", "already linked")

        with patch("bootstrap_lib.shared_lib.link_shared_lib", side_effect=fake_link):
            _shared_lib_convergence_sweep(plugins, engine_data)

        # Linked twice (not deduped by bare name) ...
        assert len(calls) == 2
        roots = {os.path.normcase(os.path.normpath(c)) for c in calls}
        # ... each into its own marketplace's _shared_libs.
        assert os.path.normcase(os.path.normpath(
            str(tmp_path / "data" / "plugins-kit" / "_shared_libs"))) in roots
        assert os.path.normcase(os.path.normpath(
            str(tmp_path / "data" / "plugins-kit-sf" / "_shared_libs"))) in roots
