"""Tests for Bash login startup-file discovery."""

import os

import pytest

from bootstrap_lib import shell_rc


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.setattr(shell_rc.os, "name", "posix")
    return home


def test_bash_login_readable_precedence_and_profile_fallback(isolated_home):
    profile = isolated_home / ".profile"
    bash_login = isolated_home / ".bash_login"
    bash_profile = isolated_home / ".bash_profile"

    assert shell_rc.effective_bash_login_file(isolated_home) == profile
    profile.write_text("profile\n")
    assert shell_rc.effective_bash_login_file(isolated_home) == profile
    bash_login.write_text("bash login\n")
    assert shell_rc.effective_bash_login_file(isolated_home) == bash_login
    bash_profile.write_text("bash profile\n")
    assert shell_rc.effective_bash_login_file(isolated_home) == bash_profile


def test_unreadable_candidate_is_skipped(isolated_home, monkeypatch):
    bash_profile = isolated_home / ".bash_profile"
    bash_login = isolated_home / ".bash_login"
    bash_profile.write_text("unreadable\n")
    bash_login.write_text("readable\n")
    real_access = os.access

    def fake_access(path, mode):
        if os.fspath(path) == os.fspath(bash_profile):
            return False
        return real_access(path, mode)

    monkeypatch.setattr(shell_rc.os, "access", fake_access)

    assert shell_rc.effective_bash_login_file(isolated_home) == bash_login
