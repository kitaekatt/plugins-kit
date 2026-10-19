"""`uninstall: true` plugins[] entries: the resolver, the engine step, the writers.

A plugins[] entry may carry `uninstall: true` to say "remove this plugin's
local-scope install for this project". It is the declarative half of a profile
switch: the switch writes the entries into the project's bootstrap.local.json
and every later pass carries them out, so no hidden record of what a profile
installed is needed.

Layers:
  * plugin_tiers.resolve_uninstall_entries -- pure decisions;
  * profiles.plan_switch_uninstalls / apply_uninstall_edit / write_* -- which
    entries a switch writes and how they are merged into the local file;
  * engine._process_plugin_uninstall_entries -- inventory in, uninstall out,
    with the real layered loader for the merge-by-ref conflict case.

The CLI verbs that call the writers are covered in test_bootstrap_cli_profile.py.
Nothing here runs the real `claude` CLI or touches the real HOME.
"""

import json

import pytest

import bootstrap_lib.engine as engine
from bootstrap_lib import marketplace_lifecycle as lifecycle
from bootstrap_lib import plugin_tiers as tiers
from bootstrap_lib import profiles
from bootstrap_lib.marketplace_lifecycle import (
    Install, InventoryError, LifecycleResult,
)

PROJ = "D:/proj"
OLD = "mkt:old"
REQ = "mkt:req"
REC = "mkt:rec"
NOREC = "mkt:norec"


def tier_entry(ref, tier, **extra):
    data = {"ref": ref, "tier": tier, "install": "manual"}
    if tier != "required":
        data["why_install"] = "You will want %s." % ref
        data["why_skip"] = "You may not need %s." % ref
    data.update(extra)
    return data


def un(ref, **extra):
    return dict({"ref": ref, "uninstall": True}, **extra)


def rec(ref, scope, installed=True, path=PROJ):
    return {"ref": ref, "scope": scope, "installed": installed,
            "project_path": None if scope == "user" else path}


def local_install(**kw):
    kw.setdefault("project_path", PROJ)
    return Install(scope="local", install_path="/cache/x", source="registry", **kw)


def user_install(**kw):
    return Install(scope="user", project_path="", install_path="/cache/x",
                   source="registry", **kw)


def project_install(**kw):
    kw.setdefault("project_path", PROJ)
    return Install(scope="project", install_path="/cache/x", source="registry", **kw)


# --------------------------------------------------------------------------- #
# The pure resolver
# --------------------------------------------------------------------------- #


