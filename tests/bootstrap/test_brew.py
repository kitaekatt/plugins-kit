"""Unit tests for bootstrap_lib/brew.py (macOS Homebrew backend).

Mirrors the scoop backend's shape: stdlib-only, subprocess-based, macOS-only.
subprocess is mocked; no real brew is invoked. Two behaviors are load-bearing
and pinned here:
  * ensure_brew() is DETECT-ONLY -- it NEVER runs the Homebrew installer;
  * brew_install() drives formula / cask / tap correctly and requires exactly
    one of formula|cask.
"""

from unittest.mock import patch

import bootstrap_lib.brew as brew
from bootstrap_lib.brew import BrewResult


def _darwin():
    return patch.object(brew.sys, "platform", "darwin")


class TestEnsureBrew:
    def test_non_macos_is_noop_failure(self):
        with patch.object(brew.sys, "platform", "linux"):
            r = brew.ensure_brew()
        assert r.ok is False and "macOS-only" in r.message

    def test_present_returns_ok_with_path(self):
        with _darwin(), patch.object(brew, "_brew_bin", return_value="/opt/homebrew/bin/brew"):
            r = brew.ensure_brew()
        assert r.ok is True
        assert r.path == "/opt/homebrew/bin/brew"
        assert r.message == "already installed"

    def test_missing_fails_descriptively_and_never_installs(self):
        # DETECT-ONLY: subprocess must never be touched when brew is absent.
        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value=None), \
             patch("subprocess.run", side_effect=AssertionError("ensure_brew must not install")):
            r = brew.ensure_brew()
        assert r.ok is False
        assert "brew.sh" in r.message  # tells the user where to install it


class TestBrewInstall:
    def test_non_macos_is_noop_failure(self):
        with patch.object(brew.sys, "platform", "linux"):
            r = brew.brew_install(formula="direnv")
        assert r.ok is False and "macOS-only" in r.message

    def test_requires_exactly_one_of_formula_or_cask(self):
        with _darwin(), patch.object(brew, "_brew_bin", return_value="/x/brew"):
            neither = brew.brew_install()
            both = brew.brew_install(formula="a", cask="b")
        assert neither.ok is False and "exactly one" in neither.message
        assert both.ok is False and "exactly one" in both.message

    def test_formula_install_runs_brew_install(self):
        calls = []

        def fake_run(bin_path, args, timeout=600):
            calls.append(list(args))
            return True, "ok"

        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", side_effect=fake_run):
            r = brew.brew_install(formula="direnv")
        assert r.ok is True and "installed direnv via brew" in r.message
        assert calls == [["install", "direnv"]]

    def test_cask_install_passes_cask_flag(self):
        calls = []

        def fake_run(bin_path, args, timeout=600):
            calls.append(list(args))
            return True, "ok"

        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", side_effect=fake_run):
            r = brew.brew_install(cask="google-chrome")
        assert r.ok is True and "installed google-chrome via brew" in r.message
        assert calls == [["install", "--cask", "google-chrome"]]

    def test_tap_added_before_formula_install(self):
        calls = []

        def fake_run(bin_path, args, timeout=600):
            calls.append(list(args))
            return True, "ok"

        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", side_effect=fake_run):
            r = brew.brew_install(formula="jj", tap="tidwall/jj")
        assert r.ok is True
        # tap first, then install, in order.
        assert calls == [["tap", "tidwall/jj"], ["install", "jj"]]

    def test_install_failure_reports_output(self):
        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", return_value=(False, "No available formula")):
            r = brew.brew_install(formula="nope")
        assert r.ok is False
        assert "brew install nope failed: No available formula" in r.message
