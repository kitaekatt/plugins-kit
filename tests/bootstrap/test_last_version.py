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

from bootstrap_lib.engine import _bootstrap_single_plugin, _plugin_log_label
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


class TestPluginLogLabel:
    """bootstrap is the one plugin whose per-plugin log section carries the
    REGISTRY version while its other sections carry the RUNNING binary's
    version. One pass could emit two "bootstrap@X" headers with different X and
    no way to tell them apart; when the two differ, the label names both."""

    def _info(self, name, version):
        return PluginInfo(name=name, install_path="/x", version=version,
                          marketplace="plugins-kit")

    def test_bootstrap_self_names_both_versions_on_mismatch(self, tmp_path):
        d = str(tmp_path / "data")
        assert _plugin_log_label(
            self._info("bootstrap", "0.63.0"), d, d, engine_version="0.61.0"
        ) == "bootstrap@0.63.0 (engine 0.61.0)"

    def test_bootstrap_self_stays_plain_when_versions_match(self, tmp_path):
        d = str(tmp_path / "data")
        assert _plugin_log_label(
            self._info("bootstrap", "0.63.0"), d, d, engine_version="0.63.0"
        ) == "bootstrap@0.63.0"

    def test_bootstrap_self_stays_plain_without_an_engine_version(self, tmp_path):
        d = str(tmp_path / "data")
        assert _plugin_log_label(self._info("bootstrap", "0.63.0"), d, d) == "bootstrap@0.63.0"

    def test_other_plugins_are_unchanged(self, tmp_path):
        engine_dir = str(tmp_path / "data")
        plugin_dir = str(tmp_path / "data" / "other-kit")
        assert _plugin_log_label(
            self._info("other-kit", "1.0.0"), plugin_dir, engine_dir, engine_version="0.61.0"
        ) == "other-kit@1.0.0"

    def test_versionless_plugin_is_just_the_name(self, tmp_path):
        d = str(tmp_path / "data")
        assert _plugin_log_label(self._info("bootstrap", ""), d, d, engine_version="0.61.0") == "bootstrap"


def _engine_root(tmp_path, version):
    """A minimal bootstrap plugin root: empty self_setup and an empty manifest,
    so _main reaches Step 2b and the end of the pass with no side effects."""
    root = tmp_path / "plugin_root"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "bootstrap", "version": version}), encoding="utf-8"
    )
    defaults = root / "defaults"
    defaults.mkdir()
    (defaults / "config.json").write_text(json.dumps({
        "schema_version": 5,
        "no_bootstrap": [],
        "bootstrap_cache": [],
        "log_success_shell": False,
        "log_success_checks": False,
        "self_setup": {},
        "notify_reload_needed": False,
    }), encoding="utf-8")
    (root / "bootstrap.json").write_text(json.dumps({}), encoding="utf-8")
    return str(root)


def _run_engine(tmp_path, monkeypatch, version, last_version=None, console=False):
    """Drive engine._main once; return (data_dir, bootstrap.log text)."""
    from bootstrap_lib import engine

    root = _engine_root(tmp_path, version)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if last_version is not None:
        (data_dir / "last_version").write_text(last_version, encoding="utf-8")
    iso_home = tmp_path / "home"
    iso_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(iso_home))
    monkeypatch.setenv("USERPROFILE", str(iso_home))
    argv = [
        "bootstrap_engine.py",
        "--plugin-root", root,
        "--data-dir", str(data_dir),
        "--verbose",
    ]
    argv.append("--console" if console else "--background")
    monkeypatch.setattr("sys.argv", argv)
    engine._main()
    log = data_dir / "bootstrap.log"
    return data_dir, (log.read_text(encoding="utf-8") if log.is_file() else "")


class TestConsoleDoesNotPoisonLastVersion:
    """--console returns before the engine_ran_version stamp, so a console run
    that advanced last_version would leave the two stamps inconsistent -- and
    the next cache-engine pass would report a transition that never happened."""

    def test_console_run_does_not_write_last_version(self, tmp_path, monkeypatch, capsys):
        data_dir, _ = _run_engine(tmp_path, monkeypatch, "0.63.0", console=True)
        capsys.readouterr()
        assert not (data_dir / "last_version").exists()

    def test_console_run_does_not_advance_an_existing_last_version(
        self, tmp_path, monkeypatch, capsys
    ):
        data_dir, _ = _run_engine(
            tmp_path, monkeypatch, "0.63.0", last_version="0.61.0", console=True
        )
        capsys.readouterr()
        assert (data_dir / "last_version").read_text(encoding="utf-8").strip() == "0.61.0"

    def test_background_run_still_writes_last_version(self, tmp_path, monkeypatch):
        data_dir, _ = _run_engine(tmp_path, monkeypatch, "0.63.0")
        assert (data_dir / "last_version").read_text(encoding="utf-8").strip() == "0.63.0"


class TestSelfTransitionLine:
    """Only a genuine UPGRADE is an action entry. The reverse direction -- an
    older binary running after a newer one (a dev tree, or an older resident
    session) -- used to print "updated: 0.62.0 -> 0.61.0", which reads as a
    downgrade that never happened."""

    def test_upgrade_logs_the_transition_as_an_action(self, tmp_path, monkeypatch):
        _, log = _run_engine(tmp_path, monkeypatch, "0.63.0", last_version="0.62.0")
        assert "updated: 0.62.0 -> 0.63.0" in log

    def test_older_engine_logs_an_ok_entry_not_a_transition(self, tmp_path, monkeypatch):
        _, log = _run_engine(tmp_path, monkeypatch, "0.61.0", last_version="0.62.0")
        assert "updated:" not in log
        assert "engine 0.61.0 ran" in log
        assert "0.62.0" in log

    def test_equal_versions_log_nothing(self, tmp_path, monkeypatch):
        _, log = _run_engine(tmp_path, monkeypatch, "0.62.0", last_version="0.62.0")
        assert "updated:" not in log and "installed:" not in log

    def test_first_ever_pass_logs_installed(self, tmp_path, monkeypatch):
        _, log = _run_engine(tmp_path, monkeypatch, "0.63.0")
        assert "installed: 0.63.0" in log

    def test_numeric_semver_not_string_compare(self, tmp_path, monkeypatch):
        # "0.9.0" > "0.62.0" as strings; numerically it is OLDER.
        _, log = _run_engine(tmp_path, monkeypatch, "0.9.0", last_version="0.62.0")
        assert "updated:" not in log
        assert "engine 0.9.0 ran" in log
