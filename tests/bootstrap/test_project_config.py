"""Tests for the project_config engine primitive."""

import os
import stat

import pytest

from bootstrap_lib.config_check import load_yaml_config, save_yaml_config
from bootstrap_lib.engine import _process_project_config


def _write_autodetect_script(plugin_root, script_name="custom_bootstrap.py", body=""):
    """Write an autodetect script to plugin_root."""
    path = os.path.join(plugin_root, script_name)
    with open(path, "w") as f:
        f.write(body)
    return path


class TestProcessProjectConfig:
    def test_creates_file_from_autodetect(self, tmp_path, monkeypatch):
        """Autodetect returns values -> project config file created + data-dir updated."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {"uproject": "/path/to/Game.uproject", "engine_dir": "/path/to/engine"}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        assert result is True
        # Project config file created
        project_config_path = os.path.join(str(project_dir), ".claude", "unreal-kit.yaml")
        assert os.path.isfile(project_config_path)
        project_data = load_yaml_config(project_config_path)
        assert project_data["uproject"] == "/path/to/Game.uproject"
        assert project_data["engine_dir"] == "/path/to/engine"

        # Data-dir config synced
        data_config = load_yaml_config(os.path.join(plugin_data_dir, "config.yaml"))
        assert data_config["uproject"] == "/path/to/Game.uproject"
        assert data_config["engine_dir"] == "/path/to/engine"

        # Action logged
        assert any("created" in e for e in action_entries)

    def test_reads_existing_file(self, tmp_path, monkeypatch):
        """Pre-existing project config is read and synced to data-dir, ok entry logged."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Pre-create project config
        project_config_dir = project_dir / ".claude"
        project_config_dir.mkdir()
        save_yaml_config(
            str(project_config_dir / "unreal-kit.yaml"),
            {"uproject": "/existing/Game.uproject", "engine_dir": "/existing/engine"},
        )

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
        }

        action_entries = []
        ok_entries = []
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        # Data-dir config synced
        data_config = load_yaml_config(os.path.join(plugin_data_dir, "config.yaml"))
        assert data_config["uproject"] == "/existing/Game.uproject"
        assert data_config["engine_dir"] == "/existing/engine"

        # Ok entry logged
        assert any("ok" in e for e in ok_entries)
        assert len(action_entries) == 0

    def test_autodetect_returns_none(self, tmp_path, monkeypatch):
        """Autodetect returning None -> no file created, no crash."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return None
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        assert result is False
        project_config_path = os.path.join(str(project_dir), ".claude", "unreal-kit.yaml")
        assert not os.path.exists(project_config_path)

    def test_autodetect_returns_empty_dict_still_counts_as_detected(
        self, tmp_path, monkeypatch
    ):
        """`{}` is a real (if empty) autodetect result, not "nothing detected" --
        only `None` means that (per the reference doc). `if detected:` conflates
        the two because an empty dict is falsy; this must return True, same as
        a non-empty dict would."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": [],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        assert result is True
        project_config_path = os.path.join(str(project_dir), ".claude", "unreal-kit.yaml")
        assert os.path.exists(project_config_path)

    def test_runs_every_session(self, tmp_path, monkeypatch):
        """Existing file -> values re-merged to data-dir (no stale data)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Pre-create project config with updated values
        project_config_dir = project_dir / ".claude"
        project_config_dir.mkdir()
        save_yaml_config(
            str(project_config_dir / "unreal-kit.yaml"),
            {"uproject": "/new/Game.uproject", "engine_dir": "/new/engine"},
        )

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        # Pre-create data-dir config with STALE values
        save_yaml_config(
            os.path.join(plugin_data_dir, "config.yaml"),
            {"uproject": "/old/Game.uproject", "engine_dir": "/old/engine"},
        )

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
        }

        action_entries = []
        ok_entries = []
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        # Data-dir config updated with new values
        data_config = load_yaml_config(os.path.join(plugin_data_dir, "config.yaml"))
        assert data_config["uproject"] == "/new/Game.uproject"
        assert data_config["engine_dir"] == "/new/engine"

    def test_partial_fields(self, tmp_path, monkeypatch):
        """Autodetect returns only some fields — rest handled by config phase fix-all."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {"uproject": "/path/to/Game.uproject"}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/unreal-kit.yaml",
            "required_fields": ["uproject", "engine_dir"],
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="test",
        )

        # Project config created with partial data
        project_config_path = os.path.join(str(project_dir), ".claude", "unreal-kit.yaml")
        assert os.path.isfile(project_config_path)
        project_data = load_yaml_config(project_config_path)
        assert project_data["uproject"] == "/path/to/Game.uproject"
        assert "engine_dir" not in project_data

        # Data-dir config has partial data
        data_config = load_yaml_config(os.path.join(plugin_data_dir, "config.yaml"))
        assert data_config["uproject"] == "/path/to/Game.uproject"


