"""Unit tests for bootstrap_lib/apt.py (Ubuntu apt backend + privilege probes).

Mirrors the brew/scoop backends' shape: stdlib-only, subprocess-based,
Ubuntu/Linux-only. subprocess is mocked; no real apt/sudo/dpkg is invoked.
Load-bearing behaviors pinned here:
  * privilege detection -- root short-circuits, passwordless sudo detected via
    `sudo -n true`, missing sudo binary / nonzero exit -> unavailable;
  * apt_install NEVER attempts the install when elevation is missing (returns a
    needs-elevation outcome) and NEVER prompts;
  * the dpkg idempotency guard skips a present package without elevation.
"""

from unittest.mock import patch

import bootstrap_lib.apt as apt
from bootstrap_lib.apt import AptResult


def _linux():
    return patch.object(apt.sys, "platform", "linux")


class TestIsRoot:
    def test_euid_zero_is_root(self, monkeypatch):
        monkeypatch.setattr(apt.os, "geteuid", lambda: 0, raising=False)
        assert apt.is_root() is True

    def test_nonzero_euid_is_not_root(self, monkeypatch):
        monkeypatch.setattr(apt.os, "geteuid", lambda: 1000, raising=False)
        assert apt.is_root() is False

    def test_no_geteuid_is_not_root(self, monkeypatch):
        # Windows has no os.geteuid; is_root must not raise.
        monkeypatch.delattr(apt.os, "geteuid", raising=False)
        assert apt.is_root() is False


class TestSudoNoninteractiveAvailable:
    def test_root_short_circuits_without_probing_sudo(self):
        with patch.object(apt, "is_root", return_value=True), \
             patch("subprocess.run", side_effect=AssertionError("root must not probe sudo")):
            assert apt.sudo_noninteractive_available() is True

    def test_no_sudo_binary_is_unavailable(self):
        with patch.object(apt, "is_root", return_value=False), \
             patch.object(apt.shutil, "which", return_value=None):
            assert apt.sudo_noninteractive_available() is False

    def test_passwordless_sudo_available(self):
        class R:
            returncode = 0
        with patch.object(apt, "is_root", return_value=False), \
             patch.object(apt.shutil, "which", return_value="/usr/bin/sudo"), \
             patch("subprocess.run", return_value=R()):
            assert apt.sudo_noninteractive_available() is True

    def test_sudo_needs_password_is_unavailable(self):
        class R:
            returncode = 1
        with patch.object(apt, "is_root", return_value=False), \
             patch.object(apt.shutil, "which", return_value="/usr/bin/sudo"), \
             patch("subprocess.run", return_value=R()):
            assert apt.sudo_noninteractive_available() is False

    def test_timeout_is_unavailable(self):
        import subprocess
        with patch.object(apt, "is_root", return_value=False), \
             patch.object(apt.shutil, "which", return_value="/usr/bin/sudo"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sudo", 10)):
            assert apt.sudo_noninteractive_available() is False


class TestWindowsAdminAvailable:
    def test_non_windows_is_false(self):
        with patch.object(apt.sys, "platform", "linux"):
            assert apt.windows_admin_available() is False


class TestDpkgInstalled:
    def test_no_dpkg_query_binary_is_false(self):
        with patch.object(apt.shutil, "which", return_value=None):
            assert apt.dpkg_installed("net-tools") is False

    def test_installed_status_is_true(self):
        class R:
            returncode = 0
            stdout = "installed\n"
        with patch.object(apt.shutil, "which", return_value="/usr/bin/dpkg-query"), \
             patch("subprocess.run", return_value=R()):
            assert apt.dpkg_installed("net-tools") is True

    def test_not_installed_status_is_false(self):
        class R:
            returncode = 1
            stdout = "no packages found matching net-tools\n"
        with patch.object(apt.shutil, "which", return_value="/usr/bin/dpkg-query"), \
             patch("subprocess.run", return_value=R()):
            assert apt.dpkg_installed("net-tools") is False

    def test_config_files_status_is_false(self):
        # A purged-but-config-remaining package reports "config-files", not
        # "installed" -- must not count as present.
        class R:
            returncode = 0
            stdout = "config-files\n"
        with patch.object(apt.shutil, "which", return_value="/usr/bin/dpkg-query"), \
             patch("subprocess.run", return_value=R()):
            assert apt.dpkg_installed("net-tools") is False


class TestAptInstall:
    def test_non_linux_is_noop_failure(self):
        with patch.object(apt.sys, "platform", "darwin"):
            r = apt.apt_install("net-tools")
        assert r.ok is False and r.needs_elevation is False and "Linux-only" in r.message

    def test_already_installed_via_dpkg_needs_no_elevation(self):
        # A present package must short-circuit -- no sudo probe, no apt-get.
        with _linux(), \
             patch.object(apt, "dpkg_installed", return_value=True), \
             patch.object(apt, "sudo_noninteractive_available",
                          side_effect=AssertionError("must not probe sudo when already installed")), \
             patch("subprocess.run", side_effect=AssertionError("must not run apt-get when already installed")):
            r = apt.apt_install("net-tools")
        assert r.ok is True and r.needs_elevation is False
        assert "already installed" in r.message

    def test_missing_elevation_defers_without_attempting(self):
        # No passwordless sudo -> NEVER shell out to apt-get; report needs_elevation.
        with _linux(), \
             patch.object(apt, "dpkg_installed", return_value=False), \
             patch.object(apt, "sudo_noninteractive_available", return_value=False), \
             patch.object(apt, "_run", side_effect=AssertionError("must not run apt-get without privilege")):
            r = apt.apt_install("net-tools")
        assert r.ok is False
        assert r.needs_elevation is True
        assert "requires elevation" in r.message

    def test_install_success_with_passwordless_sudo(self):
        calls = []

        def fake_run(argv, timeout=600):
            calls.append(list(argv))
            return True, "ok"

        with _linux(), \
             patch.object(apt, "dpkg_installed", return_value=False), \
             patch.object(apt, "sudo_noninteractive_available", return_value=True), \
             patch.object(apt, "is_root", return_value=False), \
             patch.object(apt, "_run", side_effect=fake_run):
            r = apt.apt_install("net-tools")
        assert r.ok is True and r.needs_elevation is False
        assert "installed net-tools via apt" in r.message
        assert calls == [["sudo", "-n", "apt-get", "install", "-y", "net-tools"]]

    def test_install_as_root_omits_sudo(self):
        calls = []

        def fake_run(argv, timeout=600):
            calls.append(list(argv))
            return True, "ok"

        with _linux(), \
             patch.object(apt, "dpkg_installed", return_value=False), \
             patch.object(apt, "sudo_noninteractive_available", return_value=True), \
             patch.object(apt, "is_root", return_value=True), \
             patch.object(apt, "_run", side_effect=fake_run):
            r = apt.apt_install("net-tools")
        assert r.ok is True
        assert calls == [["apt-get", "install", "-y", "net-tools"]]

    def test_install_failure_reports_output(self):
        with _linux(), \
             patch.object(apt, "dpkg_installed", return_value=False), \
             patch.object(apt, "sudo_noninteractive_available", return_value=True), \
             patch.object(apt, "is_root", return_value=False), \
             patch.object(apt, "_run", return_value=(False, "E: Unable to locate package nope")):
            r = apt.apt_install("nope")
        assert r.ok is False and r.needs_elevation is False
        assert "apt-get install nope failed: E: Unable to locate package nope" in r.message