class TestResolveUninstallEntries:

    def resolve(self, entries, inventory=(), answers=None, active=True):
        return tiers.resolve_uninstall_entries(
            entries, answers or {}, inventory, profile_active=active)

    def test_a_local_install_is_ordered_uninstalled_at_local_scope(self):
        res = self.resolve([un(OLD)], [rec(OLD, "local")])
        assert [(a.ref, a.scope) for a in res.uninstalls] == [(OLD, "local")]
        assert res.notices == () and res.conflicts == ()

    def test_two_local_records_for_one_project_are_one_order(self):
        res = self.resolve([un(OLD)], [rec(OLD, "local"), rec(OLD, "local")])
        assert len(res.uninstalls) == 1

    def test_no_install_is_satisfied_and_orders_nothing(self):
        res = self.resolve([un(OLD)], [])
        assert res.uninstalls == () and res.notices == () and res.conflicts == ()
        assert [a.ref for a in res.satisfied] == [OLD]

    def test_a_dormant_record_does_not_count_as_installed(self):
        res = self.resolve([un(OLD)], [rec(OLD, "local", installed=False)])
        assert res.uninstalls == ()
        assert [a.ref for a in res.satisfied] == [OLD]

    def test_user_and_project_installs_are_a_notice_never_an_uninstall(self):
        res = self.resolve([un(OLD)], [rec(OLD, "user"), rec(OLD, "project")])
        assert res.uninstalls == ()
        assert [(a.ref, a.scope) for a in res.notices] == [(OLD, "user,project")]
        assert res.satisfied == ()

    def test_a_local_install_alongside_a_user_install_removes_only_the_local_one(self):
        res = self.resolve([un(OLD)], [rec(OLD, "local"), rec(OLD, "user")])
        assert [(a.scope) for a in res.uninstalls] == ["local"]
        assert [a.scope for a in res.notices] == ["user"]

    def test_the_entry_is_rewritten_manual_so_the_plugin_phase_cannot_install_it(self):
        res = self.resolve([un(OLD)], [])
        assert res.plugins[0]["install"] == "manual"
        assert res.plugins[0]["uninstall"] is True

    @pytest.mark.parametrize("entry", [
        un(REQ, install="auto"),
        tier_entry(REQ, "required", uninstall=True),
        tier_entry(REC, "recommended", uninstall=True),
    ])
    def test_a_plugin_the_manifest_wants_is_a_conflict_and_nothing_is_ordered(
            self, entry):
        res = self.resolve([entry], [rec(entry["ref"], "local")])
        assert res.uninstalls == ()
        assert [a.ref for a in res.conflicts] == [entry["ref"]]
        assert "not uninstalled" in res.conflicts[0].reason

    def test_a_conflicting_entry_keeps_the_install_mode_the_rest_of_the_manifest_set(self):
        entry = un(REQ, install="auto")
        assert self.resolve([entry], [rec(REQ, "local")]).plugins[0]["install"] == "auto"

    def test_a_recommended_plugin_the_user_declined_is_uninstalled(self):
        entry = tier_entry(REC, "recommended", uninstall=True)
        res = self.resolve([entry], [rec(REC, "local")],
                           answers={REC: {"want": False}})
        assert [a.ref for a in res.uninstalls] == [REC]
        assert res.conflicts == ()

    def test_a_not_recommended_plugin_is_uninstalled_unless_kept_by_a_yes(self):
        entry = tier_entry(NOREC, "not_recommended", uninstall=True)
        installed = [rec(NOREC, "local")]
        assert [a.ref for a in self.resolve([entry], installed).uninstalls] == [NOREC]
        kept = self.resolve([entry], installed, answers={NOREC: {"want": True}})
        assert kept.uninstalls == () and [a.ref for a in kept.conflicts] == [NOREC]

    def test_tiers_are_inert_without_a_selected_profile(self):
        entry = tier_entry(REQ, "required", uninstall=True)
        res = self.resolve([entry], [rec(REQ, "local")], active=False)
        assert [a.ref for a in res.uninstalls] == [REQ]
        assert res.conflicts == ()

    def test_uninstall_false_and_absent_entries_are_left_alone(self):
        entries = [dict(un(OLD), uninstall=False), {"ref": REQ, "install": "auto"}]
        res = self.resolve(entries, [rec(OLD, "local"), rec(REQ, "local")])
        assert res.actions == ()
        assert list(res.plugins) == entries

    @pytest.mark.parametrize("bad", ["yes", 1, [], {}])
    def test_a_non_boolean_uninstall_is_an_error(self, bad):
        with pytest.raises(tiers.PluginTierError, match="true or false"):
            self.resolve([{"ref": OLD, "uninstall": bad}])

    def test_an_entry_with_no_ref_is_an_error(self):
        with pytest.raises(tiers.PluginTierError, match="no ref"):
            self.resolve([{"uninstall": True}])


# --------------------------------------------------------------------------- #
# The engine step
# --------------------------------------------------------------------------- #


class Calls:
    def __init__(self):
        self.installs = {}           # ref -> [Install] or an Exception to raise
        self.uninstalls = []
        self.results = {}            # (ref, scope) -> [LifecycleResult]


@pytest.fixture
def world(monkeypatch):
    calls = Calls()

    def fake_installs(ref, project_dir, include_dormant=False):
        found = calls.installs.get(ref, [])
        if isinstance(found, Exception):
            raise found
        return list(found)

    def fake_uninstall(ref, installs, project_dir):
        scope = installs[0].scope if installs else None
        calls.uninstalls.append((ref, [i.scope for i in installs], project_dir))
        return calls.results.get(
            (ref, scope), [LifecycleResult(passed=True, ref=ref, message="uninstalled")])

    monkeypatch.setattr(lifecycle, "plugin_installs", fake_installs)
    monkeypatch.setattr(lifecycle, "uninstall_installs", fake_uninstall)
    monkeypatch.setattr(lifecycle, "resolve_claude_cli", lambda: "/usr/bin/claude")
    return calls


