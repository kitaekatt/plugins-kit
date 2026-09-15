"""Tests for bootstrap_lib/profiles.py -- layered profile resolution.

Layer fixtures are built as ``(path, kind, manifest)`` triples lowest-priority
first, exactly as the engine's loader hands them over. A manifest of ``None``
stands for a file that exists but could not be parsed.
"""

import json
import os
import subprocess
import time

import pytest

from bootstrap_lib import profiles


def _layer(kind, data):
    return ("/fixtures/%s.json" % kind, kind, data)


def _resolve(layers, project_dir="/proj", home="/home/u"):
    return profiles.resolve_layers(layers, project_dir=project_dir, home=home)


ONE_PROFILE = {"profiles": {"engineer": {"description": "Engineering tools."}}}


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


class TestStatuses:
    def test_no_profiles_when_nothing_declares_any(self):
        manifest, state = _resolve([_layer("user", {"tools": [{"name": "uv"}]})])
        assert state.status == "no_profiles"
        assert state.warnings == () and state.errors == ()
        assert state.chain == ()
        assert manifest == {"tools": [{"name": "uv"}]}

    def test_unselected_when_profiles_exist_and_none_is_chosen(self):
        _, state = _resolve([_layer("project", ONE_PROFILE)])
        assert state.status == "unselected"
        assert state.selected is None
        assert [p.name for p in state.available] == ["engineer"]

    def test_none_is_an_explicit_base_only_selection(self):
        _, state = _resolve([
            _layer("project", ONE_PROFILE),
            _layer("project_local", {"profile": "none"}),
        ])
        assert state.status == "none"
        assert state.selected == "none"
        assert state.chain == ()

    def test_selected_applies_the_profile_body(self):
        layers = [_layer("project", {
            "profiles": {"engineer": {"path_entries": ["/opt/eng/bin"]}},
        }), _layer("project_local", {"profile": "engineer"})]
        manifest, state = _resolve(layers)
        assert state.status == "selected"
        assert state.chain == ("engineer",)
        assert state.source == "/fixtures/project_local.json"
        assert manifest["path_entries"] == ["/opt/eng/bin"]

    def test_unknown_name_warns_and_applies_the_base_only(self):
        manifest, state = _resolve([
            _layer("project", {"profiles": {"engineer": {}}, "tools": [{"name": "uv"}]}),
            _layer("user_local", {"profile": "designer"}),
        ])
        assert state.status == "unknown"
        assert state.selected == "designer"
        assert any("designer" in w for w in state.warnings)
        assert manifest == {"tools": [{"name": "uv"}]}

    def test_invalid_on_a_declaration_error(self):
        _, state = _resolve([_layer("project", {"profiles": {"engineer": "nope"}})])
        assert state.status == "invalid"
        assert any("must be an object" in e for e in state.errors)

    def test_invalid_when_a_local_layer_could_not_be_parsed(self):
        # The caller reports the parse error itself, so no duplicate error here
        # -- what matters is that the selection is treated as unknowable.
        _, state = _resolve([
            _layer("project", ONE_PROFILE),
            _layer("user_local", None),
        ])
        assert state.status == "invalid"
        assert state.errors == ()

    def test_a_non_local_parse_error_does_not_invalidate(self):
        _, state = _resolve([_layer("project", None), _layer("user", ONE_PROFILE)])
        assert state.status == "unselected"


class TestD9NoProfiles:
    def test_empty_profiles_is_no_profiles_even_with_a_selection(self):
        """D9: an inert feature never warns, errors, or asks."""
        _, state = _resolve([
            _layer("project", {"profiles": {}}),
            _layer("project_local", {"profile": "engineer"}),
        ])
        assert state.status == "no_profiles"
        assert state.errors == () and state.warnings == ()
        assert profiles.build_question(state, "first_run") is None
        assert profiles.should_prompt(
            state,
            {"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_SESSION_ID": "s1"},
            "/nonexistent",
        ) is False

    def test_absent_profiles_with_a_selection_is_also_no_profiles(self):
        _, state = _resolve([_layer("user_local", {"profile": "engineer"})])
        assert state.status == "no_profiles"

    def test_write_target_is_user_local_under_no_profiles(self):
        _, state = _resolve([_layer("project", {"profiles": {}})])
        assert state.write_target == os.path.join(
            "/home/u", ".claude", "bootstrap.local.json")


# ---------------------------------------------------------------------------
# Selection layers
# ---------------------------------------------------------------------------


