"""Tests for enable_plugin_at_scope -- the convergence repair.

`claude plugin install --scope X` short-circuits with "already installed"
whenever the registry records scope X, WITHOUT writing the enabledPlugins
entry that actually enables the plugin. When the registry and the settings
file disagree, the CLI can no longer repair it and bootstrap re-installs
every session while reporting success. These tests pin the direct write that
closes that loop, and the formatting discipline it owes a shared,
source-controlled settings.json.
"""

import json
import os

from bootstrap_lib import settings_writable
from bootstrap_lib.marketplace_lifecycle import enable_plugin_at_scope


def _settings(tmp_path, payload, newline="\n"):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline))
    return path


class TestEnablePluginAtScope:
    def test_adds_entry_to_project_settings(self, tmp_path):
        path = _settings(tmp_path, {"enabledPlugins": {"a@mkt": True}})

        result = enable_plugin_at_scope("mkt:b", "project", str(tmp_path))

        assert result.passed
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["enabledPlugins"] == {"a@mkt": True, "b@mkt": True}

    def test_creates_enabled_plugins_block_when_absent(self, tmp_path):
        path = _settings(tmp_path, {"permissions": {"allow": []}})

        assert enable_plugin_at_scope("mkt:b", "project", str(tmp_path)).passed

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["enabledPlugins"] == {"b@mkt": True}
        assert "permissions" in data  # unrelated settings survive

    def test_local_scope_targets_settings_local(self, tmp_path):
        (tmp_path / ".claude").mkdir(parents=True)

        assert enable_plugin_at_scope("mkt:b", "local", str(tmp_path)).passed

        target = tmp_path / ".claude" / "settings.local.json"
        assert json.loads(target.read_text(encoding="utf-8"))["enabledPlugins"] == {"b@mkt": True}

    def test_is_idempotent(self, tmp_path):
        path = _settings(tmp_path, {"enabledPlugins": {"b@mkt": True}})
        before = path.read_bytes()

        assert enable_plugin_at_scope("mkt:b", "project", str(tmp_path)).passed

        assert path.read_bytes() == before

    def test_preserves_crlf_line_endings(self, tmp_path):
        """The target is often a shared source-controlled file; churning every
        line turns a one-line change into an unmergeable whole-file diff."""
        path = _settings(tmp_path, {"enabledPlugins": {"a@mkt": True}}, newline="\r\n")

        assert enable_plugin_at_scope("mkt:b", "project", str(tmp_path)).passed

        data = path.read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n")

    def test_preserves_non_ascii_characters(self, tmp_path):
        """json.dump's ensure_ascii default would escape these to \\uXXXX,
        rewriting settings the caller never touched."""
        path = _settings(tmp_path, {"note": "an em dash — here", "enabledPlugins": {}})

        assert enable_plugin_at_scope("mkt:b", "project", str(tmp_path)).passed

        assert "—" in path.read_text(encoding="utf-8")

    def test_unresolvable_scope_fails_cleanly(self, tmp_path):
        result = enable_plugin_at_scope("mkt:b", "project", None)
        assert not result.passed
        assert "settings file" in result.message

    def test_unwritable_target_reports_failure(self, tmp_path, monkeypatch):
        _settings(tmp_path, {"enabledPlugins": {}})
        monkeypatch.setattr(
            settings_writable, "ensure_writable",
            lambda path: settings_writable.WritableResult(False, "failed", "read-only"),
        )

        result = enable_plugin_at_scope("mkt:b", "project", str(tmp_path))

        assert not result.passed
        assert "not writable" in result.message

    def test_corrupt_settings_file_fails_cleanly(self, tmp_path):
        path = tmp_path / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")

        result = enable_plugin_at_scope("mkt:b", "project", str(tmp_path))

        assert not result.passed
        assert path.read_text(encoding="utf-8") == "{ not json"  # left untouched