def selected(answers=()):
    return profiles.ProfileState(
        status="selected", selected="designer", chain=("designer",),
        plugin_answers=tuple(
            profiles.PluginAnswer(ref=ref, want=want) for ref, want in answers))


NO_PROFILE = profiles.ProfileState(status="no_profiles")


def run(manifest, state=NO_PROFILE, project_dir=PROJ):
    action, ok, quiet = [], [], []
    new, failures = engine._process_plugin_uninstall_entries(
        manifest, state, project_dir, action, ok, quiet)
    return new, failures, [str(e) for e in action], [str(e) for e in ok], \
        [str(e) for e in quiet]


class TestEngineStep:

    def test_a_manifest_without_uninstall_entries_is_returned_untouched(self, world):
        manifest = {"plugins": [{"ref": REQ, "install": "auto"}]}
        new, failures, action, ok, quiet = run(manifest)
        assert new is manifest
        assert (failures, action, ok, quiet) == ([], [], [], [])

    def test_the_entry_uninstalls_the_local_install_at_local_scope(self, world):
        world.installs[OLD] = [local_install()]

        new, failures, action, _ok, _q = run({"plugins": [un(OLD)]})

        assert failures == []
        assert world.uninstalls == [(OLD, ["local"], PROJ)]
        assert any("uninstalled at local scope (uninstall: true)" in l for l in action)
        assert new["plugins"][0]["install"] == "manual"

    def test_it_is_idempotent_once_the_install_is_gone(self, world):
        world.installs[OLD] = [local_install()]
        run({"plugins": [un(OLD)]})
        world.installs[OLD] = []

        _new, failures, action, ok, _q = run({"plugins": [un(OLD)]})

        assert len(world.uninstalls) == 1
        assert failures == [] and action == []
        assert any("uninstall: true satisfied" in l for l in ok)

    def test_a_satisfied_entry_is_quiet_and_never_a_failure(self, world):
        _new, failures, action, ok, quiet = run({"plugins": [un(OLD)]})
        assert world.uninstalls == []
        assert failures == [] and action == [] and quiet == []
        assert len(ok) == 1

    def test_user_and_project_installs_get_a_notice_and_no_uninstall(self, world):
        world.installs[OLD] = [user_install(), project_install()]

        _new, failures, action, ok, _q = run({"plugins": [un(OLD)]})

        assert world.uninstalls == []
        assert failures == [] and ok == []
        notice = [l for l in action if "cannot remove" in l]
        assert len(notice) == 1 and "user,project-scope" in notice[0]

    def test_a_local_and_a_user_install_remove_only_the_local_one(self, world):
        world.installs[OLD] = [local_install(), user_install()]

        _new, failures, action, _ok, _q = run({"plugins": [un(OLD)]})

        assert failures == []
        assert world.uninstalls == [(OLD, ["local"], PROJ)]
        assert any("cannot remove its user-scope install" in l for l in action)

    @pytest.mark.parametrize("entry", [
        un(REQ, install="auto"),
        tier_entry(REQ, "required", uninstall=True),
    ])
    def test_a_wanted_plugin_warns_and_nothing_is_uninstalled(self, world, entry):
        world.installs[REQ] = [local_install()]

        for _ in range(2):  # no flapping: the same answer every pass
            new, failures, action, _ok, _q = run(
                {"plugins": [entry]}, selected())
            assert failures == []
            assert any("conflicts" in l and REQ in l for l in action)

        assert world.uninstalls == []
        assert new["plugins"][0].get("install") == entry["install"]

    def test_a_failed_uninstall_is_a_failure_that_recurs(self, world):
        world.installs[OLD] = [local_install()]
        world.results[(OLD, "local")] = [LifecycleResult(
            passed=False, ref=OLD, message="uninstall failed (boom)", detail="raw")]

        for _ in range(2):
            _new, failures, action, _ok, quiet = run({"plugins": [un(OLD)]})
            assert len(failures) == 1
            assert "uninstall: true" in failures[0]["agent_msg"]
            assert "tier not_recommended" not in failures[0]["agent_msg"]
            assert any("uninstall failed" in l for l in action)
            assert any("raw" in l for l in quiet)

        assert len(world.uninstalls) == 2

    def test_an_unreadable_inventory_is_a_failure_and_the_entry_stays_manual(self, world):
        world.installs[OLD] = InventoryError("registry is broken")

        new, failures, action, _ok, _q = run({"plugins": [un(OLD)]})

        assert world.uninstalls == []
        assert len(failures) == 1 and "registry is broken" in failures[0]["message"]
        assert any("state unreadable" in l or "unreadable" in l for l in action)
        assert new["plugins"][0]["install"] == "manual"

    def test_a_missing_claude_cli_leaves_the_uninstall_pending(self, world, monkeypatch):
        monkeypatch.setattr(lifecycle, "resolve_claude_cli", lambda: None)
        world.installs[OLD] = [local_install()]

        _new, failures, action, _ok, _q = run({"plugins": [un(OLD)]})

        assert failures == [] and world.uninstalls == []
        assert any("uninstall(s) pending" in l for l in action)

    def test_a_malformed_entry_is_one_failure_and_every_marked_entry_goes_manual(
            self, world):
        world.installs[OLD] = [local_install()]
        manifest = {"plugins": [un(OLD), {"ref": REQ, "uninstall": "yes"},
                                {"ref": "mkt:other", "install": "auto"}]}

        new, failures, _action, _ok, _q = run(manifest)

        assert world.uninstalls == []
        assert len(failures) == 1 and "true or false" in failures[0]["message"]
        assert [e.get("install") for e in new["plugins"]] == ["manual", "manual", "auto"]

    def test_the_tracked_settings_file_is_never_written(self, world, tmp_path):
        project = tmp_path / "proj"
        (project / ".claude").mkdir(parents=True)
        settings = project / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"old@mkt": True}}))
        before = settings.read_bytes()
        world.installs[OLD] = [project_install(project_path=str(project))]

        run({"plugins": [un(OLD)]}, project_dir=str(project))

        assert world.uninstalls == []
        assert settings.read_bytes() == before