class TestSelectionLayers:
    def test_project_local_beats_user_local(self):
        layers = [
            _layer("user", {"profiles": {"a": {}, "b": {}}}),
            _layer("user_local", {"profile": "a"}),
            _layer("project_local", {"profile": "b"}),
        ]
        _, state = _resolve(layers)
        assert state.selected == "b"
        assert state.source == "/fixtures/project_local.json"

    @pytest.mark.parametrize("kind", ["legacy", "user", "project"])
    def test_a_committed_or_legacy_profile_key_is_ignored_with_a_warning(self, kind):
        layers = [_layer(kind, dict(ONE_PROFILE, profile="engineer"))]
        _, state = _resolve(layers)
        assert state.status == "unselected"
        assert state.selected is None
        assert any("/fixtures/%s.json" % kind in w for w in state.warnings)

    def test_a_non_string_selection_is_ignored_with_a_warning(self):
        _, state = _resolve([
            _layer("project", ONE_PROFILE),
            _layer("user_local", {"profile": 3}),
        ])
        assert state.status == "unselected"
        assert any("not a string" in w for w in state.warnings)

    def test_write_target_follows_a_project_declaration(self):
        _, state = _resolve([_layer("project", ONE_PROFILE)])
        assert state.write_target == os.path.join(
            "/proj", ".claude", "bootstrap.local.json")

    def test_write_target_is_user_local_for_a_user_declaration(self):
        _, state = _resolve([_layer("user", ONE_PROFILE)])
        assert state.write_target == os.path.join(
            "/home/u", ".claude", "bootstrap.local.json")


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


class TestInheritance:
    def test_diamond_applies_the_shared_ancestor_once_and_first(self):
        layers = [_layer("user", {"profiles": {
            "base": {"path_entries": ["/base"]},
            "left": {"extends": ["base"], "path_entries": ["/left"]},
            "right": {"extends": ["base"], "path_entries": ["/right"]},
            "full": {"extends": ["left", "right"], "path_entries": ["/full"]},
        }}), _layer("user_local", {"profile": "full"})]
        manifest, state = _resolve(layers)
        assert state.status == "selected"
        assert state.chain == ("base", "left", "right", "full")
        assert manifest["path_entries"] == ["/base", "/left", "/right", "/full"]

    def test_a_cycle_is_an_error(self):
        _, state = _resolve([_layer("user", {"profiles": {
            "a": {"extends": ["b"]},
            "b": {"extends": ["a"]},
        }})])
        assert state.status == "invalid"
        assert any("cycle" in e for e in state.errors)

    def test_self_extends_is_an_error(self):
        _, state = _resolve([_layer("user", {"profiles": {"a": {"extends": ["a"]}}})])
        assert state.status == "invalid"
        assert any("extends itself" in e for e in state.errors)

    def test_an_unknown_parent_is_an_error(self):
        _, state = _resolve([_layer("user", {"profiles": {
            "a": {"extends": ["ghost"]},
        }})])
        assert state.status == "invalid"
        assert any("ghost" in e for e in state.errors)

    def test_extends_must_be_a_list_of_names(self):
        _, state = _resolve([_layer("user", {"profiles": {"a": {"extends": "b"}}})])
        assert state.status == "invalid"
        assert any("'extends'" in e for e in state.errors)


# ---------------------------------------------------------------------------
# Declaration validation
# ---------------------------------------------------------------------------


class TestValidation:
    @pytest.mark.parametrize("bad", ["Engineer", "-lead", "a" * 33, "with space", ""])
    def test_invalid_profile_names_are_errors(self, bad):
        _, state = _resolve([_layer("user", {"profiles": {bad: {}}})])
        assert state.status == "invalid"
        assert any("invalid profile name" in e for e in state.errors)

    def test_none_is_reserved_as_a_profile_name(self):
        _, state = _resolve([_layer("user", {"profiles": {"none": {}}})])
        assert state.status == "invalid"
        assert any("reserved" in e for e in state.errors)

    @pytest.mark.parametrize("nested", ["profiles", "profile"])
    def test_a_nested_profile_key_inside_a_body_is_an_error(self, nested):
        _, state = _resolve([_layer("user", {"profiles": {
            "a": {nested: {"b": {}} if nested == "profiles" else "b"},
        }})])
        assert state.status == "invalid"
        assert any("do not nest" in e for e in state.errors)

    def test_a_non_string_description_is_an_error(self):
        _, state = _resolve([_layer("user", {"profiles": {"a": {"description": 7}}})])
        assert state.status == "invalid"
        assert any("description" in e for e in state.errors)

    def test_profiles_must_be_an_object(self):
        _, state = _resolve([_layer("user", {"profiles": ["a", "b"]})])
        assert state.status == "invalid"
        assert any("must be an object" in e for e in state.errors)

    def test_an_error_leaves_the_base_manifest_usable(self):
        manifest, state = _resolve([_layer("user", {
            "profiles": {"Bad Name": {}},
            "tools": [{"name": "uv"}],
        })])
        assert state.status == "invalid"
        assert manifest == {"tools": [{"name": "uv"}]}


