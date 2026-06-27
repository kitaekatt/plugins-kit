"""Tests for the `requires_bootstrap` forward-compat guard.

A plugin's bootstrap.json may declare a minimum bootstrap-engine version
(e.g. it uses a `scoop:` fulfillment older engines can't process). When the
running engine is too old, the plugin's manifest is skipped with an
"update bootstrap" failure rather than misprocessed.
"""

import json
from types import SimpleNamespace

import bootstrap_lib.engine as engine


class TestVersionSatisfies:
    def test_equal_and_greater_satisfy(self):
        assert engine._version_satisfies("0.21.0", "0.21.0")
        assert engine._version_satisfies("0.22.0", "0.21.0")
        assert engine._version_satisfies("1.0.0", "0.21.0")

    def test_lower_does_not_satisfy(self):
        assert not engine._version_satisfies("0.20.0", "0.21.0")
        assert not engine._version_satisfies("0.20.99", "0.21.0")

    def test_tolerates_prefix_and_short_forms(self):
        assert engine._version_satisfies("0.21", ">=0.21.0")
        assert engine._version_satisfies("0.21.5", ">=0.21")
        assert not engine._version_satisfies("0.20", ">=0.21.0")


def _plugin(tmp_path, manifest, *, name="p4-kit", version="0.14.0"):
    (tmp_path / "bootstrap.json").write_text(json.dumps(manifest))
    return SimpleNamespace(install_path=str(tmp_path), name=name,
                           version=version, marketplace="plugins-kit")


def _data_dir(tmp_path):
    # _plugin_data_dir walks two dirs up from data_dir; this layout keeps the
    # derived per-plugin data dir inside tmp_path.
    return str(tmp_path / "plugins-kit" / "bootstrap")


class TestRequiresBootstrapGuard:
    def test_skips_and_reports_when_engine_too_old(self, tmp_path):
        pi = _plugin(tmp_path, {"requires_bootstrap": "0.21.0",
                                "tools": [{"name": "p4"}]})
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "windows", _data_dir(tmp_path), all_failures,
            False, display, deferred, SimpleNamespace(project_dir=None),
            engine_version="0.20.0",
        )
        outdated = [f for f in all_failures if f["type"] == "bootstrap_outdated"]
        assert len(outdated) == 1
        assert "0.21.0" in outdated[0]["agent_msg"]
        assert outdated[0]["plugin"] == "p4-kit"
        # non-auto-fixable: only the user can update bootstrap
        assert engine._is_auto_fixable(outdated[0]) is False
        # the skip note reached the display section
        assert display and "skipped: requires bootstrap >= 0.21.0" in display[0][1][0]

    def test_processes_when_engine_new_enough(self, tmp_path):
        # Minimal manifest (no tools) so nothing real runs once the guard opens.
        pi = _plugin(tmp_path, {"requires_bootstrap": "0.21.0"})
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "windows", _data_dir(tmp_path), all_failures,
            False, display, deferred, SimpleNamespace(project_dir=None),
            engine_version="0.21.0",
        )
        assert not any(f["type"] == "bootstrap_outdated" for f in all_failures)

    def test_no_requirement_is_unaffected(self, tmp_path):
        pi = _plugin(tmp_path, {"tools": []})
        all_failures, display, deferred = [], [], []
        engine._bootstrap_single_plugin(
            pi, "windows", _data_dir(tmp_path), all_failures,
            False, display, deferred, SimpleNamespace(project_dir=None),
            engine_version="0.1.0",
        )
        assert not any(f["type"] == "bootstrap_outdated" for f in all_failures)
