"""Tests for plugin self-registration (bootstrap_lib/self_register.py).

Covers the module contract (idempotent, non-destructive, quiet in steady
state) and the load-bearing guarantee it rides on: a ``plugins[]`` entry with
``install: "manual"`` NEVER installs -- an uninstalled plugin stays
uninstalled, which is the one supported opt-out from self-registration.
"""

import json
from unittest.mock import patch

from bootstrap_lib.self_register import (
    declared_plugin_ids,
    ensure_self_registration,
)


def _read(path):
    with open(path) as f:
        return json.load(f)


class TestDeclaredPluginIds:
    def test_full_ref_yields_ref_and_name(self):
        refs, names = declared_plugin_ids(
            {"plugins": [{"ref": "plugins-kit:skills-kit"}]})
        assert refs == {"plugins-kit:skills-kit"}
        assert names == {"skills-kit"}

    def test_bare_ref_yields_name_only(self):
        refs, names = declared_plugin_ids({"plugins": [{"ref": "bootstrap"}]})
        assert refs == set()
        assert names == {"bootstrap"}

    def test_tolerates_junk(self):
        for manifest in (None, {}, {"plugins": None}, {"plugins": "x"},
                         {"plugins": ["str", {}, {"ref": ""}]}):
            refs, names = declared_plugin_ids(manifest)
            assert refs == set() and names == set()


class TestEnsureSelfRegistration:
    def test_creates_file_with_manual_entries(self, tmp_path):
        local = str(tmp_path / ".claude" / "bootstrap.local.json")
        actions, oks = ensure_self_registration(
            local, ["plugins-kit:hue-kit", "plugins-kit:git-kit"],
            set(), set())
        data = _read(local)
        assert data["plugins"] == [
            {"ref": "plugins-kit:git-kit", "install": "manual"},
            {"ref": "plugins-kit:hue-kit", "install": "manual"},
        ]
        assert len(actions) == 2
        assert all("install: manual" in a for a in actions)
        assert oks == []

    def test_steady_state_is_quiet_and_writes_nothing(self, tmp_path):
        local = str(tmp_path / "bootstrap.local.json")
        ensure_self_registration(local, ["plugins-kit:hue-kit"], set(), set())
        before = _read(local)
        # Next pass: the file is a merged layer, so its refs arrive declared.
        actions, oks = ensure_self_registration(
            local, ["plugins-kit:hue-kit"], {"plugins-kit:hue-kit"}, {"hue-kit"})
        assert actions == []
        assert oks and "have auto-update entries" in oks[0]
        assert _read(local) == before

    def test_no_duplicate_when_file_already_has_entry(self, tmp_path):
        # Belt over the merged-layer check: the file declares the ref but the
        # caller's declared sets (stale layers) do not.
        local = str(tmp_path / "bootstrap.local.json")
        local_data = {"plugins": [{"ref": "plugins-kit:hue-kit",
                                   "install": "manual"}]}
        with open(local, "w") as f:
            json.dump(local_data, f)
        actions, oks = ensure_self_registration(
            local, ["plugins-kit:hue-kit"], set(), set())
        assert actions == []
        assert oks == ["self-register: entries already present"]
        assert _read(local) == local_data

    def test_declared_elsewhere_is_skipped(self, tmp_path):
        local = str(tmp_path / "bootstrap.local.json")
        # skills-kit declared via awesome-kit's manifest; bootstrap declared
        # bare in its own manifest.
        actions, oks = ensure_self_registration(
            local,
            ["plugins-kit:skills-kit", "plugins-kit:bootstrap",
             "plugins-kit:hue-kit"],
            {"plugins-kit:skills-kit"}, {"skills-kit", "bootstrap"})
        data = _read(local)
        assert data["plugins"] == [
            {"ref": "plugins-kit:hue-kit", "install": "manual"}]
        assert len(actions) == 1

    def test_preserves_unrelated_keys_and_entries(self, tmp_path):
        local = str(tmp_path / "bootstrap.local.json")
        existing = {
            "$comment": "mine",
            "tools": [{"name": "jq"}],
            "plugins": [{"ref": "other-mkt:thing", "enabled": True,
                         "scope": "user"}],
        }
        with open(local, "w") as f:
            json.dump(existing, f)
        ensure_self_registration(local, ["plugins-kit:hue-kit"], set(), set())
        data = _read(local)
        assert data["$comment"] == "mine"
        assert data["tools"] == [{"name": "jq"}]
        assert data["plugins"][0] == existing["plugins"][0]
        assert data["plugins"][1] == {"ref": "plugins-kit:hue-kit",
                                      "install": "manual"}

    def test_unparseable_file_left_untouched(self, tmp_path):
        local = tmp_path / "bootstrap.local.json"
        local.write_text("{not json")
        actions, oks = ensure_self_registration(
            str(local), ["plugins-kit:hue-kit"], set(), set())
        assert local.read_text() == "{not json"
        assert actions and "left untouched" in actions[0]

    def test_non_object_file_left_untouched(self, tmp_path):
        local = tmp_path / "bootstrap.local.json"
        local.write_text("[1, 2]")
        actions, oks = ensure_self_registration(
            str(local), ["plugins-kit:hue-kit"], set(), set())
        assert local.read_text() == "[1, 2]"
        assert actions and "left untouched" in actions[0]

    def test_non_list_plugins_key_left_untouched(self, tmp_path):
        local = tmp_path / "bootstrap.local.json"
        local.write_text(json.dumps({"plugins": {"bad": "shape"}}))
        actions, oks = ensure_self_registration(
            str(local), ["plugins-kit:hue-kit"], set(), set())
        assert _read(str(local)) == {"plugins": {"bad": "shape"}}
        assert actions and "left untouched" in actions[0]


class TestManualEntryNeverInstalls:
    """The opt-out guarantee: a self-registered (install: manual) entry for a
    plugin the user uninstalled must not resurrect it."""

    def test_uninstalled_manual_plugin_is_not_installed(self, tmp_path):
        from bootstrap_lib.engine import _process_manifest
        from bootstrap_lib.marketplace_lifecycle import LifecycleResult

        manifest = {"plugins": [
            {"ref": "plugins-kit:hue-kit", "install": "manual"}]}
        action_entries, ok_entries = [], []
        with patch("bootstrap_lib.marketplace_lifecycle.check_plugin_installed",
                   return_value=LifecycleResult(False, "plugins-kit:hue-kit",
                                                "not installed")), \
             patch("bootstrap_lib.marketplace_lifecycle.install_plugin") as mock_install, \
             patch("bootstrap_lib.marketplace_lifecycle.update_plugin") as mock_update, \
             patch("bootstrap_lib.marketplace_lifecycle.enable_plugin_in_claude") as mock_enable:
            failures = _process_manifest(
                manifest, "macos", str(tmp_path / "data"),
                str(tmp_path / "root"), action_entries, ok_entries,
                plugin_name="test",
            )
        mock_install.assert_not_called()
        mock_update.assert_not_called()
        mock_enable.assert_not_called()
        assert failures == []
        # Quiet: verbose-only ok entry, no action noise.
        assert action_entries == []
        assert any("not installed (install: manual" in e for e in ok_entries)