# ---------------------------------------------------------------------------
# The effective manifest
# ---------------------------------------------------------------------------


def _every_status_case():
    """One layer set per status, so the strip rule is checked across all six."""
    return {
        # The no_profiles layers still carry a `profile` key, so this status is
        # covered by the strip rule rather than passing for want of a key.
        "no_profiles": [_layer("user", {"tools": [{"name": "uv"}]}),
                         _layer("user_local", {"profile": "engineer"})],
        "unselected": [_layer("user", ONE_PROFILE)],
        "none": [_layer("user", ONE_PROFILE), _layer("user_local", {"profile": "none"})],
        "selected": [_layer("user", ONE_PROFILE),
                      _layer("user_local", {"profile": "engineer"})],
        "unknown": [_layer("user", ONE_PROFILE),
                     _layer("user_local", {"profile": "ghost"})],
        "invalid": [_layer("user", {"profiles": {"a": {"extends": ["ghost"]}}}),
                     _layer("user_local", {"profile": "a"})],
    }


class TestEffectiveManifest:
    @pytest.mark.parametrize("status", sorted(_every_status_case()))
    def test_profile_keys_are_stripped_in_every_status(self, status):
        manifest, state = _resolve(_every_status_case()[status])
        assert state.status == status
        assert "profiles" not in manifest
        assert "profile" not in manifest

    def test_a_profile_overlay_merges_a_section_by_identity_key(self):
        layers = [
            _layer("user", {"tools": [
                {"name": "uv", "min_version": "1.0"},
                {"name": "git"},
            ]}),
            _layer("project", {"profiles": {"engineer": {"tools": [
                {"name": "uv", "min_version": "2.0"},
                {"name": "jq"},
            ]}}}),
            _layer("project_local", {"profile": "engineer"}),
        ]
        manifest, state = _resolve(layers)
        assert state.status == "selected"
        by_name = {t["name"]: t for t in manifest["tools"]}
        assert by_name["uv"]["min_version"] == "2.0"
        assert "jq" in by_name and "git" in by_name

    def test_profile_definitions_deep_merge_across_layers(self):
        layers = [
            _layer("user", {"profiles": {"engineer": {"description": "User."}}}),
            _layer("project", {"profiles": {"designer": {"description": "Proj."}}}),
        ]
        _, state = _resolve(layers)
        assert sorted(p.name for p in state.available) == ["designer", "engineer"]

    def test_profile_metadata_never_reaches_the_effective_manifest(self):
        layers = [
            _layer("user", {"profiles": {
                "base": {"path_entries": ["/base"]},
                "engineer": {"extends": ["base"], "description": "Eng.",
                              "path_entries": ["/eng"]},
            }}),
            _layer("user_local", {"profile": "engineer"}),
        ]
        manifest, _ = _resolve(layers)
        assert "extends" not in manifest and "description" not in manifest


# ---------------------------------------------------------------------------
# Question building
# ---------------------------------------------------------------------------


def _state_with(names, status="unselected", selected=None):
    return profiles.ProfileState(
        status=status,
        selected=selected,
        available=tuple(profiles.ProfileInfo(name=n, description="The %s set." % n)
                        for n in names),
    )


class TestBuildQuestion:
    def test_none_under_no_profiles(self):
        assert profiles.build_question(
            profiles.ProfileState(status="no_profiles"), "first_run") is None

    def test_first_run_leads_with_not_now(self):
        question = profiles.build_question(_state_with(["a", "b"]), "first_run")
        assert question["header"] == "Profile"
        assert question["multiSelect"] is False
        assert [o["label"] for o in question["options"]] == ["Not now", "a", "b"]

    def test_switch_leads_with_keep_current_and_omits_the_selection(self):
        state = _state_with(["a", "b"], status="selected", selected="a")
        question = profiles.build_question(state, "switch")
        assert [o["label"] for o in question["options"]] == ["Keep current", "b"]
        assert "'a'" in question["options"][0]["description"]

    def test_more_than_three_profiles_names_them_all_in_the_text(self):
        state = _state_with(["a", "b", "c", "d", "e"])
        question = profiles.build_question(state, "first_run")
        assert [o["label"] for o in question["options"]] == ["Not now", "a", "b", "c"]
        for name in ("a", "b", "c", "d", "e"):
            assert name in question["question"]
        assert "Other" in question["question"]

    def test_none_is_always_reachable_through_other(self):
        question = profiles.build_question(_state_with(["a"]), "first_run")
        assert "'none'" in question["question"]