class TestProjectConfigDictFormRequiredFields:
    """Dict-form required_fields support: defaults + fix-all emission.

    ``project_config.required_fields`` mirrors ``config.required_fields``:
    a dict keyed by field name with ``{user_msg, agent_msg, default?}`` values.
    """

    def test_string_list_form_still_works(self, tmp_path, monkeypatch):
        """Backwards compat: flat list of field names still functions as before."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {"P4PORT": "ssl:perforce:1666", "P4USER": "alice"}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/p4-kit.yaml",
            "required_fields": ["P4PORT", "P4USER"],  # string-list form
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        failures = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="p4-kit",
            failures=failures,
        )

        assert result is True
        project_config_path = os.path.join(str(project_dir), ".claude", "p4-kit.yaml")
        project_data = load_yaml_config(project_config_path)
        assert project_data["P4PORT"] == "ssl:perforce:1666"
        assert project_data["P4USER"] == "alice"
        # String-list form produces no fix-all entries, defaults, or mismatches.
        assert failures == []

    def test_dict_form_applies_default_when_missing(self, tmp_path, monkeypatch):
        """Field has ``default`` and isn't in autodetect -> default written to project file."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {"P4PORT": "ssl:perforce:1666", "P4USER": "alice"}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/p4-kit.yaml",
            "required_fields": {
                "P4PORT": {"user_msg": "P4 server", "agent_msg": "Ask for P4PORT"},
                "P4USER": {"user_msg": "P4 user", "agent_msg": "Ask for P4USER"},
                "DEFAULT_AGENT": {
                    "user_msg": "Default review agent",
                    "agent_msg": "Ask user for DEFAULT_AGENT",
                    "default": "claude-opus",
                },
            },
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        failures = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="p4-kit",
            failures=failures,
        )

        assert result is True
        project_config_path = os.path.join(str(project_dir), ".claude", "p4-kit.yaml")
        project_data = load_yaml_config(project_config_path)
        assert project_data["P4PORT"] == "ssl:perforce:1666"
        assert project_data["P4USER"] == "alice"
        assert project_data["DEFAULT_AGENT"] == "claude-opus"
        # No fix-all: defaulted fields are satisfied.
        assert failures == []

        # Data-dir config synced with the default too.
        data_config = load_yaml_config(os.path.join(plugin_data_dir, "config.yaml"))
        assert data_config["DEFAULT_AGENT"] == "claude-opus"

    def test_dict_form_default_does_not_override_existing_value(self, tmp_path, monkeypatch):
        """Pre-existing field value must not be overwritten by a declared default."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Pre-create project config with DEFAULT_AGENT already set to a custom value
        project_config_dir = project_dir / ".claude"
        project_config_dir.mkdir()
        save_yaml_config(
            str(project_config_dir / "p4-kit.yaml"),
            {
                "P4PORT": "ssl:perforce:1666",
                "P4USER": "alice",
                "DEFAULT_AGENT": "custom-agent-name",
            },
        )

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/p4-kit.yaml",
            "required_fields": {
                "P4PORT": {"user_msg": "P4 server", "agent_msg": "Ask for P4PORT"},
                "P4USER": {"user_msg": "P4 user", "agent_msg": "Ask for P4USER"},
                "DEFAULT_AGENT": {
                    "user_msg": "Default review agent",
                    "agent_msg": "Ask user for DEFAULT_AGENT",
                    "default": "claude-opus",
                },
            },
        }

        action_entries = []
        ok_entries = []
        failures = []
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="p4-kit",
            failures=failures,
        )

        # Existing value preserved — default did NOT override.
        project_config_path = os.path.join(str(project_dir), ".claude", "p4-kit.yaml")
        project_data = load_yaml_config(project_config_path)
        assert project_data["DEFAULT_AGENT"] == "custom-agent-name"
        assert failures == []

    def test_dict_form_emits_fix_all_when_missing_and_no_default(self, tmp_path, monkeypatch):
        """Field missing from autodetect AND has no default -> fix-all entry emitted."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        # autodetect only fills P4PORT; P4USER missing and has no default.
        _write_autodetect_script(plugin_root, body="""\
def autodetect():
    return {"P4PORT": "ssl:perforce:1666"}
""")

        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/p4-kit.yaml",
            "required_fields": {
                "P4PORT": {"user_msg": "P4 server", "agent_msg": "Ask for P4PORT"},
                "P4USER": {
                    "user_msg": "Perforce username",
                    "agent_msg": "Ask the user for P4USER and write it to {config_path}",
                },
            },
            "autodetect": "custom_bootstrap.py autodetect",
        }

        action_entries = []
        ok_entries = []
        failures = []
        result = _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="p4-kit",
            failures=failures,
        )

        assert result is True  # File was created even if fields incomplete
        assert len(failures) == 1
        f = failures[0]
        assert f["type"] == "project_config"
        assert f["field"] == "P4USER"
        assert f["plugin"] == "p4-kit"
        assert f["user_msg"] == "Perforce username"
        # {config_path} placeholder should be expanded to the absolute project config path.
        # Normalize to handle Windows mixed separators (os.path.join vs. raw "/" in config_file).
        assert "p4-kit.yaml" in f["agent_msg"]
        assert str(project_dir) in f["agent_msg"] or os.path.normpath(str(project_dir)) in os.path.normpath(f["agent_msg"])
        assert "{config_path}" not in f["agent_msg"]

    def test_dict_form_default_applied_on_existing_file_with_missing_field(self, tmp_path, monkeypatch):
        """Pre-existing file missing a field with a default -> default applied and saved."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Existing config has P4PORT + P4USER but no DEFAULT_AGENT
        project_config_dir = project_dir / ".claude"
        project_config_dir.mkdir()
        save_yaml_config(
            str(project_config_dir / "p4-kit.yaml"),
            {"P4PORT": "ssl:perforce:1666", "P4USER": "alice"},
        )

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".claude/p4-kit.yaml",
            "required_fields": {
                "P4PORT": {"user_msg": "P4 server", "agent_msg": "Ask for P4PORT"},
                "P4USER": {"user_msg": "P4 user", "agent_msg": "Ask for P4USER"},
                "DEFAULT_AGENT": {
                    "user_msg": "Default review agent",
                    "agent_msg": "Ask user for DEFAULT_AGENT",
                    "default": "claude-opus",
                },
            },
        }

        action_entries = []
        ok_entries = []
        failures = []
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="p4-kit",
            failures=failures,
        )

        project_config_path = os.path.join(str(project_dir), ".claude", "p4-kit.yaml")
        project_data = load_yaml_config(project_config_path)
        assert project_data["DEFAULT_AGENT"] == "claude-opus"
        assert failures == []
        # Action must be logged (no silent bootstrap operations).
        assert any("defaults" in e and "DEFAULT_AGENT" in e for e in action_entries)


class TestLegacyFileMigration:
    """One-shot relocation: legacy_file -> file when only the legacy path exists.

    The engine moves the legacy file to the new path on session start so the rest
    of the project_config flow runs against the new location. Idempotent.
    """

    def _common_section(self):
        return {
            "file": ".local-data/myplugin/config.yaml",
            "legacy_file": ".claude/myplugin.yaml",
            "required_fields": ["foo"],
        }

    def test_migrates_legacy_file_to_new_path(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        # Pre-existing legacy file with content
        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "bar"})

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        ok_entries = []
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=ok_entries, plugin_name="myplugin",
        )

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        assert new_path.is_file(), "new path should exist after migration"
        assert not legacy_path.exists(), "legacy path should be gone after migration"
        assert load_yaml_config(str(new_path))["foo"] == "bar"
        # Action must be logged (no silent bootstrap operations).
        assert any("migrated" in e for e in action_entries)

    def test_migration_removes_empty_legacy_dir(self, tmp_path, monkeypatch):
        """Cleanup: the now-empty legacy directory is removed after migration."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".local-data" / "oldplace"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "config.yaml"
        save_yaml_config(str(legacy_path), {"foo": "bar"})

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".local-data/plugins-kit/myplugin/config.yaml",
            "legacy_file": ".local-data/oldplace/config.yaml",
            "required_fields": ["foo"],
        }
        _process_project_config(
            section, plugin_data_dir, plugin_root, [], ok_entries=[], plugin_name="myplugin",
        )

        new_path = project_dir / ".local-data" / "plugins-kit" / "myplugin" / "config.yaml"
        assert new_path.is_file()
        assert not legacy_path.exists()
        assert not legacy_dir.exists(), "empty legacy dir should be cleaned up"

    def test_migration_keeps_nonempty_legacy_dir(self, tmp_path, monkeypatch):
        """Cleanup is best-effort: a legacy dir with other files is left in place."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".local-data" / "oldplace"
        legacy_dir.mkdir(parents=True)
        legacy_path = legacy_dir / "config.yaml"
        save_yaml_config(str(legacy_path), {"foo": "bar"})
        sibling = legacy_dir / "other.flag"
        sibling.write_text("x", encoding="utf-8")

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        section = {
            "file": ".local-data/plugins-kit/myplugin/config.yaml",
            "legacy_file": ".local-data/oldplace/config.yaml",
            "required_fields": ["foo"],
        }
        _process_project_config(
            section, plugin_data_dir, plugin_root, [], ok_entries=[], plugin_name="myplugin",
        )

        assert not legacy_path.exists()
        assert legacy_dir.exists(), "non-empty legacy dir must be preserved"
        assert sibling.exists()

    def test_idempotent_when_only_new_path_exists(self, tmp_path, monkeypatch):
        """Second session: legacy file already gone, new file present -> no-op."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "bar"})

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )

        assert not any("migrated" in e for e in action_entries)

    def test_both_paths_legacy_older_deletes_legacy(self, tmp_path, monkeypatch):
        """Both paths exist and legacy mtime <= new mtime: drop the legacy file.

        Earlier engine versions wrote the new path from defaults without honoring
        legacy_file, leaving an orphaned legacy file alongside a fresh new one.
        We delete the legacy so future sessions don't keep flagging the conflict.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "from-legacy"})

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "from-new"})

        # Make legacy strictly older than new.
        legacy_stat = os.stat(legacy_path)
        os.utime(legacy_path, (legacy_stat.st_atime, legacy_stat.st_mtime - 60))

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )

        assert not legacy_path.exists(), "stale legacy should be removed"
        assert load_yaml_config(str(new_path))["foo"] == "from-new"
        assert any("removed stale legacy" in e for e in action_entries)

    def test_both_paths_legacy_same_age_deletes_legacy(self, tmp_path, monkeypatch):
        """Tie-break: same mtime is treated as legacy older — delete legacy."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "from-legacy"})

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "from-new"})

        # Force identical mtime on both files.
        new_stat = os.stat(new_path)
        os.utime(legacy_path, (new_stat.st_atime, new_stat.st_mtime))

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )

        assert not legacy_path.exists(), "same-age legacy should be removed"
        assert load_yaml_config(str(new_path))["foo"] == "from-new"
        assert any("removed stale legacy" in e for e in action_entries)

    def test_both_paths_legacy_newer_overwrites_new(self, tmp_path, monkeypatch):
        """Both paths exist and legacy mtime > new mtime: legacy wins, overwrites new.

        The user edited the legacy after the new file was auto-created; preserve
        their changes by promoting the legacy copy to the new path.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "from-legacy"})

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "from-new"})

        # Make legacy strictly newer than new.
        new_stat = os.stat(new_path)
        os.utime(legacy_path, (new_stat.st_atime, new_stat.st_mtime + 60))

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )

        assert not legacy_path.exists(), "legacy should be moved (not left as a duplicate)"
        assert load_yaml_config(str(new_path))["foo"] == "from-legacy"
        assert any("migrated" in e and "overwrote stale new path" in e for e in action_entries)

    def test_readonly_legacy_is_removed_when_stale(self, tmp_path, monkeypatch):
        """Stale legacy with read-only bit (P4-tracked file on Windows) still gets removed.

        os.remove raises PermissionError on a read-only file on Windows; the
        engine clears the read-only bit and retries.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "from-legacy"})

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "from-new"})

        # Legacy older than new, AND read-only.
        legacy_stat = os.stat(legacy_path)
        os.utime(legacy_path, (legacy_stat.st_atime, legacy_stat.st_mtime - 60))
        os.chmod(legacy_path, stat.S_IREAD)
        try:
            plugin_root = str(tmp_path / "plugin")
            os.makedirs(plugin_root)
            plugin_data_dir = str(tmp_path / "data")
            os.makedirs(plugin_data_dir)

            action_entries = []
            _process_project_config(
                self._common_section(), plugin_data_dir, plugin_root,
                action_entries, ok_entries=[], plugin_name="myplugin",
            )

            assert not legacy_path.exists(), "read-only legacy should still be removed"
            assert load_yaml_config(str(new_path))["foo"] == "from-new"
            assert any("removed stale legacy" in e for e in action_entries)
            assert not any("WARNING" in e for e in action_entries)
        finally:
            # If the test failed before remove, restore writable bit so cleanup works.
            if legacy_path.exists():
                os.chmod(legacy_path, stat.S_IWRITE)

    def test_warns_instead_of_crashing_on_unrecoverable_error(self, tmp_path, monkeypatch):
        """If reconciliation hits an OSError it can't recover from, log a warning
        and let bootstrap continue rather than dying mid-run.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        legacy_dir = project_dir / ".claude"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "myplugin.yaml"
        save_yaml_config(str(legacy_path), {"foo": "from-legacy"})

        new_path = project_dir / ".local-data" / "myplugin" / "config.yaml"
        new_path.parent.mkdir(parents=True)
        save_yaml_config(str(new_path), {"foo": "from-new"})

        # Make legacy older so the engine wants to remove it.
        legacy_stat = os.stat(legacy_path)
        os.utime(legacy_path, (legacy_stat.st_atime, legacy_stat.st_mtime - 60))

        # Force os.remove inside the engine to fail with a non-PermissionError.
        import bootstrap_lib.engine as engine_mod
        original = engine_mod.os.remove
        def explode(path):
            raise OSError("simulated unrecoverable failure")
        monkeypatch.setattr(engine_mod.os, "remove", explode)

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        # Must not raise — the warning path should swallow the error.
        _process_project_config(
            self._common_section(), plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )
        assert any("WARNING failed to reconcile" in e for e in action_entries)
        # Restore so the new-path-exists branch in downstream logic can proceed normally.
        monkeypatch.setattr(engine_mod.os, "remove", original)

    def test_no_op_when_legacy_file_field_omitted(self, tmp_path, monkeypatch):
        """Plugins without legacy_file declared: nothing migrates, normal flow runs."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)

        section = {
            "file": ".local-data/myplugin/config.yaml",
            "required_fields": ["foo"],
        }

        plugin_root = str(tmp_path / "plugin")
        os.makedirs(plugin_root)
        plugin_data_dir = str(tmp_path / "data")
        os.makedirs(plugin_data_dir)

        action_entries = []
        # Should not raise; just runs the no-file branch.
        _process_project_config(
            section, plugin_data_dir, plugin_root,
            action_entries, ok_entries=[], plugin_name="myplugin",
        )
        assert not any("migrated" in e for e in action_entries)