# --------------------------------------------------------------------------- #
# The real layered loader: a local entry merges onto the profile's entry by ref
# --------------------------------------------------------------------------- #


@pytest.fixture
def layered(tmp_path, monkeypatch, world):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    (home / ".claude").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (project / ".claude" / "bootstrap.json").write_text(json.dumps({
        "profiles": {
            "designer": {"plugins": [tier_entry(REQ, "required")]},
            "engineer": {"plugins": []},
        }}))
    return project


def _local(project, payload):
    (project / ".claude" / "bootstrap.local.json").write_text(json.dumps(payload))


def _passes(project):
    manifest, _errors, state = engine._load_layered_manifests_ex(str(project))
    action, ok, quiet = [], [], []
    manifest, tier_failures = engine._process_plugin_tiers(
        manifest, state, str(project), action, ok, quiet)
    manifest, failures = engine._process_plugin_uninstall_entries(
        manifest, state, str(project), action, ok, quiet)
    return manifest, tier_failures + failures, action


class TestLayeredMerge:

    def test_a_local_entry_merges_onto_the_selected_profiles_tiered_entry(
            self, layered, world):
        # The hypothesis the conflict rule rests on: layers union plugins[] by
        # ref, so the local `uninstall` and the profile's `tier` are ONE entry.
        _local(layered, {"profile": "designer", "plugins": [un(REQ)]})
        world.installs[REQ] = [local_install(project_path=str(layered))]

        manifest, failures, action = _passes(layered)

        entries = [e for e in manifest["plugins"] if e["ref"] == REQ]
        assert len(entries) == 1
        assert entries[0]["tier"] == "required" and entries[0]["uninstall"] is True
        assert failures == [] and world.uninstalls == []
        assert any("conflicts" in l for l in action)

    def test_the_same_entry_uninstalls_once_the_profile_no_longer_lists_the_plugin(
            self, layered, world):
        _local(layered, {"profile": "engineer", "plugins": [un(REQ)]})
        world.installs[REQ] = [local_install(project_path=str(layered))]

        _manifest, failures, _action = _passes(layered)

        assert failures == []
        assert world.uninstalls == [(REQ, ["local"], str(layered))]

    def test_it_also_runs_with_no_profile_selected_at_all(self, layered, world):
        _local(layered, {"profile": "none", "plugins": [un(REQ)]})
        world.installs[REQ] = [local_install(project_path=str(layered))]

        _passes(layered)

        assert len(world.uninstalls) == 1


