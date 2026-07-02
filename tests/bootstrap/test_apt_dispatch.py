"""Wiring tests for the apt install strategy in the engine dispatch table.

Covers _strategy_apt as consumed by engine._process_tool_entry: it applies only
when the canonical apt spec is present for this host (Ubuntu), is terminal on
apply (install / needs-elevation / failure), sits AFTER resolve/scoop/brew and
BEFORE url-download / install-command, and produces the failure shapes the later
elevation-queue step consumes. apt.py itself is mocked (its own subprocess
behavior is covered by test_apt.py).
"""

import bootstrap_lib.engine as engine
import bootstrap_lib.apt as apt_mod
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths
import bootstrap_lib.downloader as downloader


def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


# --------------------------------------------------------------------------- #
# ctx.apt_pkg parsing (canonical string form + malformed specs)
# --------------------------------------------------------------------------- #

class TestAptSpecParsing:
    def _ctx(self, tool_def, current_os):
        tool_def = engine._normalize_tool_entry(tool_def, current_os)
        return engine._ToolEntryCtx(
            tool_def, current_os, "", [], [], [], plugin_name="p")

    def test_string_package_on_ubuntu(self):
        ctx = self._ctx({"name": "t", "install": {"ubuntu": {"apt": "net-tools"}}}, "ubuntu")
        assert ctx.apt_pkg == "net-tools"

    def test_no_apt_key_is_none(self):
        ctx = self._ctx({"name": "t", "install": {"ubuntu": {"command": "x"}}}, "ubuntu")
        assert ctx.apt_pkg is None

    def test_apt_entry_invisible_on_non_ubuntu_host(self):
        # apt lives under install.ubuntu; a macos pass never sees it.
        ctx = self._ctx({"name": "t", "install": {"ubuntu": {"apt": "net-tools"}}}, "macos")
        assert ctx.apt_pkg is None

    def test_bare_string_install_is_command_not_apt(self):
        # A legacy bare-string install normalizes to {"command": ...}, so apt_pkg
        # stays None (the string is an opaque command, not an apt package).
        ctx = self._ctx({"name": "t", "install": {"ubuntu": "apt-get install foo"}}, "ubuntu")
        assert ctx.apt_pkg is None


# --------------------------------------------------------------------------- #
# Strategy application + terminality
# --------------------------------------------------------------------------- #

class TestAptStrategyApplies:
    def test_install_success_is_terminal(self, tmp_path, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # tool absent pre-install

        def fake_install(pkg, timeout=600):
            (tmp_path / "ifconfig").write_text("#!/bin/sh\n")  # apt makes it appear
            return apt_mod.AptResult(True, False, f"installed {pkg} via apt")

        monkeypatch.setattr(apt_mod, "apt_install", fake_install)

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "ifconfig", "installPath": str(tmp_path),
             "install": {"ubuntu": {"apt": "net-tools"}}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "via apt" in tools_installed[0][1]
        assert "`net-tools`" in tools_installed[0][1]

    def test_needs_elevation_failure_shape(self, monkeypatch):
        # The step-8-consumable outcome: install_state needs_elevation, install_cmd
        # None (manual-only), a structured `elevation` descriptor, persistent.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda pkg, timeout=600: apt_mod.AptResult(
                                False, True, f"apt install {pkg} requires elevation"))

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "net-tools", "install": {"ubuntu": {"apt": "net-tools"}}},
            "ubuntu", "/data", "", action_entries, [], [], plugin_name="p",
        )
        assert failure is not None
        assert failure["type"] == "tool"
        assert failure["install_state"] == "needs_elevation"
        assert failure["install_cmd"] is None
        assert failure["persist_across_sessions"] is True
        assert failure["elevation"] == {"method": "apt", "package": "net-tools", "os": "ubuntu"}
        assert "sudo apt-get install -y net-tools" in failure["agent_msg"]
        assert any("needs elevation" in a for a in action_entries)
        # And it is NOT fix-all eligible (only the user can elevate).
        assert engine._is_auto_fixable(failure) is False

    def test_needs_elevation_never_attempts_install(self, monkeypatch):
        # Re-check must not even run: a deferred entry does nothing on the machine.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda pkg, timeout=600: apt_mod.AptResult(False, True, "needs elevation"))
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("no install command may run for a deferred apt entry")))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "net-tools", "install": {"ubuntu": {"apt": "net-tools"}}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure["install_state"] == "needs_elevation"
        assert tools_installed == []

    def test_install_success_but_recheck_fails_is_apt_failed(self, monkeypatch):
        # No cask-style trust for apt: apt claims success but the tool still does
        # not resolve -> apt_failed (re-check authoritative).
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool never appears
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda pkg, timeout=600: apt_mod.AptResult(
                                True, False, f"installed {pkg} via apt"))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "ghost", "install": {"ubuntu": {"apt": "ghost-pkg"}}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is not None
        assert failure["install_state"] == "apt_failed"
        assert failure["install_cmd"] is None
        assert tools_installed == []  # never reported as installed
        assert "does not resolve" in failure["message"]

    def test_install_failure_is_apt_failed_with_output(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda pkg, timeout=600: apt_mod.AptResult(
                                False, False, "apt-get install nope failed: E: not found"))

        failure = engine._process_tool_entry(
            {"name": "nope", "install": {"ubuntu": {"apt": "nope"}}},
            "ubuntu", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "apt_failed"
        assert "not found" in failure["message"]


# --------------------------------------------------------------------------- #
# Precedence + OS gating
# --------------------------------------------------------------------------- #

class TestAptPrecedence:
    def test_resolve_short_circuits_before_apt(self, tmp_path, monkeypatch):
        _stub(monkeypatch)
        tool = tmp_path / "ifconfig"
        tool.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("apt must not run when the tool already resolves")))

        ok_entries = []
        failure = engine._process_tool_entry(
            {"name": "ifconfig", "installPath": str(tmp_path),
             "install": {"ubuntu": {"apt": "net-tools"}}},
            "ubuntu", "/data", "", [], ok_entries, [], plugin_name="p",
        )
        assert failure is None
        assert any("ifconfig: ok" in e for e in ok_entries)

    def test_apt_precedes_url_download_and_install_command(self, tmp_path, monkeypatch):
        # An entry declaring apt + a url download + an install command resolves
        # via apt; neither the download nor the command runs (apt > download).
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))

        def fake_install(pkg, timeout=600):
            (tmp_path / "tmux").write_text("#!/bin/sh\n")  # apt makes it appear
            return apt_mod.AptResult(True, False, f"installed {pkg} via apt")

        monkeypatch.setattr(apt_mod, "apt_install", fake_install)
        monkeypatch.setattr(downloader, "download_and_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("url download must not run when apt applies")))
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("install command must not run when apt applies")))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "tmux", "installPath": str(tmp_path),
             "install": {"ubuntu": {"apt": "tmux"}},
             "download": {"ubuntu-amd64": {"url": "http://x/y", "sha256": "ab"}}},
            "ubuntu", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "via apt" in tools_installed[0][1]

    def test_apt_not_applied_on_non_ubuntu_host(self, monkeypatch):
        # An Ubuntu-only apt entry is dead on macos: apt_pkg is None there, so the
        # strategy falls through to the install command (here: manual).
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(apt_mod, "apt_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("apt strategy must not engage on macos")))

        failure = engine._process_tool_entry(
            {"name": "t", "install": {"ubuntu": {"apt": "net-tools"}, "macos": "manual"}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        # Falls through to install-command strategy -> manual sentinel.
        assert failure["install_state"] == "manual_install"
