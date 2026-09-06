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
    def test_run_brew_closes_stdin_and_bounds_timeout(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            return 0, "stdout", "stderr"

        monkeypatch.setattr(brew, "run_captured", fake_run, raising=False)

        ok, output = brew._run_brew("/x/brew", ["info"], timeout=37)

        assert ok is True
        assert output == "stdout"
        assert seen["stdin_devnull"] is True
        assert seen["timeout"] == 37

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
        # tap first, then install, in order. The install target MUST be
        # tap-qualified (<tap>/<formula>): homebrew-core ships a formula also
        # named `jj`, so a bare `brew install jj` after tapping tidwall/jj can
        # install the WRONG tool from core instead of the tapped one.
        assert calls == [["tap", "tidwall/jj"], ["install", "tidwall/jj/jj"]]
        assert "installed tidwall/jj/jj via brew" in r.message

    def test_tap_qualified_failure_names_qualified_target(self):
        # The error message must name the tap-qualified target the install
        # actually attempted, not the bare formula name.
        def fake_run(bin_path, args, timeout=600):
            if args[0] == "tap":
                return True, "ok"
            return False, "No available formula"

        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", side_effect=fake_run):
            r = brew.brew_install(formula="jj", tap="tidwall/jj")
        assert r.ok is False
        assert "brew install tidwall/jj/jj failed: No available formula" in r.message

    def test_untapped_formula_stays_bare(self):
        # No tap declared -> the install target is the bare formula (no prefix).
        calls = []

        def fake_run(bin_path, args, timeout=600):
            calls.append(list(args))
            return True, "ok"

        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", side_effect=fake_run):
            r = brew.brew_install(formula="direnv")
        assert r.ok is True
        assert calls == [["install", "direnv"]]

    def test_install_failure_reports_output(self):
        with _darwin(), \
             patch.object(brew, "_brew_bin", return_value="/x/brew"), \
             patch.object(brew, "_run_brew", return_value=(False, "No available formula")):
            r = brew.brew_install(formula="nope")
        assert r.ok is False
        assert "brew install nope failed: No available formula" in r.message
