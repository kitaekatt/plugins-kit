"""Tests for plugins/bootstrap/lib/ini_check.py."""

import pytest

from bootstrap_lib.ini_check import check_ini_setting, write_ini_setting


class TestCheckIniSetting:
    def test_matching_value(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Section]\nkey=value\n")
        result = check_ini_setting(str(ini), "[Section]", "key", "value")
        assert result.passed is True

    def test_mismatched_value(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Section]\nkey=wrong\n")
        result = check_ini_setting(str(ini), "[Section]", "key", "value")
        assert result.passed is False
        assert "expected value" in result.message

    def test_key_not_found(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Section]\nother=value\n")
        result = check_ini_setting(str(ini), "[Section]", "key", "value")
        assert result.passed is False
        assert "not found" in result.message

    def test_file_not_found(self):
        result = check_ini_setting("/nonexistent/path.ini", "[Section]", "key", "value")
        assert result.passed is False
        assert "does not exist" in result.message

    def test_wrong_section(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Other]\nkey=value\n")
        result = check_ini_setting(str(ini), "[Section]", "key", "value")
        assert result.passed is False

    def test_multiple_sections(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[First]\nkey=wrong\n[Second]\nkey=right\n")
        result = check_ini_setting(str(ini), "[Second]", "key", "right")
        assert result.passed is True

    def test_utf16_file_returns_failed_result(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_bytes("[Section]\nkey=value\n".encode("utf-16"))

        result = check_ini_setting(str(ini), "[Section]", "key", "value")

        assert result.passed is False
        assert result.file == str(ini)


class TestWriteIniSetting:
    def test_create_file_and_section(self, tmp_path):
        ini = tmp_path / "new.ini"
        write_ini_setting(str(ini), "[Section]", "key", "value")
        assert ini.is_file()
        content = ini.read_text()
        assert "[Section]" in content
        assert "key=value" in content

    def test_create_parent_dirs(self, tmp_path):
        ini = tmp_path / "deep" / "path" / "config.ini"
        write_ini_setting(str(ini), "[Section]", "key", "value")
        assert ini.is_file()

    def test_update_existing_key(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Section]\nkey=old\n")
        write_ini_setting(str(ini), "[Section]", "key", "new")
        content = ini.read_text()
        assert "key=new" in content
        assert "key=old" not in content

    def test_add_key_to_existing_section(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[Section]\nexisting=yes\n")
        write_ini_setting(str(ini), "[Section]", "newkey", "newval")
        content = ini.read_text()
        assert "existing=yes" in content
        assert "newkey=newval" in content

    def test_add_new_section_to_existing_file(self, tmp_path):
        ini = tmp_path / "test.ini"
        ini.write_text("[First]\nfoo=bar\n")
        write_ini_setting(str(ini), "[Second]", "key", "value")
        content = ini.read_text()
        assert "[First]" in content
        assert "[Second]" in content
        assert "key=value" in content

    def test_long_ue_section_name(self, tmp_path):
        """UE uses long section names like [/Script/PythonScriptPlugin.Settings]."""
        ini = tmp_path / "test.ini"
        section = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]"
        write_ini_setting(str(ini), section, "bRemoteExecution", "True")
        result = check_ini_setting(str(ini), section, "bRemoteExecution", "True")
        assert result.passed is True
