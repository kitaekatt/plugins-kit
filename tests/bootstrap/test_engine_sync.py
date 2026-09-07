"""Tests for sync_to_data engine feature."""

import json
import os

import pytest

from bootstrap_lib.engine import _process_manifest


class TestSyncToData:
    def test_sync_copies_directory(self, tmp_path):
        """Files from src are copied to dst in data_dir."""
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        # Create source lib directory with files
        src_lib = plugin_root / "lib"
        src_lib.mkdir()
        (src_lib / "bootstrap.py").write_text("# bootstrap module")
        (src_lib / "helper.py").write_text("# helper module")

        manifest = {"sync_to_data": [{"src": "lib", "dst": "lib"}]}
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries,
        )

        assert (data_dir / "lib" / "bootstrap.py").exists()
        assert (data_dir / "lib" / "helper.py").exists()
        assert (data_dir / "lib" / "bootstrap.py").read_text() == "# bootstrap module"
        assert failures == []
        assert any("sync" in e.lower() and "ok" in e for e in ok_entries)

    def test_sync_overwrites_existing(self, tmp_path):
        """Sync overwrites old content in dst."""
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        # Pre-populate dst with old content
        dst_lib = data_dir / "lib"
        dst_lib.mkdir()
        (dst_lib / "bootstrap.py").write_text("# old content")

        # Create source with new content
        src_lib = plugin_root / "lib"
        src_lib.mkdir()
        (src_lib / "bootstrap.py").write_text("# new content")

        manifest = {"sync_to_data": [{"src": "lib", "dst": "lib"}]}
        action_entries = []
        ok_entries = []
        _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries,
        )

        assert (dst_lib / "bootstrap.py").read_text() == "# new content"

    def test_sync_source_missing_fails(self, tmp_path):
        """Missing source directory produces a failure entry."""
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        manifest = {"sync_to_data": [{"src": "lib", "dst": "lib"}]}
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries,
        )

        assert not (data_dir / "lib").exists()
        assert len(failures) == 1
        assert failures[0]["type"] == "sync_to_data"
        assert any("FAILED" in e for e in action_entries)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="exec bits are a POSIX concept; os.access(X_OK) is unreliable on "
        "Windows (returns True for every file), so neither the positive nor the "
        "negative assertion is meaningful. The chmod the engine performs is a "
        "no-op on Windows anyway.",
    )
    def test_sync_grants_exec_bit_on_shell_scripts(self, tmp_path):
        """Synced *.sh files are executable even when the source is not."""
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        src_scripts = plugin_root / "scripts"
        src_scripts.mkdir()
        script = src_scripts / "statusline.sh"
        script.write_text("#!/usr/bin/env bash\necho ok\n")
        script.chmod(0o644)
        (src_scripts / "helper.py").write_text("# not a shell script")
        (src_scripts / "helper.py").chmod(0o644)

        manifest = {"sync_to_data": [{"src": "scripts", "dst": "scripts"}]}
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries,
        )

        assert failures == []
        assert os.access(data_dir / "scripts" / "statusline.sh", os.X_OK)
        assert not os.access(data_dir / "scripts" / "helper.py", os.X_OK)

    def test_sync_custom_dst(self, tmp_path):
        """Custom dst mapping places files at the correct location."""
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        src = plugin_root / "src" / "modules"
        src.mkdir(parents=True)
        (src / "mod.py").write_text("# module")

        manifest = {"sync_to_data": [{"src": "src/modules", "dst": "vendor/modules"}]}
        action_entries = []
        ok_entries = []
        failures = _process_manifest(
            manifest, "windows", str(data_dir), str(plugin_root),
            action_entries, ok_entries,
        )

        assert (data_dir / "vendor" / "modules" / "mod.py").exists()
        assert (data_dir / "vendor" / "modules" / "mod.py").read_text() == "# module"
        assert failures == []


class TestSyncToDataSurvivesAnOSErrorFromCopytree:
    """shutil.copytree / _ensure_shell_scripts_executable can raise OSError
    (read-only dst file, broken symlink) -- that must not propagate out of
    _process_manifest and abort every later phase in the same manifest.
    """

    def test_permission_error_is_a_failure_and_later_phases_still_run(
            self, tmp_path, monkeypatch):
        import shutil
        from bootstrap_lib import engine

        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()
        (plugin_root / "lib").mkdir()
        (plugin_root / "lib" / "mod.py").write_text("# module")

        ini_path = tmp_path / "DefaultEngine.ini"
        ini_path.write_text("[Section]\nKey=Value\n")

        monkeypatch.setattr(
            shutil, "copytree",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("read-only dst")),
        )

        manifest = {
            "sync_to_data": [{"src": "lib", "dst": "lib"}],
            "ini_settings": [
                {
                    "file": str(ini_path),
                    "section": "[Section]",
                    "settings": {"Key": "Value"},
                }
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="test",
            project_detected=True,
        )

        assert len(failures) == 1
        assert any("sync" in e.lower() and "FAILED" in e for e in action_entries)
        # The later ini_settings phase still ran (its setting already matched,
        # so it reports ok rather than an action -- proof the pass continued
        # instead of aborting on the sync_to_data OSError).
        assert any("ini Key: ok" in e for e in ok_entries)
