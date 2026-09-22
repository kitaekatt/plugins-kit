"""Durable, comment-preserving UE ini updates."""

from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "lib"
import sys
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import ue_ini


def test_existing_key_preserves_comments_and_other_sections(tmp_path):
    ini = tmp_path / "DefaultEditor.ini"
    ini.write_text(
        "; header\n[Other]\nKeep=one\n\n[Section]\n; keep this\nKey = old ; inline\n\n[Last]\nStay=yes\n",
        encoding="utf-8",
    )
    ue_ini.write_ini_setting(ini, "[Section]", "Key", "new")
    assert ini.read_text(encoding="utf-8") == (
        "; header\n[Other]\nKeep=one\n\n[Section]\n; keep this\nKey = new ; inline\n\n[Last]\nStay=yes\n"
    )


def test_append_key_preserves_sections_and_comments(tmp_path):
    ini = tmp_path / "DefaultEditor.ini"
    ini.write_text("[Section]\n; comment\nExisting=yes\n[Other]\nKeep=yes\n", encoding="utf-8")
    ue_ini.write_ini_setting(ini, "[Section]", "Added", "value")
    assert ini.read_text(encoding="utf-8") == (
        "[Section]\n; comment\nExisting=yes\nAdded=value\n[Other]\nKeep=yes\n"
    )


def test_append_section_preserves_existing_bytes(tmp_path):
    ini = tmp_path / "DefaultEditor.ini"
    ini.write_text("; header\n[Other]\nKeep=yes", encoding="utf-8")
    ue_ini.write_ini_setting(ini, "[New]", "Key", "value")
    assert ini.read_text(encoding="utf-8") == "; header\n[Other]\nKeep=yes\n\n[New]\nKey=value\n"


def test_replace_failure_preserves_old_ini_and_cleans_candidate(tmp_path, monkeypatch):
    ini = tmp_path / "DefaultEditor.ini"
    ini.write_text("[Section]\nKey=old\n", encoding="utf-8")
    old = ini.read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(ue_ini.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        ue_ini.write_ini_setting(ini, "[Section]", "Key", "new")
    assert ini.read_bytes() == old
    assert not list(tmp_path.glob(".DefaultEditor.ini.*.tmp"))