# --------------------------------------------------------------------------- #
# What a profile switch writes
# --------------------------------------------------------------------------- #


def state_of(status, selected_name=None):
    return profiles.ProfileState(
        status=status, selected=selected_name if status == "selected" else None)


def manifest_of(*entries):
    return {"plugins": list(entries)}


class TestPlanSwitchUninstalls:

    def plan(self, old, old_entries, new, new_entries):
        return profiles.plan_switch_uninstalls(
            old, manifest_of(*old_entries), new, manifest_of(*new_entries))

    def test_old_required_and_recommended_plugins_the_new_chain_omits_are_added(self):
        old = [tier_entry(REQ, "required"), tier_entry(REC, "recommended"),
               tier_entry(NOREC, "not_recommended")]
        edit = self.plan(state_of("selected", "a"), old, state_of("selected", "b"), [])
        assert edit.add == (REQ, REC)
        assert edit.remove == ()

    def test_a_plugin_the_new_chain_lists_in_any_tier_is_not_added(self):
        old = [tier_entry(REQ, "required"), tier_entry(REC, "recommended")]
        new = [tier_entry(REQ, "recommended"), tier_entry(REC, "not_recommended")]
        edit = self.plan(state_of("selected", "a"), old, state_of("selected", "b"), new)
        assert edit.add == ()
        assert edit.remove == (REQ,)

    def test_a_base_plugin_is_never_added(self):
        # The effective manifests both carry the base list, so a base entry the
        # old profile ALSO tiers is still listed on the new side.
        old = [tier_entry(REQ, "required")]
        new = [{"ref": REQ, "install": "auto"}]
        edit = self.plan(state_of("selected", "a"), old, state_of("selected", "b"), new)
        assert edit.add == ()

    @pytest.mark.parametrize("status", ["none", "unselected"])
    def test_leaving_for_none_or_unset_adds_every_old_required_or_recommended(
            self, status):
        old = [tier_entry(REQ, "required")]
        edit = self.plan(state_of("selected", "a"), old, state_of(status), [])
        assert edit.add == (REQ,) and edit.remove == ()

    @pytest.mark.parametrize("old_status", ["none", "unselected", "no_profiles", "unknown"])
    def test_nothing_is_added_when_no_profile_was_selected_before(self, old_status):
        new = [tier_entry(REQ, "required")]
        edit = self.plan(state_of(old_status), [], state_of("selected", "b"), new)
        assert edit.add == ()
        assert edit.remove == (REQ,)

    def test_reselecting_the_same_profile_adds_nothing(self):
        # Even if the effective manifests disagree (a layer edited between the
        # two reads), the same selection is not a switch.
        old = [tier_entry(REQ, "required")]
        edit = self.plan(state_of("selected", "a"), old, state_of("selected", "a"), [])
        assert edit.add == ()

    def test_entries_are_unique_and_ordered(self):
        old = [tier_entry(REQ, "required"), tier_entry(REC, "recommended"),
               tier_entry(REQ, "required")]
        edit = self.plan(state_of("selected", "a"), old, state_of("none"), [])
        assert edit.add == (REQ, REC)


