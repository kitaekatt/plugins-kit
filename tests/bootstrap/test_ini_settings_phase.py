"""Tests for the ini_settings manifest phase (_phase_ini_settings), as
distinct from tests/bootstrap/test_ini_check.py which tests the
check_ini_setting/write_ini_setting primitives directly.
"""

import os

from bootstrap_lib.engine import _process_manifest


class TestIniSettingsFileExpandsHomeTilde:
    """`file` gets resolve_vars but historically no expanduser, while
    json_entries.target does -- so a manifest declaring `"file": "~/x.ini"`
    silently wrote/read `<cwd>/~/x.ini` instead of the user's home directory.
    """

    def test_tilde_prefixed_file_resolves_under_home(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))

        manifest = {
            "ini_settings": [
                {
                    "file": "~/settings.ini",
                    "section": "[Section]",
                    "settings": {"Key": "Value"},
                }
            ],
        }
        action_entries = []
        ok_entries = []

        _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
            project_detected=True,
        )

        assert (fake_home / "settings.ini").exists(), (
            "the ini file must be written under HOME, not under an unresolved "
            "'~' path relative to cwd"
        )
        assert not (tmp_path / "~").exists()
