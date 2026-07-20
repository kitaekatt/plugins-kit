"""Tests for bootstrap_lib/plugins_snapshot.py — the installed/enabled
plugin-set hash behind the mid-session install relaunch.

Pins the hash contract (stable across key order / missing inputs; changes on
install, uninstall, version bump, enable/disable) and the engine-side absorb
(stamp written, launch-dedup marker cleared).
"""

import json

from bootstrap_lib.plugins_snapshot import (
    LAUNCHED_STAMP,
    STATE_STAMP,
    plugins_state_hash,
    stamp_plugins_state,
)
from bootstrap_lib.stamps import global_stamp


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def _registry(tmp_path, plugins, name="installed_plugins.json"):
    return _write(tmp_path, name, {"version": 2, "plugins": plugins})


def _settings(tmp_path, enabled, name="settings.json"):
    return _write(tmp_path, name, {"enabledPlugins": enabled})


HUE = {"hue-kit@plugins-kit": [{"version": "0.5.1", "installPath": "/cache/hue-kit/0.5.1"}]}


class TestPluginsStateHash:
    def test_stable_across_key_order(self, tmp_path):
        reg_a = _write(tmp_path, "a.json", {"plugins": {"x@m": {"version": "1"}, "y@m": {"version": "2"}}})
        reg_b = _write(tmp_path, "b.json", {"plugins": {"y@m": {"version": "2"}, "x@m": {"version": "1"}}})
        st = _settings(tmp_path, {"x@m": True})
        assert plugins_state_hash(reg_a, st) == plugins_state_hash(reg_b, st)

    def test_install_changes_hash(self, tmp_path):
        st = _settings(tmp_path, {})
        before = plugins_state_hash(_registry(tmp_path, {}, "r1.json"), st)
        after = plugins_state_hash(_registry(tmp_path, HUE, "r2.json"), st)
        assert before != after

    def test_enable_flip_changes_hash(self, tmp_path):
        # The empty-registry v2 case: registry never changes, only enabledPlugins.
        reg = _registry(tmp_path, {})
        on = plugins_state_hash(reg, _settings(tmp_path, {"hue-kit@plugins-kit": True}, "s1.json"))
        off = plugins_state_hash(reg, _settings(tmp_path, {"hue-kit@plugins-kit": False}, "s2.json"))
        absent = plugins_state_hash(reg, _settings(tmp_path, {}, "s3.json"))
        assert len({on, off, absent}) == 3

    def test_version_bump_changes_hash(self, tmp_path):
        st = _settings(tmp_path, {})
        v1 = plugins_state_hash(_registry(tmp_path, HUE, "r1.json"), st)
        bumped = {"hue-kit@plugins-kit": [{"version": "0.6.0", "installPath": "/cache/hue-kit/0.6.0"}]}
        v2 = plugins_state_hash(_registry(tmp_path, bumped, "r2.json"), st)
        assert v1 != v2

    def test_unrelated_settings_keys_do_not_change_hash(self, tmp_path):
        # Content-hash rationale: settings.json churns (statusline, model);
        # only the enabledPlugins map may matter.
        reg = _registry(tmp_path, HUE)
        s1 = _write(tmp_path, "s1.json", {"enabledPlugins": {"x@m": True}, "model": "a"})
        s2 = _write(tmp_path, "s2.json", {"enabledPlugins": {"x@m": True}, "model": "b", "statusLine": {"command": "z"}})
        assert plugins_state_hash(reg, s1) == plugins_state_hash(reg, s2)

    def test_missing_and_malformed_inputs_hash_stably(self, tmp_path):
        missing_reg = str(tmp_path / "nope.json")
        missing_st = str(tmp_path / "nope2.json")
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        h1 = plugins_state_hash(missing_reg, missing_st)
        h2 = plugins_state_hash(str(bad), missing_st)
        h3 = plugins_state_hash(_write(tmp_path, "nonobj.json", ["list"]), missing_st)
        assert h1 == h2 == h3  # all degrade to the same empty-state hash


class TestStampPluginsState:
    def test_writes_state_and_clears_launch_marker(self, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        reg = _registry(tmp_path, HUE)
        st = _settings(tmp_path, {"hue-kit@plugins-kit": True})
        global_stamp(str(data_dir), LAUNCHED_STAMP).write("stale-hash")

        stamp_plugins_state(str(data_dir), reg, st)

        assert global_stamp(str(data_dir), STATE_STAMP).read() == plugins_state_hash(reg, st)
        assert not global_stamp(str(data_dir), LAUNCHED_STAMP).exists()