class TestApplyUninstallEdit:

    def test_adds_new_entries_and_marks_existing_ones(self):
        existing = {"tools": [1], "plugins": [{"ref": REC, "install": "manual"}]}

        change = profiles.apply_uninstall_edit(
            existing, profiles.UninstallEdit(add=(REQ, REC)))

        assert existing["plugins"] == [
            {"ref": REC, "install": "manual", "uninstall": True},
            {"ref": REQ, "uninstall": True}]
        assert existing["tools"] == [1]
        assert change.written == (REQ, REC) and change.removed == ()

    def test_is_idempotent(self):
        existing = {"plugins": [un(REQ)]}
        change = profiles.apply_uninstall_edit(
            existing, profiles.UninstallEdit(add=(REQ,)))
        assert not change
        assert existing == {"plugins": [un(REQ)]}

    def test_removes_a_bare_entry_and_only_unmarks_a_richer_one(self):
        existing = {"plugins": [un(REQ), un(REC, min_version="1"), un(OLD)]}

        change = profiles.apply_uninstall_edit(
            existing, profiles.UninstallEdit(remove=(REQ, REC)))

        assert existing["plugins"] == [{"ref": REC, "min_version": "1"}, un(OLD)]
        assert change.removed == (REQ, REC)

    def test_creates_no_plugins_key_when_there_is_nothing_to_do(self):
        existing = {"profile": "x"}
        profiles.apply_uninstall_edit(existing, profiles.UninstallEdit(remove=(REQ,)))
        profiles.apply_uninstall_edit(existing, None)
        assert existing == {"profile": "x"}

    def test_a_plugins_value_that_is_not_a_list_is_refused_untouched(self):
        existing = {"plugins": {"ref": REQ}}
        with pytest.raises(profiles.ProfileWriteError, match="not a list"):
            profiles.apply_uninstall_edit(
                existing, profiles.UninstallEdit(add=(REQ,)), "/x/local.json")
        assert existing == {"plugins": {"ref": REQ}}

    def test_non_dict_items_are_preserved(self):
        existing = {"plugins": ["junk", un(REQ)]}
        profiles.apply_uninstall_edit(existing, profiles.UninstallEdit(remove=(REQ,)))
        assert existing["plugins"] == ["junk"]


class TestWriters:

    def read(self, path):
        return json.loads(path.read_text())

    def test_write_selection_applies_the_edit_in_the_same_write(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a", "tools": [1]}))

        change = profiles.write_selection(
            path, "b", uninstall_edit=profiles.UninstallEdit(add=(REQ,)))

        assert self.read(path) == {
            "profile": "b", "tools": [1], "plugins": [un(REQ)]}
        assert change.written == (REQ,)

    def test_write_selection_writes_for_an_edit_alone(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a"}))
        profiles.write_selection(
            path, "a", uninstall_edit=profiles.UninstallEdit(add=(REQ,)))
        assert self.read(path)["plugins"] == [un(REQ)]

    def test_write_selection_without_an_edit_is_unchanged(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a"}))
        before = path.read_bytes()
        assert not profiles.write_selection(path, "a")
        assert path.read_bytes() == before

    def test_write_selection_with_answers_applies_the_edit(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a", "plugins": [un(REC)]}))

        change = profiles.write_selection_with_answers(
            path, "b", {REQ: {"want": True}},
            uninstall_edit=profiles.UninstallEdit(add=(OLD,), remove=(REC,)))

        data = self.read(path)
        assert data["profile"] == "b" and data["plugins"] == [un(OLD)]
        assert data["plugin_choices"]["profile"] == "b"
        assert change.written == (OLD,) and change.removed == (REC,)

    def test_clear_selection_applies_the_edit(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a"}))
        change = profiles.clear_selection(
            path, uninstall_edit=profiles.UninstallEdit(add=(REQ,)))
        assert self.read(path) == {"profile": "unset", "plugins": [un(REQ)]}
        assert change.written == (REQ,)

    def test_a_refused_edit_leaves_the_file_byte_identical(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a", "plugins": {}}))
        before = path.read_bytes()
        for call in (
            lambda: profiles.write_selection(
                path, "b", uninstall_edit=profiles.UninstallEdit(add=(REQ,))),
            lambda: profiles.write_selection_with_answers(
                path, "b", {}, uninstall_edit=profiles.UninstallEdit(add=(REQ,))),
            lambda: profiles.clear_selection(
                path, uninstall_edit=profiles.UninstallEdit(add=(REQ,))),
        ):
            with pytest.raises(profiles.ProfileWriteError):
                call()
        assert path.read_bytes() == before

    def test_write_uninstall_entries_only_touches_plugins(self, tmp_path):
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"profile": "a"}))
        change = profiles.write_uninstall_entries(
            path, profiles.UninstallEdit(add=(REQ,)))
        assert self.read(path) == {"profile": "a", "plugins": [un(REQ)]}
        assert change.written == (REQ,)
        before = path.read_bytes()
        assert not profiles.write_uninstall_entries(
            path, profiles.UninstallEdit(add=(REQ,)))
        assert path.read_bytes() == before
