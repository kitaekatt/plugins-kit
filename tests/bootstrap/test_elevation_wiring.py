"""Engine-integration tests for the step-8 elevation wiring.

Covers the three descriptor SOURCES as the engine emits them plus the pass-end
surfacing:
  * source (c): _strategy_brew signals a brew_installer descriptor when Homebrew
    is missing (and NOT when a present-brew install merely fails);
  * emit_failure_response renders the aggregated elevation_script item (its path
    + what it does) and classifies it manual-only (not fix-all);
  * next-session pickup: a pass whose deferred op has been satisfied harvests an
    empty queue and the stale remediation script is removed.
"""

import json
import os

import bootstrap_lib.engine as engine
import bootstrap_lib.brew as brew_mod
import bootstrap_lib.elevation as elev
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths


def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)


# --------------------------------------------------------------------------- #
# Source (c): missing-brew installer signal
# --------------------------------------------------------------------------- #

class TestBrewInstallerSignal:
    def test_missing_brew_emits_brew_installer_descriptor(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: brew_mod.BrewResult(False, None, "Homebrew is not installed."))
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError("must not install when brew absent")))

        failure = engine._process_tool_entry(
            {"name": "direnv", "install": {"macos": {"brew": "direnv"}}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "brew_failed"
        assert failure["elevation"] == {"method": "brew_installer", "os": "macos"}
        # And the queue harvests it into the brew_installer flag.
        q = elev.queue_from_failures([failure], "macos")
        assert q.brew_installer is True

    def test_present_brew_install_failure_has_no_installer_descriptor(self, monkeypatch):
        # brew present but the formula install fails: NOT a missing-installer case.
        _stub(monkeypatch)
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setattr(brew_mod, "ensure_brew",
                            lambda: brew_mod.BrewResult(True, "/opt/homebrew/bin/brew", "already installed"))
        monkeypatch.setattr(brew_mod, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew_mod.BrewResult(False, None, "brew install nope failed"))

        failure = engine._process_tool_entry(
            {"name": "nope", "install": {"macos": {"brew": "nope"}}},
            "macos", "/data", "", [], [], [], plugin_name="p",
        )
        assert failure["install_state"] == "brew_failed"
        assert "elevation" not in failure


# --------------------------------------------------------------------------- #
# emit_failure_response rendering of the aggregated item
# --------------------------------------------------------------------------- #

class TestElevationScriptRendering:
    def test_failure_line_includes_script_path_and_is_manual(self, tmp_path, capsys):
        path = "/data/elevate/install-elevated.sh"
        q = elev.ElevationQueue(apt_packages=["net-tools"])
        agg = elev.elevation_script_failure(q, "ubuntu", path)
        # Not fix-all eligible: only the user can supply credentials.
        assert engine._is_auto_fixable(agg) is False

        engine.emit_failure_response(
            [agg], current_os="ubuntu", log_content="log",
            label="plugins-kit:bootstrap@test",
        )
        payload = json.loads(capsys.readouterr().out)
        ac = payload["hookSpecificOutput"]["additionalContext"]
        assert path in ac
        assert "sudo bash" in ac
        # All-manual footer wording (no fix-all-eligible items).
        assert "None of these are fix-all eligible" in ac


# --------------------------------------------------------------------------- #
# Next-session re-check pickup: stale script removed once the queue empties
# --------------------------------------------------------------------------- #

class TestNextSessionPickup:
    def test_satisfied_op_clears_the_script(self, tmp_path):
        data_dir = str(tmp_path)
        # Pass 1: an apt package was deferred -> script written.
        pass1 = [{"type": "tool", "name": "net-tools", "install_state": "needs_elevation",
                  "elevation": {"method": "apt", "package": "net-tools", "os": "ubuntu"}}]
        q1 = elev.queue_from_failures(pass1, "ubuntu")
        p1 = elev.write_or_clear_script(q1, data_dir, "ubuntu")
        assert p1 and os.path.isfile(p1)

        # Pass 2 (next session): the user ran the script, the tool now resolves,
        # so it produces no needs_elevation failure -> empty queue -> script gone.
        pass2 = []
        q2 = elev.queue_from_failures(pass2, "ubuntu")
        p2 = elev.write_or_clear_script(q2, data_dir, "ubuntu")
        assert p2 is None
        assert not os.path.exists(p1)
