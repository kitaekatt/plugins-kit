"""Unit tests for bootstrap_lib/scoop.py: install-failure detection and the
elevated-install command render.

The live defect these pin down (2060W, bootstrap 0.35.3): scoop's
extras/tailscale manifest has an admin-gated ``pre_install``
(``if (!(is_admin)) { error '...'; break }``), so an unelevated
``scoop install`` prints an ``ERROR`` line and creates no shim -- but the
scoop process still EXITS 0. The old scoop_install treated exit 0 as
"installed ... (shim not located)" instead of a failure. Detection now keys
on failure text in the output AND/OR a non-zero exit AND/OR the shim
re-check failing, and reports the captured scoop error.
"""

import sys

import pytest

import bootstrap_lib.scoop as scoop
from bootstrap_lib.scoop import (
    ScoopResult,
    _install_failure_detail,
    elevated_install_command,
    scoop_install,
)

ADMIN_GATE_OUTPUT = (
    "Installing 'tailscale' (1.98.8) [64bit] from 'extras' bucket\n"
    "ERROR This package requires admin rights to install\n"
)


@pytest.fixture
def win32(monkeypatch):
    """scoop_install's Windows-only guard, satisfied on any test host."""
    monkeypatch.setattr(sys, "platform", "win32")


class TestInstallFailureDetail:
    def test_error_line_with_exit_zero_is_a_failure(self):
        detail = _install_failure_detail(True, ADMIN_GATE_OUTPUT)
        assert detail == "ERROR This package requires admin rights to install"

    def test_install_failed_text_is_a_failure(self):
        out = "Install failed with previous errors\n"
        assert _install_failure_detail(True, out) == (
            "Install failed with previous errors")

    def test_installation_of_pkg_failed_text_is_a_failure(self):
        out = ("It looks like a previous installation of tailscale failed.\n"
               "Run 'scoop uninstall tailscale' before retrying the install.\n")
        assert "failed" in _install_failure_detail(True, out)

    def test_nonzero_exit_without_markers_uses_last_line(self):
        assert _install_failure_detail(False, "a\nboom happened\n") == (
            "boom happened")

    def test_nonzero_exit_with_no_output(self):
        assert _install_failure_detail(False, "") == "no output"

    def test_clean_success_is_none(self):
        out = "'p4' (2024.1) was installed successfully!\n"
        assert _install_failure_detail(True, out) is None

    def test_benign_error_word_mid_line_is_not_a_marker(self):
        # "0 errors" / hash lines must not read as failures.
        out = "Checking hash of p4.zip ... ok.\nLinking shims ... done\n"
        assert _install_failure_detail(True, out) is None


class TestScoopInstall:
    def _wire(self, monkeypatch, ok, out, shim=None):
        calls = []

        def fake_cmd(args, timeout=300):
            calls.append(args)
            if args.startswith("bucket add"):
                return True, "bucket added"
            return ok, out

        monkeypatch.setattr(scoop, "_scoop_cmd", fake_cmd)
        monkeypatch.setattr(scoop, "_find_shim", lambda name: shim)
        return calls

    def test_admin_gated_exit_zero_is_a_failed_install(self, win32, monkeypatch):
        """The live 2060W case: exit 0 + ERROR line + no shim => failure
        carrying the scoop error, NOT 'installed ... (shim not located)'."""
        self._wire(monkeypatch, ok=True, out=ADMIN_GATE_OUTPUT, shim=None)

        res = scoop_install("extras/tailscale", tool_name="tailscale")

        assert res.ok is False
        assert res.path is None
        assert "requires admin rights" in res.message
        assert "shim not located" not in res.message

    def test_nonzero_exit_is_a_failed_install(self, win32, monkeypatch):
        self._wire(monkeypatch, ok=False, out="something broke", shim=None)
        res = scoop_install("main/p4", tool_name="p4")
        assert res.ok is False
        assert "something broke" in res.message

    def test_clean_install_with_shim_succeeds(self, win32, monkeypatch):
        calls = self._wire(monkeypatch, ok=True,
                           out="'p4' was installed successfully!",
                           shim="C:/u/scoop/shims/p4.exe")
        res = scoop_install("main/p4", tool_name="p4")
        assert res == ScoopResult(True, "C:/u/scoop/shims/p4.exe",
                                  "installed main/p4 via scoop")
        assert calls == ["bucket add main", "install main/p4"]

    def test_clean_install_without_shim_is_a_failure(self, win32, monkeypatch):
        """Exit 0 and no failure text, but no shim materialized: no longer a
        trusted 'installed (shim not located)' success. (A legitimately
        shimless package still passes at the engine level via the entry's
        own `check` re-check, which the engine consults first.)"""
        self._wire(monkeypatch, ok=True, out="done", shim=None)
        res = scoop_install("main/odd", tool_name="odd")
        assert res.ok is False
        assert "no shim" in res.message

    def test_error_line_wins_even_if_a_stale_shim_exists(self, win32, monkeypatch):
        # Failure text takes precedence: never claim success off a leftover shim.
        self._wire(monkeypatch, ok=True, out=ADMIN_GATE_OUTPUT,
                   shim="C:/u/scoop/shims/tailscale.exe")
        res = scoop_install("extras/tailscale", tool_name="tailscale")
        assert res.ok is False


class TestElevatedInstallCommand:
    def test_bucket_form_adds_bucket_then_installs(self):
        cmd = elevated_install_command("extras/tailscale")
        assert cmd == (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "'scoop bucket add extras; scoop install extras/tailscale'"
        )

    def test_bare_package_form(self):
        cmd = elevated_install_command("nodejs")
        assert cmd == (
            "powershell -NoProfile -ExecutionPolicy Bypass -Command "
            "'scoop install nodejs'"
        )

    def test_powershell_command_stays_single_quoted(self):
        # This used to guard the script renderer's double-quote ban, which is
        # gone (the queue carries commands as data, so nothing splices them into
        # shell text). It still guards a real constraint one level down: the
        # command is itself a `powershell -Command '...'` string, so an
        # unescaped double quote would break THAT parse.
        assert '"' not in elevated_install_command("extras/tailscale")
