"""Tests for last_version handling (B14).

Step 2b writes <engine data_dir>/last_version with the RUNNING engine's
version; the Step-4 plugin loop writes <plugin_data_dir>/last_version with
the REGISTRY version. For bootstrap itself those are the same file, so when
a dev tree ran against a cached registry the two writers flip-flopped an
"updated: X -> Y" entry every pass. The plugin loop now skips version
detection when plugin_data_dir == engine data_dir.
"""

import argparse
import json
import os

from bootstrap_lib.engine import _bootstrap_single_plugin
from bootstrap_lib.plugin_resolve import PluginInfo


def _run_single(plugin_name, version, data_dir, install_path):
    """Drive _bootstrap_single_plugin with an empty manifest; return display sections."""
    display_sections = []
    args = argparse.Namespace(project_dir=None)
    _bootstrap_single_plugin(
        PluginInfo(name=plugin_name, install_path=install_path,
                   version=version, marketplace="plugins-kit"),
        "macos", data_dir, [], False, display_sections, [], args,
    )
    return display_sections


class TestLastVersionFlipFlop:
    def test_bootstrap_self_skips_version_detection(self, tmp_path):
        """When the plugin's data dir IS the engine data dir (bootstrap itself),
        the plugin loop must not write/compare last_version (Step 2b owns it)."""
        # Realistic layout: <root>/data/<marketplace>/bootstrap (the engine's own
        # data dir). bootstrap's own marketplace == the engine's, so its plugin
        # data dir resolves back to this same dir -> version detection is skipped.
        data_dir = tmp_path / "data" / "plugins-kit" / "bootstrap"
        data_dir.mkdir(parents=True)
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "bootstrap.json").write_text(json.dumps({}))

        # Step 2b already recorded the running engine's version.
        (data_dir / "last_version").write_text("0.15.2")

        sections = _run_single("bootstrap", "0.15.1", str(data_dir), str(install_path))

        # No flip-flop entry, and the Step-2b file is untouched.
        all_actions = [e for _, actions, _ in sections for e in actions]
        assert not any("updated:" in e or "installed:" in e for e in all_actions)
        assert (data_dir / "last_version").read_text() == "0.15.2"

    def test_other_plugin_still_detects_version_change(self, tmp_path):
        """Regular plugins (distinct data dir) keep their update detection."""
        engine_data_dir = tmp_path / "data" / "plugins-kit" / "bootstrap"
        engine_data_dir.mkdir(parents=True)
        # other-kit's own marketplace is plugins-kit (set in _run_single), so its
        # data dir lands under data/plugins-kit/, a sibling of the engine dir.
        plugin_data_dir = tmp_path / "data" / "plugins-kit" / "other-kit"
        plugin_data_dir.mkdir(parents=True)
        (plugin_data_dir / "last_version").write_text("1.0.0")
        install_path = tmp_path / "install"
        install_path.mkdir()
        (install_path / "bootstrap.json").write_text(json.dumps({}))

        sections = _run_single("other-kit", "1.1.0", str(engine_data_dir), str(install_path))

        all_actions = [e for _, actions, _ in sections for e in actions]
        assert any("updated: 1.0.0 -> 1.1.0" in e for e in all_actions)
        assert (plugin_data_dir / "last_version").read_text() == "1.1.0"
