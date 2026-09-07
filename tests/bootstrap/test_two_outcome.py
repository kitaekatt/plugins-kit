"""The two-outcome contract: every surfaced issue is AUTO (fix now, no prompt)
or ASK (AskUserQuestion first). There is no third 'manual attention / work
through it with Claude' outcome.

AUTO is the fleet-management default -- including installing non-elevated
software. ASK fires only when the fix needs elevation, a user action, or info
only the user has (see engine._ask_reason).
"""

import json
import os

import bootstrap_lib.engine as engine


# --------------------------------------------------------------------------- #
# _ask_reason: the classifier
# --------------------------------------------------------------------------- #

class TestAskReason:
    def test_plain_types_are_auto(self):
        for t in ("venv", "json", "pypi", "sync_to_data", "ini", "path"):
            assert engine._ask_reason({"type": t}) is None
            assert engine._needs_user({"type": t}) is False

    def test_credential_network_types_ask(self):
        # marketplace/plugin/git_dep only surface after a failed in-line network
        # op; a doomed AUTO retry helps nobody and the fix often needs a
        # credential. They ASK, even carrying a remediation_cmd.
        for t in ("marketplace", "plugin", "git_dep"):
            assert engine._ask_reason({"type": t}) == "info"
            assert engine._ask_reason(
                {"type": t, "remediation_cmd": "git clone x"}) == "info"
            assert engine._needs_user({"type": t}) is True
        # And they are no longer advertised as fix-all-eligible.
        assert engine._is_auto_fixable({"type": "git_dep",
                                        "remediation_cmd": "git clone x"}) is False

    def test_json_ini_in_user_scope_are_auto(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        claude = tmp_path / ".claude"
        assert engine._ask_reason(
            {"type": "json", "target": str(claude / "settings.json")}) is None
        assert engine._ask_reason(
            {"type": "ini", "file": str(claude / "plugins/data/x/foo.ini")}) is None
        # No target named at all: nothing to guard -> stays AUTO.
        assert engine._ask_reason({"type": "json"}) is None

    def test_json_ini_outside_user_scope_ask(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        # A manifest pointing json/ini at a shared/VCS-tracked project file.
        assert engine._ask_reason(
            {"type": "json", "target": str(tmp_path / "proj/.p4config")}) == "info"
        assert engine._ask_reason(
            {"type": "ini", "file": "/etc/someapp.ini"}) == "info"

    def test_sync_to_data_outside_user_scope_asks(self):
        # sync_to_data is in _AUTO_FIXABLE_TYPES and _write_target reads its
        # `dst`, so an unattended sync whose destination is outside ~/.claude
        # must ASK -- the same scope guard json/ini already get.
        assert engine._ask_reason(
            {"type": "sync_to_data", "dst": "/tmp/x"}) == "info"

    def test_sync_to_data_in_user_scope_is_auto(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        claude = tmp_path / ".claude"
        assert engine._ask_reason(
            {"type": "sync_to_data", "dst": str(claude / "plugins/data/x/y")}
        ) is None


class TestUserScopeResolution:
    """_user_scope_root / _path_in_user_scope must resolve BOTH sides the
    same way -- see fix_runner.py's HOME split note for why HOME and
    expanduser() can disagree on Windows Git Bash."""

    def test_home_env_expanduser_mismatch_still_resolves_in_scope(
            self, monkeypatch, tmp_path):
        """Simulates the Windows Git Bash split: $HOME is msys-style
        ("/c/Users/x") while os.path.expanduser resolves the native profile
        (ntpath prefers USERPROFILE over HOME). Before the fix, the root was
        built straight from os.environ["HOME"] while the target went through
        expanduser, so a genuinely in-scope target routed ASK."""
        native_home = str(tmp_path / "native")
        os.makedirs(native_home, exist_ok=True)
        monkeypatch.setenv("HOME", "/msys/style/home")  # deliberately wrong
        monkeypatch.setattr(
            engine.os.path, "expanduser",
            lambda p: (native_home + p[1:]) if p.startswith("~") else p)
        target = os.path.join(native_home, ".claude", "settings.json")
        assert engine._path_in_user_scope(target) is True

    def test_symlink_inside_claude_pointing_outside_is_not_in_scope(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        claude = tmp_path / ".claude"
        claude.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        real = outside / "real.json"
        real.write_text("{}")
        link = claude / "escape.json"
        link.symlink_to(real)
        assert engine._path_in_user_scope(str(link)) is False

    def test_symlink_inside_claude_pointing_inside_stays_in_scope(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        claude = tmp_path / ".claude"
        claude.mkdir()
        real = claude / "real.json"
        real.write_text("{}")
        link = claude / "alias.json"
        link.symlink_to(real)
        assert engine._path_in_user_scope(str(link)) is True

    def test_case_insensitive_host_normalizes_case(self, monkeypatch, tmp_path):
        # Guarded by monkeypatching normcase itself (rather than relying on
        # the real host filesystem's case sensitivity) so the test result
        # does not depend on which platform it runs on.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # ntpath.expanduser reads this, not HOME
        monkeypatch.setattr(engine.os.path, "normcase", lambda p: p.lower())
        claude_dir = os.path.join(str(tmp_path), ".claude")
        target = os.path.join(claude_dir.upper(), "settings.json")
        assert engine._path_in_user_scope(target) is True

    def test_tool_install_is_auto(self):
        # Installing non-elevated software is AUTO on a fleet.
        f = {"type": "tool", "name": "jq", "install_cmd": "winget install jq"}
        assert engine._ask_reason(f) is None

    def test_explicit_ask_reason_wins(self):
        for reason in ("elevation", "action", "info"):
            assert engine._ask_reason({"type": "venv", "ask_reason": reason}) == reason

    def test_bogus_explicit_reason_ignored(self):
        assert engine._ask_reason({"type": "venv", "ask_reason": "nonsense"}) is None

    def test_derived_elevation(self):
        assert engine._ask_reason({"type": "tool", "install_state": "needs_elevation"}) == "elevation"
        assert engine._ask_reason({"type": "python_stub"}) == "elevation"
        assert engine._ask_reason({"type": "elevation_script"}) == "elevation"

    def test_derived_action(self):
        assert engine._ask_reason({"type": "tool", "install_state": "manual_install"}) == "action"
        assert engine._ask_reason({"type": "bootstrap_outdated"}) == "action"

    def test_derived_info(self):
        assert engine._ask_reason({"type": "tool", "install_state": "installed_but_path_stale"}) == "info"
        assert engine._ask_reason({"type": "config"}) == "info"
        assert engine._ask_reason({"type": "project_config"}) == "info"

    def test_unfixable_unmarked_falls_to_ask(self):
        # Not fix-all-eligible, no runnable command, not explicitly marked: must
        # ASK, never be handed a bogus AUTO 'fix now' with nothing to run.
        assert engine._ask_reason({"type": "env_check", "message": "sudoers missing"}) == "info"
        assert engine._ask_reason({"type": "tool", "name": "x"}) == "info"  # no install_cmd

    def test_unfixable_but_runnable_is_auto(self):
        # A non-fix-all type WITH a runnable command is genuinely AUTO.
        assert engine._ask_reason({"type": "env_check", "remediation_cmd": "systemctl x"}) is None


class TestAutoFixableNowAgreesWithIsAutoFixable:
    """_auto_fixable_now's own fallback used to return True for two shapes
    _is_auto_fixable (and _ask_reason) already route ASK -- masked in
    practice only because _ask_reason checks install_state/credential-network
    BEFORE ever reaching _auto_fixable_now. Called standalone, the two
    predicates must agree."""

    def test_installed_but_path_stale_is_not_auto_fixable_now(self):
        f = {"type": "tool", "name": "jq", "install_state": "installed_but_path_stale",
             "install_cmd": "winget install jq"}
        assert engine._is_auto_fixable(f) is False
        assert engine._auto_fixable_now(f) is False

    def test_credential_network_types_are_not_auto_fixable_now(self):
        for t in ("git_dep", "marketplace", "plugin"):
            f = {"type": t, "remediation_cmd": "git clone x"}
            assert engine._is_auto_fixable(f) is False
            assert engine._auto_fixable_now(f) is False

    def test_ordinary_runnable_type_is_still_auto_fixable_now(self):
        # Not a regression target -- an unrelated type with a runnable
        # command must remain AUTO.
        f = {"type": "env_check", "remediation_cmd": "systemctl x"}
        assert engine._auto_fixable_now(f) is True


# --------------------------------------------------------------------------- #
# emit_failure_response: the two outcomes end to end (background mode)
# --------------------------------------------------------------------------- #

def _emit(failures, tmp_path, current_os="ubuntu"):
    out = tmp_path / "bootstrap_display.pending"
    engine.emit_failure_response(
        failures, current_os=current_os, log_content="log",
        label="mkt:bootstrap@test", output_file=str(out))
    payload = json.loads(out.read_text())
    return (payload["systemMessage"],
            payload["hookSpecificOutput"]["additionalContext"])


class TestTwoOutcomeEmission:
    def test_all_auto_fixes_now_never_asks(self, tmp_path):
        failures = [
            {"type": "venv", "remediation_cmd": "python -m venv .venv"},
            {"type": "pypi", "package": "requests", "message": "missing"},
        ]
        sysmsg, ac = _emit(failures, tmp_path)
        # The sentinel is the directive's load-bearing content, not a slogan:
        # the citation has to survive, or the text stops being verifiable by
        # the agent receiving it. See docs/reference/agent-directive-standards.md.
        assert "are AUTO under" in ac
        assert "remediation-reference.md" in ac
        assert "AskUserQuestion" not in ac
        assert "fixing these automatically" in sysmsg
        assert "Claude will ask" not in sysmsg

    def test_ask_block_is_items_only(self, tmp_path):
        # The ASK half of the systemMessage is the items and nothing else: the
        # old constant lead ("Claude will ask before changing anything here")
        # said the same thing on every message and the AskUserQuestion prompt
        # demonstrates it a beat later, and the "info" reason blurb ("needs a
        # detail from you") only restated the item's own message. Both were
        # removed for being pure line-count; this is what stops them returning.
        failures = [
            {"type": "config", "name": "api-key", "agent_msg": "set OPENAI_API_KEY",
             "message": "OPENAI_API_KEY is not set"},
        ]
        sysmsg, _ = _emit(failures, tmp_path)
        assert "OPENAI_API_KEY is not set" in sysmsg
        assert "Claude will ask" not in sysmsg
        assert "needs a detail from you" not in sysmsg
        # An elevation/action item keeps its blurb -- it names a constraint the
        # item's own message does not carry.
        sysmsg, _ = _emit(
            [{"type": "tool", "name": "vpn", "install_state": "manual_install",
              "message": "install the VPN client"}], tmp_path)
        assert "install the VPN client (needs you to do something)" in sysmsg

    def test_all_ask_uses_askuserquestion_never_auto(self, tmp_path):
        failures = [
            {"type": "config", "name": "api-key", "agent_msg": "set OPENAI_API_KEY",
             "message": "OPENAI_API_KEY is not set"},
            {"type": "tool", "name": "vpn", "install_state": "manual_install",
             "message": "install the VPN client"},
        ]
        sysmsg, ac = _emit(failures, tmp_path)
        assert "AskUserQuestion" in ac
        assert "are AUTO under" not in ac
        assert "OPENAI_API_KEY is not set" in sysmsg
        # No third-outcome language anywhere.
        assert "manual attention" not in (sysmsg + ac)
        assert "work through" not in (sysmsg + ac)

    def test_mixed_has_both_directives(self, tmp_path):
        failures = [
            {"type": "venv", "remediation_cmd": "python -m venv .venv"},
            {"type": "config", "name": "api-key", "agent_msg": "set OPENAI_API_KEY",
             "message": "OPENAI_API_KEY is not set"},
        ]
        sysmsg, ac = _emit(failures, tmp_path)
        assert "are AUTO under" in ac
        assert "AskUserQuestion" in ac
        assert "fixing these automatically" in sysmsg
        assert "OPENAI_API_KEY is not set" in sysmsg

    def test_auto_directive_meets_agent_directive_standards(self, tmp_path):
        # docs/reference/agent-directive-standards.md. This text reaches a
        # CONSUMER's session via additionalContext -- the same channel that
        # carries untrusted content -- so it must name the file backing any
        # authority it claims (AD-2) and must not tell Claude to bypass the
        # user (AD-3). A user publicly refused the earlier wording on exactly
        # these grounds on 2026-08-11; this test is what stops it coming back.
        failures = [
            {"type": "venv", "remediation_cmd": "python -m venv .venv"},
            {"type": "pypi", "package": "requests", "message": "missing"},
        ]
        _, ac = _emit(failures, tmp_path)
        lowered = ac.lower()
        for banned in ("fleet policy", "without asking the user",
                       "do not wait for the user", "do not tell the user"):
            assert banned not in lowered, f"agent-directive standards: {banned!r}"
        # AD-2: the authority claim names a file the receiving agent can open.
        assert "remediation-reference.md" in ac
        # AD-3: the user's ability to intervene is stated, not pre-empted.
        assert "stop" in lowered

    def test_custom_failure_can_declare_ask(self, tmp_path):
        # A plugin custom_bootstrap failure that needs a user ACTION declares it
        # via ask_reason; its friendly user_msg reaches the user verbatim.
        failures = [{
            "type": "hue_bridge_pairing",
            "plugin": "hue-kit",
            "ask_reason": "action",
            "user_msg": "hue-kit wants to pair with your Hue bridge",
            "agent_msg": "run `hue-kit pair`; the user presses the bridge button",
            "message": "hue-kit: no application key -- pairing needed",
        }]
        sysmsg, ac = _emit(failures, tmp_path)
        assert ("hue-kit wants to pair with your Hue bridge "
                "(needs you to do something)") in sysmsg
        assert "AskUserQuestion" in ac
        assert "manual attention" not in (sysmsg + ac)

    def test_no_forbidden_third_outcome_language(self, tmp_path):
        # Whatever the mix, the retired phrasings never appear.
        failures = [
            {"type": "venv", "remediation_cmd": "c"},
            {"type": "config", "name": "k", "message": "m", "agent_msg": "a"},
            {"type": "tool", "name": "t", "install_state": "manual_install", "message": "m"},
        ]
        sysmsg, ac = _emit(failures, tmp_path)
        blob = sysmsg + "\n" + ac
        for forbidden in ("need manual attention", "needs manual attention",
                          "work through them with Claude",
                          "guide the user through the steps",
                          "fix-all eligible"):
            assert forbidden not in blob
