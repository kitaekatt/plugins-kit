"""Tests for bootstrap engine/config.py."""

import json
import os
from copy import deepcopy

import bootstrap_lib.config as config_module
from config import CURRENT_SCHEMA_VERSION, load_config, migrate_config, save_config


class TestLoadConfig:
    def test_copies_defaults_on_first_load(self, data_dir, defaults_dir):
        config = load_config(data_dir, defaults_dir)
        # Config file should now exist in data_dir
        config_path = os.path.join(data_dir, "config.json")
        assert os.path.exists(config_path)
        assert config["schema_version"] == CURRENT_SCHEMA_VERSION

    def test_loads_existing_config(self, data_dir, defaults_dir):
        # Pre-create a current-version config
        config_path = os.path.join(data_dir, "config.json")
        existing = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "custom_field": "keep_me", "enabled_plugins": [], "log_level": "info",
            "log_success_shell": False, "log_success_checks": False,
            "self_setup": {"tools": [], "path_entries": [], "venv": {"check_imports": ["yaml"]}},
        }
        with open(config_path, "w") as f:
            json.dump(existing, f)

        config = load_config(data_dir, defaults_dir)
        assert config["custom_field"] == "keep_me"


class TestMigrateConfig:
    def test_migrates_v0_to_current(self, defaults_dir):
        v0 = {"some_setting": True}
        migrated = migrate_config(v0, defaults_dir=defaults_dir)
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "enabled_plugins" not in migrated
        assert "no_bootstrap" in migrated
        assert "bootstrap_cache" in migrated
        assert "log_success_shell" in migrated
        assert "log_success_checks" in migrated
        assert "self_setup" in migrated
        assert migrated["some_setting"] is True

    def test_migrates_v1_to_current(self, defaults_dir):
        v1 = {"schema_version": 1, "enabled_plugins": [], "log_level": "info"}
        migrated = migrate_config(v1, defaults_dir=defaults_dir)
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "enabled_plugins" not in migrated
        assert migrated["no_bootstrap"] == []
        assert migrated["bootstrap_cache"] == []
        assert migrated["log_success_shell"] is False
        assert migrated["log_success_checks"] is False
        assert "self_setup" in migrated

    def test_migrates_v2_to_current(self, defaults_dir):
        v2 = {"schema_version": 2, "enabled_plugins": [], "log_level": "info",
               "log_success_shell": True, "log_success_checks": True}
        migrated = migrate_config(v2, defaults_dir=defaults_dir)
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "enabled_plugins" not in migrated
        assert migrated["no_bootstrap"] == []
        assert migrated["bootstrap_cache"] == []
        assert migrated["log_success_shell"] is False
        assert migrated["log_success_checks"] is False
        assert "self_setup" in migrated

    def test_migrates_v3_to_current(self, defaults_dir):
        v3 = {"schema_version": 3, "enabled_plugins": ["kit:a"], "log_success_shell": False, "log_success_checks": False}
        migrated = migrate_config(v3, defaults_dir=defaults_dir)
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "enabled_plugins" not in migrated
        assert migrated["no_bootstrap"] == []
        assert migrated["bootstrap_cache"] == []
        assert "self_setup" in migrated

    def test_migrates_v4_to_current(self, defaults_dir):
        v4 = {
            "schema_version": 4,
            "no_bootstrap": ["bootstrap@plugins-kit"],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
        }
        migrated = migrate_config(v4, defaults_dir=defaults_dir)
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "self_setup" in migrated
        assert migrated["self_setup"]["tools"][0]["name"] == "uv"
        assert migrated["self_setup"]["tools"][1]["name"] == "git"
        assert "~/.local/bin" in migrated["self_setup"]["path_entries"]
        assert migrated["self_setup"]["venv"]["check_imports"] == ["yaml"]
        # bootstrap@plugins-kit should be cleaned from no_bootstrap
        assert "bootstrap@plugins-kit" not in migrated["no_bootstrap"]

    def test_v4_to_v5_uses_hardcoded_fallback_without_defaults_dir(self):
        v4 = {
            "schema_version": 4,
            "no_bootstrap": [],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
        }
        migrated = migrate_config(v4)  # No defaults_dir
        assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION
        assert "self_setup" in migrated
        assert migrated["self_setup"]["tools"][0]["name"] == "uv"

    def test_v5_to_v6_uses_fallback_when_defaults_omit_self_setup(self, tmp_path):
        defaults_dir = tmp_path / "defaults"
        defaults_dir.mkdir()
        (defaults_dir / "config.json").write_text(json.dumps({"schema_version": 6}))

        v5 = {"schema_version": 5, "self_setup": {}}

        migrated = migrate_config(v5, defaults_dir=str(defaults_dir))

        assert migrated["self_setup"]["python_stub_check"]["stub_markers"] == [
            "WindowsApps"
        ]

    def test_no_migration_on_current_version(self):
        current = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "enabled_plugins": [], "log_level": "info",
            "log_success_shell": False, "log_success_checks": False,
            "self_setup": {"tools": [], "path_entries": [], "venv": {"check_imports": ["yaml"]}},
        }
        result = migrate_config(current)
        assert result is current  # Same object -- no copy needed

    def test_migration_does_not_mutate_nested_input(self):
        original = {
            "schema_version": 4,
            "no_bootstrap": ["bootstrap@plugins-kit", "other@plugin"],
            "bootstrap_cache": [],
            "log_success_shell": False,
            "log_success_checks": False,
        }
        before = deepcopy(original)

        migrate_config(original)

        assert original == before


class TestSaveConfig:
    def test_save_config_roundtrip(self, data_dir):
        original = {"schema_version": 1, "enabled_plugins": ["test"], "log_level": "debug"}
        save_config(data_dir, original)

        config_path = os.path.join(data_dir, "config.json")
        with open(config_path) as f:
            loaded = json.load(f)
        assert loaded == original

    def test_save_config_uses_atomic_writer(self, data_dir, monkeypatch):
        calls = []

        def fake_write_atomic(path, content):
            calls.append((path, content))

        monkeypatch.setattr(config_module, "write_atomic", fake_write_atomic)

        save_config(data_dir, {"answer": 42})

        assert calls == [
            (
                os.path.join(data_dir, "config.json"),
                '{\n  "answer": 42\n}\n',
            )
        ]

    def test_atomic_failure_preserves_previous_bytes(self, data_dir, monkeypatch):
        config_path = os.path.join(data_dir, "config.json")
        original = b'{"answer": 1}\n'
        with open(config_path, "wb") as f:
            f.write(original)

        def fail_replace(_source, _destination):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(config_module.os, "replace", fail_replace)

        try:
            save_config(data_dir, {"answer": 2})
        except OSError as exc:
            assert "simulated replace failure" in str(exc)
        else:
            raise AssertionError("save_config should propagate atomic write failure")

        with open(config_path, "rb") as f:
            assert f.read() == original
