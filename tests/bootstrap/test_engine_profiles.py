"""Engine-side wiring for bootstrap_lib.profiles -- U2-engine-integration.

profiles.py itself (resolve_layers, should_prompt, prompt_directive, ...) is
covered by test_profiles.py. This file covers only what the engine adds on
top of it: the layer-loading split (_load_layered_manifests /
_load_layered_manifests_ex), turning a resolved ProfileState into log entries
and failures (_report_profile_state), the profile-prompt directive gate
(_profile_prompt_directive / _profile_attended_note), and threading that
directive through the three emit_* response builders without ever letting it
reach persistent_output_file (_emit_pass_results).
"""

import json
import os

import pytest

import bootstrap_lib.engine as engine
from bootstrap_lib import profiles
from bootstrap_lib.records import short_form


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _state(status, **kw):
    kw.setdefault("selected", None)
    kw.setdefault("source", None)
    kw.setdefault("chain", ())
    kw.setdefault("available", ())
    kw.setdefault("warnings", ())
    kw.setdefault("errors", ())
    kw.setdefault("write_target", "/home/u/.claude/bootstrap.local.json")
    return profiles.ProfileState(status=status, **kw)


# --------------------------------------------------------------------------- #
# _load_layered_manifests_ex / _load_layered_manifests
# --------------------------------------------------------------------------- #