class TestSanitizeDescription:
    def test_non_ascii_is_stripped(self):
        # Escaped rather than literal: tracked files in this repo are ASCII.
        assert profiles.sanitize_description("caf\u00e9 \u2014 ok") == "caf ok"

    def test_newlines_become_a_single_space(self):
        assert profiles.sanitize_description("one\ntwo\tthree") == "one two three"

    def test_truncation_has_no_ellipsis(self):
        text = "".join("abcdefghij") * 25
        cleaned = profiles.sanitize_description(text)
        assert len(cleaned) == profiles.DESCRIPTION_MAX
        assert cleaned == text[:profiles.DESCRIPTION_MAX]
        assert "..." not in cleaned and not cleaned.endswith((".", "~"))

    def test_a_non_string_description_is_empty(self):
        assert profiles.sanitize_description(None) == ""


class TestPromptDirective:
    def test_it_carries_the_run_command_and_the_question_json(self):
        state = _state_with(["engineer"])
        directive = profiles.prompt_directive(state, "/root/scripts/bootstrap.sh")
        assert 'bash "/root/scripts/bootstrap.sh" profile set <name>' in directive
        assert 'profile set none' in directive
        embedded = json.dumps(
            profiles.build_question(state, "first_run"), sort_keys=True)
        assert embedded in directive
        assert "AskUserQuestion" in directive
        assert "/bootstrap profile" in directive
        assert "uninstalls nothing" in directive

    def test_it_is_empty_under_no_profiles(self):
        state = profiles.ProfileState(status="no_profiles")
        assert profiles.prompt_directive(state, "/root/bootstrap.sh") == ""


# ---------------------------------------------------------------------------
# Prompt gating
# ---------------------------------------------------------------------------


ATTENDED = {"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_SESSION_ID": "sess-1"}


class TestShouldPrompt:
    @pytest.mark.parametrize("status,expected", [
        ("unselected", True),
        ("unknown", True),
        ("selected", False),
        ("none", False),
        ("no_profiles", False),
        ("invalid", False),
    ])
    def test_only_an_open_choice_is_promptable(self, tmp_path, status, expected):
        state = profiles.ProfileState(status=status)
        assert profiles.should_prompt(state, ATTENDED, str(tmp_path)) is expected

    @pytest.mark.parametrize("env,expected", [
        ({"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_SESSION_ID": "s"}, True),
        ({"CLAUDE_CODE_SESSION_ATTENDED": "0", "CLAUDE_CODE_SESSION_ID": "s"}, False),
        ({"CLAUDE_CODE_SESSION_ID": "s"}, False),
        ({"CLAUDE_CODE_SESSION_ATTENDED": "1"}, False),
        ({"CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_SESSION_ID": "  "}, False),
    ])
    def test_the_attended_and_session_id_matrix(self, tmp_path, env, expected):
        state = profiles.ProfileState(status="unselected")
        assert profiles.should_prompt(state, env, str(tmp_path)) is expected

    def test_a_marker_for_this_session_stops_a_second_prompt(self, tmp_path):
        state = profiles.ProfileState(status="unselected")
        profiles.mark_prompted(ATTENDED, str(tmp_path))
        assert profiles.should_prompt(state, ATTENDED, str(tmp_path)) is False

    def test_a_recent_marker_for_another_session_blocks_the_prompt(self, tmp_path):
        """F13: two sessions starting together must not both ask."""
        state = profiles.ProfileState(status="unselected")
        profiles.mark_prompted(
            {"CLAUDE_CODE_SESSION_ID": "other-session"}, str(tmp_path))
        assert profiles.should_prompt(state, ATTENDED, str(tmp_path)) is False

    def test_an_old_marker_for_another_session_does_not_block(self, tmp_path):
        state = profiles.ProfileState(status="unselected")
        path = profiles.mark_prompted(
            {"CLAUDE_CODE_SESSION_ID": "other-session"}, str(tmp_path))
        stale = time.time() - profiles.PROMPT_GUARD_SECONDS - 60
        os.utime(path, (stale, stale))
        assert profiles.should_prompt(state, ATTENDED, str(tmp_path)) is True

    def test_a_missing_marker_directory_is_not_a_blocker(self, tmp_path):
        state = profiles.ProfileState(status="unselected")
        missing = str(tmp_path / "never-created")
        assert profiles.should_prompt(state, ATTENDED, missing) is True


