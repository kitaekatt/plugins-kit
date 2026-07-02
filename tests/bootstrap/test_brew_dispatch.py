"""Wiring tests for the brew install strategy in the engine dispatch table.

Covers _strategy_brew as consumed by engine._process_tool_entry: it applies
only when the canonical brew object is present for this host, is terminal on
apply (success or failure), sits AFTER resolve/scoop and BEFORE url-download /
install-command, and produces scoop-shaped failure dicts. brew.py itself is
mocked (its own subprocess behavior is covered by test_brew.py).
"""

import bootstrap_lib.engine as engine
import bootstrap_lib.brew as brew_mod
import bootstrap_lib.tool_check as tool_check
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths
import bootstrap_lib.downloader as downloader


def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


def _brew_present(monkeypatch):
    monkeypatch.setattr(brew_mod, "ensure_brew",
                        lambda: brew_mod.BrewResult(True, "/opt/homebrew/bin/brew", "already installed"))


# --------------------------------------------------------------------------- #
# ctx.brew_spec parsing (shorthand vs object forms)
# --------------------------------------------------------------------------- #

class TestBrewSpecParsing:
    def _spec(self, os_spec):
        return engine._ToolEntryCtx._parse_brew(os_spec)

    def test_string_shorthand_is_formula(self):
        assert self._spec({"brew": "direnv"}) == {"formula": "direnv"}

    def test_cask_object(self):
        assert self._spec({"brew": {"cask": "google-chrome"}}) == {"cask": "google-chrome"}

    def test_formula_with_tap(self):
        assert self._spec({"brew": {"formula": "jj", "tap": "tidwall/jj"}}) == \
            {"formula": "jj", "tap": "tidwall/jj"}

    def test_no_brew_key_is_none(self):
        assert self._spec({"command": "brew install x"}) is None
        assert self._spec("not-a-dict") is None


# --------------------------------------------------------------------------- #
# Strategy application + terminality + precedence
# --------------------------------------------------------------------------- #

class TestBrewStrategyApplies:
    def test_formula_installs_and_is_terminal(self, tmp_path, monkeypatch):
        _stub(monkeypatch)
        _brew_present(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))  # tool absent pre-install

        def fake_install(formula=None, cask=None, tap=None, timeout=600):
            (tmp_path / "direnv").write_text("#!/bin/sh\n")  # brew makes it appear
            return brew_mod.BrewResult(True, None, f"installed {formula} via brew")

        monkeypatch.setattr(brew_mod, "brew_install", fake_install)

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "direnv", "installPath": str(tmp_path),
             "install": {"macos": {"brew": "direnv"}}},
            "macos", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "via brew" in tools_installed[0][1]

    def test_cask_success_without_recheck_binary(self, monkeypatch):
        # A GUI cask has no CLI binary to re-check; brew's success is trusted.
        _stub(monkeypatch)
        _brew_present(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # nothing to resolve
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew_mod.BrewResult(True, None, f"installed {cask} via brew"))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "google-chrome",
             "install": {"macos": {"brew": {"cask": "google-chrome"}}}},
            "macos", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "google-chrome" in tools_installed[0][1]

    def test_absent_brew_is_terminal_failure_dict(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: brew_mod.BrewResult(False, None,
                                "Homebrew is not installed. Install it from https://brew.sh"))
        # brew_install must NOT be reached when ensure_brew fails.
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError("must not install when brew is absent")))

        action_entries = []
        failure = engine._process_tool_entry(
            {"name": "direnv", "install": {"macos": {"brew": "direnv"}}},
            "macos", "/data", "", action_entries, [], [], plugin_name="p",
        )
        assert failure is not None
        assert failure["type"] == "tool"
        assert failure["install_state"] == "brew_failed"
        assert failure["install_cmd"] is None
        assert failure["name"] == "direnv"
        assert any("brew unavailable" in a for a in action_entries)

    def test_formula_success_but_recheck_fails_is_failure(self, monkeypatch):
        # Audit-required pin: the trust-despite-failed-recheck leniency is CASK
        # ONLY. A formula that brew reports as installed but that still doesn't
        # resolve (keg-only, broken PATH) must surface a brew_failed failure --
        # the re-check is authoritative for formulas (strategy section 8).
        _stub(monkeypatch)
        _brew_present(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")  # tool never appears
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew_mod.BrewResult(True, None, f"installed {formula} via brew"))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "kegonly", "install": {"macos": {"brew": "kegonly"}}},
            "macos", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is not None
        assert failure["type"] == "tool"
        assert failure["install_state"] == "brew_failed"
        assert failure["install_cmd"] is None
        assert tools_installed == []  # never reported as installed

    def test_install_failure_is_terminal_failure_dict(self, monkeypatch):
        _stub(monkeypatch)
        _brew_present(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew_mod.BrewResult(False, None, "brew install nope failed: No formula"))

        failure = engine._process_tool_entry(
            {"name": "nope", "install": {"macos": {"brew": "nope"}}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "brew_failed"
        assert "No formula" in failure["message"]


class TestBrewPrecedence:
    def test_resolve_short_circuits_before_brew(self, tmp_path, monkeypatch):
        _stub(monkeypatch)
        tool = tmp_path / "direnv"
        tool.write_text("#!/bin/sh\n")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: (_ for _ in ()).throw(
                                AssertionError("brew must not run when the tool already resolves")))

        ok_entries = []
        failure = engine._process_tool_entry(
            {"name": "direnv", "installPath": str(tmp_path),
             "install": {"macos": {"brew": "direnv"}}},
            "macos", "/data", "", [], ok_entries, [], plugin_name="p",
        )
        assert failure is None
        assert any("direnv: ok" in e for e in ok_entries)

    def test_brew_precedes_url_download_and_install_command(self, tmp_path, monkeypatch):
        # An entry declaring brew + a url download + an install command resolves
        # via brew; neither the download nor the command runs (brew > download).
        _stub(monkeypatch)
        _brew_present(monkeypatch)
        monkeypatch.setenv("PATH", str(tmp_path))

        def fake_install(formula=None, cask=None, tap=None, timeout=600):
            (tmp_path / "jj").write_text("#!/bin/sh\n")  # brew makes it appear
            return brew_mod.BrewResult(True, None, f"installed {formula} via brew")

        monkeypatch.setattr(brew_mod, "brew_install", fake_install)
        monkeypatch.setattr(downloader, "download_and_install",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("url download must not run when brew applies")))
        monkeypatch.setattr(tool_check, "run_install",
                            lambda cmd: (_ for _ in ()).throw(
                                AssertionError("install command must not run when brew applies")))

        tools_installed = []
        failure = engine._process_tool_entry(
            {"name": "jj", "installPath": str(tmp_path),
             "install": {"macos": {"brew": "jj"}},
             "download": {"macos-arm64": {"url": "http://x/y", "sha256": "ab"}}},
            "macos", "/data", "", [], [], tools_installed, plugin_name="p",
        )
        assert failure is None
        assert tools_installed and "via brew" in tools_installed[0][1]

    def test_brew_not_applied_on_non_macos_host(self, monkeypatch):
        # A macOS-only brew entry is dead on ubuntu: brew_spec is None there, so
        # the strategy falls through to the install command (here: manual).
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: (_ for _ in ()).throw(
                                AssertionError("brew strategy must not engage on ubuntu")))

        failure = engine._process_tool_entry(
            {"name": "t", "install": {"macos": {"brew": "t"}, "ubuntu": "manual"}},
            "ubuntu", "/data", "", [], [], [], plugin_name="p",
        )
        # Falls through to install-command strategy -> manual sentinel.
        assert failure["install_state"] == "manual_install"
