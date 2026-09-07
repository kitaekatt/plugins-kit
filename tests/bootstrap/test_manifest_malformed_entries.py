"""A malformed manifest entry must be a per-item failure, never a pass-ending
exception (see engine-internals.md: "a malformed manifest entry is a
per-item failure"). env_vars/fonts/git_config/plugins all already guard
shape; ini_settings, pypi_packages, and sync_to_data did not -- a missing
required key raised a bare KeyError out of _process_manifest, aborting every
later phase in the same manifest.
"""

from bootstrap_lib.engine import _process_manifest


class TestIniSettingsMalformedEntry:
    def test_missing_section_key_is_a_failure_not_an_exception(self, tmp_path):
        manifest = {
            "ini_settings": [
                {"file": str(tmp_path / "DefaultEngine.ini")},  # no "section"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=True,
        )

        assert len(failures) == 1
        assert any("ini" in e.lower() for e in action_entries)

    def test_missing_file_key_is_a_failure_not_an_exception(self, tmp_path):
        manifest = {
            "ini_settings": [
                {"section": "[Foo]", "settings": {"Key": "Val"}},  # no "file"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=True,
        )

        assert len(failures) == 1


class TestPypiPackagesMalformedEntry:
    def test_missing_extract_to_key_is_a_failure_not_an_exception(self, tmp_path):
        manifest = {
            "pypi_packages": [
                {"package": "somepkg"},  # no "extract_to"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
        )

        assert len(failures) == 1
        assert any("pypi" in e.lower() for e in action_entries)

    def test_missing_package_key_is_a_failure_not_an_exception(self, tmp_path):
        manifest = {
            "pypi_packages": [
                {"extract_to": str(tmp_path / "extracted")},  # no "package"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
        )

        assert len(failures) == 1


class TestSyncToDataMalformedEntry:
    def test_missing_dst_key_is_a_failure_not_an_exception(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()
        (plugin_root / "lib").mkdir()

        manifest = {
            "sync_to_data": [
                {"src": "lib"},  # no "dst"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="test",
        )

        assert len(failures) == 1
        assert any("sync" in e.lower() for e in action_entries)

    def test_missing_src_key_is_a_failure_not_an_exception(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        data_dir = tmp_path / "data"
        plugin_root.mkdir()
        data_dir.mkdir()

        manifest = {
            "sync_to_data": [
                {"dst": "lib"},  # no "src"
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(data_dir), str(plugin_root),
            action_entries, ok_entries, plugin_name="test",
        )

        assert len(failures) == 1