class TestMarkPrompted:
    def test_the_session_id_is_sanitized_into_the_file_name(self, tmp_path):
        env = {"CLAUDE_CODE_SESSION_ID": "a/b*c:d ../e"}
        path = profiles.mark_prompted(env, str(tmp_path))
        name = os.path.basename(path)
        assert os.path.dirname(path) == str(tmp_path)
        assert name == "a_b_c_d_.._e"
        assert os.path.isfile(path)

    @pytest.mark.parametrize("sid", [None, "", "   ", ".", "..", 7])
    def test_an_unusable_session_id_writes_nothing(self, tmp_path, sid):
        assert profiles.mark_prompted({"CLAUDE_CODE_SESSION_ID": sid},
                                       str(tmp_path)) is None
        assert os.listdir(str(tmp_path)) == []

    def test_markers_older_than_seven_days_are_pruned(self, tmp_path):
        old = tmp_path / "ancient-session"
        old.write_text("0\n")
        stale = time.time() - profiles.MARKER_TTL_SECONDS - 60
        os.utime(str(old), (stale, stale))

        fresh = tmp_path / "recent-session"
        fresh.write_text("0\n")

        profiles.mark_prompted(ATTENDED, str(tmp_path))
        assert not old.exists()
        assert fresh.exists()
        assert (tmp_path / "sess-1").exists()


class TestAttendedSignalMissing:
    def test_absent_is_distinguished_from_zero(self):
        assert profiles.attended_signal_missing({}) is True
        assert profiles.attended_signal_missing(
            {"CLAUDE_CODE_SESSION_ATTENDED": "0"}) is False


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestWriteSelection:
    def test_it_creates_a_missing_file(self, tmp_path):
        path = str(tmp_path / "nested" / "bootstrap.local.json")
        profiles.write_selection(path, "engineer")
        assert json.loads(open(path).read()) == {"profile": "engineer"}
        assert open(path).read().endswith("\n")

    def test_it_preserves_every_other_key(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text(json.dumps({"tools": [{"name": "uv"}], "profile": "old"}))
        profiles.write_selection(str(path), "engineer")
        assert json.loads(path.read_text()) == {
            "tools": [{"name": "uv"}], "profile": "engineer"}

    def test_none_removes_the_key_only(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text(json.dumps({"tools": [], "profile": "engineer"}))
        profiles.write_selection(str(path), None)
        assert json.loads(path.read_text()) == {"tools": []}

    def test_none_on_a_file_without_the_key_writes_nothing(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text('{"tools": []}')
        before = path.read_text()
        profiles.write_selection(str(path), None)
        assert path.read_text() == before

    def test_an_unparseable_file_is_refused_without_writing(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text("{ not json")
        with pytest.raises(profiles.ProfileWriteError):
            profiles.write_selection(str(path), "engineer")
        assert path.read_text() == "{ not json"

    def test_a_non_object_file_is_refused_without_writing(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text("[1, 2]")
        with pytest.raises(profiles.ProfileWriteError):
            profiles.write_selection(str(path), "engineer")
        assert path.read_text() == "[1, 2]"

    def test_an_empty_file_is_treated_as_an_empty_object(self, tmp_path):
        path = tmp_path / "bootstrap.local.json"
        path.write_text("")
        profiles.write_selection(str(path), "engineer")
        assert json.loads(path.read_text()) == {"profile": "engineer"}


def _git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class TestEnsureVcsExcluded:
    def test_outside_a_git_repository_it_is_a_no_op(self, tmp_path):
        assert profiles.ensure_vcs_excluded(str(tmp_path)) is None

    def test_it_writes_its_own_header_into_info_exclude(self, tmp_path):
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _git("init", "-q", cwd=repo)

        detail = profiles.ensure_vcs_excluded(repo)
        assert detail

        exclude = os.path.join(repo, ".git", "info", "exclude")
        content = open(exclude).read()
        assert ".claude/bootstrap.local.json" in content
        assert "generated profile selection" in content
        # The Perforce default header belongs to the other caller.
        assert "generated Perforce ignore file" not in content

    def test_an_already_ignored_path_is_reported_without_a_second_rule(self, tmp_path):
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _git("init", "-q", cwd=repo)
        with open(os.path.join(repo, ".gitignore"), "w") as handle:
            handle.write(".claude/bootstrap.local.json\n")

        detail = profiles.ensure_vcs_excluded(repo)
        assert "already effective" in detail
        assert not os.path.exists(os.path.join(repo, ".git", "info", "exclude")) or (
            "generated profile selection"
            not in open(os.path.join(repo, ".git", "info", "exclude")).read()
        )