class TestLoadLayeredManifestsEx:
    def test_wrapper_returns_two_values(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        result = engine._load_layered_manifests(str(project))
        assert len(result) == 2
        merged, errors = result
        assert merged == {}
        assert errors == []

    def test_unparseable_local_layer_passed_as_none_and_reported(self, isolated_home, tmp_path):
        # A valid 'profiles' declaration in the committed project layer, plus
        # an unparseable project_local layer -- profiles.py's rule 5 needs the
        # unparseable SELECTION layer as None to mark the state invalid, and
        # the parse error must still be reported exactly as before.
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "bootstrap.json").write_text(
            json.dumps({"profiles": {"engineer": {}}})
        )
        (project_claude / "bootstrap.local.json").write_text("{bad")

        manifest, errors, state = engine._load_layered_manifests_ex(str(project))

        assert len(errors) == 1
        assert "bootstrap.local.json" in errors[0]["path"]
        assert "JSON parse error" in errors[0]["error"]
        # Rule 5: an unparseable local layer makes the selection unknowable.
        assert state.status == "invalid"
        assert state.errors == ()
        assert "profiles" not in manifest

    def test_missing_layer_is_not_passed_as_none(self, isolated_home, tmp_path):
        # A layer that does not exist at all must not trip rule 5 the way an
        # unparseable one does -- only project_local exists here, and it is
        # valid, so nothing should read as invalid.
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "bootstrap.local.json").write_text(
            json.dumps({"profiles": {"engineer": {}}, "profile": "engineer"})
        )

        manifest, errors, state = engine._load_layered_manifests_ex(str(project))

        assert errors == []
        assert state.status == "selected"
        assert state.selected == "engineer"

    def test_no_profiles_declared_is_still_no_profiles(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        manifest, errors, state = engine._load_layered_manifests_ex(str(project))
        assert errors == []
        assert state.status == "no_profiles"
        assert manifest == {}


# --------------------------------------------------------------------------- #
# _report_profile_state
# --------------------------------------------------------------------------- #


_ATTENDED_ENV = {
    profiles.ENV_SESSION_ATTENDED: "1",
    profiles.ENV_SESSION_ID: "sess-1",
}


class TestReportProfileState:
    def test_errors_become_failures_without_persist_across_sessions(self):
        state = _state("invalid", errors=("profile 'x' extends itself",))
        actions, oks, quiets = [], [], []
        failures = engine._report_profile_state(state, actions, oks, quiets)

        assert len(failures) == 1
        assert failures[0]["type"] == "profile_invalid"
        assert "persist_across_sessions" not in failures[0]
        assert failures[0]["plugin"] == "bootstrap"

    def test_warnings_become_action_entries_with_short_display(self):
        state = _state("unknown", warnings=("profile 'x' selected in y is not declared",))
        actions, oks, quiets = [], [], []
        failures = engine._report_profile_state(state, actions, oks, quiets)

        assert failures == []
        assert len(actions) == 1
        assert short_form(actions[0]) is not None
        assert len(short_form(actions[0])) <= 40

    def test_applied_chain_becomes_one_ok_entry_with_short_display(self):
        state = _state("selected", selected="engineer", chain=("base", "engineer"))
        actions, oks, quiets = [], [], []
        failures = engine._report_profile_state(state, actions, oks, quiets)

        assert failures == []
        assert len(oks) == 1
        assert short_form(oks[0]) is not None
        assert len(short_form(oks[0])) <= 40
        assert "engineer" in oks[0]

    def test_error_display_label_is_within_budget(self):
        state = _state("invalid", errors=("some very long declaration error " * 3,))
        actions, oks, quiets = [], [], []
        engine._report_profile_state(state, actions, oks, quiets)
        assert len(short_form(actions[0])) <= 40

    def test_no_profiles_reports_nothing(self):
        state = _state("no_profiles")
        actions, oks, quiets = [], [], []
        failures = engine._report_profile_state(state, actions, oks, quiets)
        assert failures == [] and actions == [] and oks == [] and quiets == []

    def test_attended_note_lands_in_the_quiet_entries_passed_in(self):
        # Delivery, not just computation: the note must be appended to the
        # SAME quiet_entries list the caller feeds into bootstrap.log's Step 6
        # block -- not merely returned or recorded into the pass record.
        state = _state("unselected")
        actions, oks, quiets = [], [], []
        engine._report_profile_state(state, actions, oks, quiets, env={})
        assert len(quiets) == 1
        assert "CLAUDE_CODE_SESSION_ATTENDED" in quiets[0]

    def test_attended_note_reaches_the_log_block_written_at_step6(self, tmp_path):
        # The regression this test exists for: the note used to be appended
        # at Step 8, AFTER Step 6 had already built and written
        # bootstrap_log_entries from bootstrap_quiet_entries -- so it was
        # recorded (RecordingList mirrors every append into the pass record
        # regardless of timing) but never appeared in bootstrap.log itself,
        # the file a maintainer actually reads. This test replicates Step 6's
        # exact formula and reads the log FILE back, so it fails if the note
        # is ever computed after that formula runs again.
        from bootstrap_lib.log import write_log_block

        state = _state("unselected")
        action_entries, ok_entries, quiet_entries = [], [], []
        engine._report_profile_state(
            state, action_entries, ok_entries, quiet_entries, env={})

        log_success = False
        bootstrap_log_entries = (
            action_entries + quiet_entries + (ok_entries if log_success else []))
        write_log_block(str(tmp_path), "bootstrap", bootstrap_log_entries)

        log_text = (tmp_path / "bootstrap.log").read_text()
        assert "CLAUDE_CODE_SESSION_ATTENDED" in log_text

    def test_no_attended_note_when_signal_present(self):
        state = _state("unselected")
        actions, oks, quiets = [], [], []
        engine._report_profile_state(state, actions, oks, quiets, env=_ATTENDED_ENV)
        assert quiets == []

    def test_no_attended_note_for_non_promptable_status(self):
        state = _state("no_profiles")
        actions, oks, quiets = [], [], []
        engine._report_profile_state(state, actions, oks, quiets, env={})
        assert quiets == []


# --------------------------------------------------------------------------- #
# _profile_prompt_directive / _profile_attended_note
# --------------------------------------------------------------------------- #


ATTENDED_ENV = {
    profiles.ENV_SESSION_ATTENDED: "1",
    profiles.ENV_SESSION_ID: "sess-1",
}


class TestProfilePromptDirective:
    def test_directive_appended_for_attended_promptable_session(self, tmp_path):
        state = _state("unselected", available=(
            profiles.ProfileInfo(name="engineer", description="Eng tools."),
        ))
        directive = engine._profile_prompt_directive(
            state, str(tmp_path / "markers"), "/plugin/scripts/bootstrap.sh",
            env=ATTENDED_ENV,
        )
        assert directive != ""
        assert "AskUserQuestion" in directive

    @pytest.mark.parametrize("env", [
        {},  # ATTENDED absent entirely
        {profiles.ENV_SESSION_ATTENDED: "0", profiles.ENV_SESSION_ID: "sess-1"},
        {profiles.ENV_SESSION_ATTENDED: "1"},  # no session id
    ])
    def test_no_directive_without_a_usable_attended_session(self, tmp_path, env):
        state = _state("unselected", available=(
            profiles.ProfileInfo(name="engineer"),
        ))
        directive = engine._profile_prompt_directive(
            state, str(tmp_path / "markers"), "/plugin/scripts/bootstrap.sh", env=env)
        assert directive == ""

    def test_no_directive_when_a_marker_already_exists(self, tmp_path):
        marker_dir = tmp_path / "markers"
        marker_dir.mkdir()
        profiles.mark_prompted(ATTENDED_ENV, str(marker_dir))
        state = _state("unselected", available=(profiles.ProfileInfo(name="engineer"),))
        directive = engine._profile_prompt_directive(
            state, str(marker_dir), "/plugin/scripts/bootstrap.sh", env=ATTENDED_ENV)
        assert directive == ""

    @pytest.mark.parametrize("status", ["no_profiles", "none", "selected", "invalid"])
    def test_no_directive_for_non_promptable_status(self, tmp_path, status):
        state = _state(status, available=(profiles.ProfileInfo(name="engineer"),))
        directive = engine._profile_prompt_directive(
            state, str(tmp_path / "markers"), "/plugin/scripts/bootstrap.sh",
            env=ATTENDED_ENV,
        )
        assert directive == ""


class TestProfileAttendedNote:
    """Covers only the PURE decision (does _profile_attended_note return
    text) -- NOT whether that text reaches bootstrap.log. This class alone
    used to give false confidence: it stayed green throughout the Step 8
    placement bug (the note was computed and returned correctly, it just
    never reached the log block). Delivery is covered separately by
    TestReportProfileState.test_attended_note_reaches_the_log_block_written_at_step6.
    """

    def test_note_when_promptable_and_signal_entirely_absent(self):
        state = _state("unselected")
        note = engine._profile_attended_note(state, {})
        assert note is not None

    def test_no_note_when_signal_present(self):
        state = _state("unselected")
        assert engine._profile_attended_note(state, ATTENDED_ENV) is None

    def test_no_note_for_non_promptable_status_even_if_signal_absent(self):
        state = _state("no_profiles")
        assert engine._profile_attended_note(state, {}) is None


# --------------------------------------------------------------------------- #
# extra_context threading through the three emit_* builders
# --------------------------------------------------------------------------- #


DIRECTIVE = "Ask the user with AskUserQuestion: pick a profile."


class TestExtraContextThreading:
    def test_silent_pass_emits_additional_context_with_no_system_message(self, tmp_path, capsys):
        engine.emit_success_response("", label="bootstrap", extra_context=DIRECTIVE)
        out = json.loads(capsys.readouterr().out)
        assert DIRECTIVE in out["hookSpecificOutput"]["additionalContext"]
        assert "systemMessage" not in out

    def test_success_response_extra_context_reaches_output_file_only(self, tmp_path):
        out_file = tmp_path / "pending.json"
        engine.emit_success_response(
            "some log line", label="bootstrap",
            output_file=str(out_file), extra_context=DIRECTIVE,
        )
        response = json.loads(out_file.read_text())
        assert DIRECTIVE in response["hookSpecificOutput"]["additionalContext"]

    def test_failure_response_extra_context_absent_from_persistent_file(self, tmp_path):
        out_file = tmp_path / "pending.json"
        persistent_file = tmp_path / "alert.json"
        failure = {
            "type": "path", "path": "/x", "plugin": "bootstrap",
            "persist_across_sessions": True,
        }
        engine.emit_failure_response(
            [failure], "macos", "log", label="bootstrap",
            output_file=str(out_file), persistent_output_file=str(persistent_file),
            extra_context=DIRECTIVE,
        )
        response = json.loads(out_file.read_text())
        persistent = json.loads(persistent_file.read_text())
        assert DIRECTIVE in response["hookSpecificOutput"]["additionalContext"]
        assert DIRECTIVE not in persistent["hookSpecificOutput"]["additionalContext"]

    def test_focused_failure_response_extra_context_absent_from_persistent_file(self, tmp_path):
        # elevation_script is the one type emit_failure_response routes through
        # _emit_focused when it is the only failure present.
        out_file = tmp_path / "pending.json"
        persistent_file = tmp_path / "alert.json"
        failure = {
            "type": "elevation_script", "plugin": "bootstrap",
            "message": "run the elevation script",
            "agent_msg": "run the elevation script",
            "persist_across_sessions": True,
        }
        engine.emit_failure_response(
            [failure], "macos", "log", label="bootstrap",
            output_file=str(out_file), persistent_output_file=str(persistent_file),
            extra_context=DIRECTIVE,
        )
        response = json.loads(out_file.read_text())
        persistent = json.loads(persistent_file.read_text())
        assert DIRECTIVE in response["hookSpecificOutput"]["additionalContext"]
        assert DIRECTIVE not in persistent["hookSpecificOutput"]["additionalContext"]


# --------------------------------------------------------------------------- #
# _emit_pass_results: mark_prompted called exactly once, after emission
# --------------------------------------------------------------------------- #


class _Args:
    def __init__(self, background=True, project_dir="/proj"):
        self.background = background
        self.project_dir = project_dir


class TestEmitPassResults:
    def test_mark_prompted_called_once_on_a_promptable_silent_pass(
        self, tmp_path, monkeypatch,
    ):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        state = _state("unselected", available=(profiles.ProfileInfo(name="engineer"),))
        monkeypatch.setattr(os, "environ", dict(ATTENDED_ENV))
        calls = []
        monkeypatch.setattr(
            profiles, "mark_prompted",
            lambda env, marker_dir: calls.append((env, marker_dir)),
        )
        monkeypatch.setattr(engine, "_clear_project_cooldown", lambda *a, **k: None)
        monkeypatch.setattr(engine, "_restamp_project_cooldown", lambda *a, **k: None)

        engine._emit_pass_results(
            all_failures=[], current_os="macos", display_content="",
            bootstrap_label="bootstrap", data_dir=str(data_dir),
            args=_Args(), recorder=None, profile_state=state,
            plugin_root=str(tmp_path / "plugin"),
        )

        assert len(calls) == 1
        pending = data_dir / "bootstrap_display.pending"
        assert pending.exists()
        response = json.loads(pending.read_text())
        assert "AskUserQuestion" in response["hookSpecificOutput"]["additionalContext"]
        assert "systemMessage" not in response

    def test_no_mark_prompted_when_nothing_to_prompt(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        state = _state("no_profiles")
        monkeypatch.setattr(os, "environ", dict(ATTENDED_ENV))
        calls = []
        monkeypatch.setattr(
            profiles, "mark_prompted",
            lambda env, marker_dir: calls.append((env, marker_dir)),
        )
        monkeypatch.setattr(engine, "_clear_project_cooldown", lambda *a, **k: None)
        monkeypatch.setattr(engine, "_restamp_project_cooldown", lambda *a, **k: None)

        engine._emit_pass_results(
            all_failures=[], current_os="macos", display_content="",
            bootstrap_label="bootstrap", data_dir=str(data_dir),
            args=_Args(), recorder=None, profile_state=state,
            plugin_root=str(tmp_path / "plugin"),
        )

        assert calls == []
        assert not (data_dir / "bootstrap_display.pending").exists()
