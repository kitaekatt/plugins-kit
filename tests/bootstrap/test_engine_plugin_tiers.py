"""Engine wiring for profile plugin tiers (U3).

plugin_tiers.py (the pure resolver) and marketplace_lifecycle.plugin_installs /
uninstall_installs are covered by their own test files. This file covers what
the engine adds: building the inventory for every tiered ref, replacing the
plugins list with the resolver's rewrite, carrying out recorded removals,
making blocked removals visible every pass, containing an unreadable inventory,
and the _phase_plugins changes the tiers need (an enabled: false entry is never
installed; a tier-ordered install is made even when the registry holds another
project's record; a local-scope install does not run the registry scope sync).

Nothing here runs the real `claude` CLI: install, uninstall and the CLI probe
are patched on marketplace_lifecycle, which the engine imports at call time.
"""

import json
import os

import pytest

import bootstrap_lib.engine as engine
from bootstrap_lib import marketplace_lifecycle as lifecycle
from bootstrap_lib import profiles
from bootstrap_lib.marketplace_lifecycle import (
    Install, InventoryError, LifecycleResult,
)

PROJ = "D:/proj"
REC = "mkt:rec"
NOT_REC = "mkt:norec"
REQ = "mkt:req"


def tier_entry(ref, tier, **extra):
    data = {"ref": ref, "tier": tier, "install": "manual"}
    if tier != "required":
        data["why_install"] = "You will want %s." % ref
        data["why_skip"] = "You may not need %s." % ref
    data.update(extra)
    return data


def local_install(**kw):
    kw.setdefault("project_path", PROJ)
    return Install(scope="local", install_path="/cache/x", source="registry", **kw)


def user_install(**kw):
    return Install(scope="user", project_path="", install_path="/cache/x",
                   source="registry", **kw)


def project_install(**kw):
    kw.setdefault("project_path", PROJ)
    return Install(scope="project", install_path="/cache/x", source="registry", **kw)


def selected_state(answers=(), **kw):
    return profiles.ProfileState(
        status="selected", selected="designer", chain=("designer",),
        plugin_answers=tuple(
            profiles.PluginAnswer(ref=ref, want=want, scope=scope)
            for ref, want, scope in answers),
        **kw)


def plain_state(status):
    return profiles.ProfileState(status=status)


class Calls:
    """What the patched lifecycle functions were asked to do."""

    def __init__(self):
        self.inventory = []
        self.uninstalls = []


@pytest.fixture
def world(monkeypatch):
    """Patch the inventory, the uninstall and the CLI probe; record the calls."""
    calls = Calls()
    calls.installs = {}          # ref -> [Install] (or an Exception to raise)
    calls.uninstall_results = {}  # (ref, scope) -> [LifecycleResult]

    def fake_installs(ref, project_dir, include_dormant=False,
                      fold_project_case=False):
        calls.inventory.append((ref, project_dir))
        found = calls.installs.get(ref, [])
        if isinstance(found, Exception):
            raise found
        return list(found)

    def fake_uninstall(ref, installs, project_dir):
        scope = installs[0].scope if installs else None
        calls.uninstalls.append((ref, list(installs), project_dir))
        return calls.uninstall_results.get(
            (ref, scope), [LifecycleResult(passed=True, ref=ref, message="uninstalled")])

    monkeypatch.setattr(lifecycle, "plugin_installs", fake_installs)
    monkeypatch.setattr(lifecycle, "uninstall_installs", fake_uninstall)
    monkeypatch.setattr(lifecycle, "resolve_claude_cli", lambda: "/usr/bin/claude")
    return calls


def run(manifest, state, project_dir=PROJ):
    action, ok, quiet = [], [], []
    new_manifest, failures = engine._process_plugin_tiers(
        manifest, state, project_dir, action, ok, quiet)
    return new_manifest, failures, [str(e) for e in action], [str(e) for e in ok], \
        [str(e) for e in quiet]


def entry_of(manifest, ref):
    return next(e for e in manifest["plugins"] if e.get("ref") == ref)


# --------------------------------------------------------------------------- #
# Recorded removals
# --------------------------------------------------------------------------- #


class TestUninstallRecordedAnswer:
    def test_want_false_uninstalls_every_applicable_local_install(self, world):
        world.installs[NOT_REC] = [local_install(), local_install(version="2")]
        manifest = {"plugins": [tier_entry(NOT_REC, "not_recommended")]}

        new, failures, action, ok, _ = run(
            manifest, selected_state([(NOT_REC, False, None)]))

        assert failures == []
        assert len(world.uninstalls) == 1
        ref, installs, project_dir = world.uninstalls[0]
        assert ref == NOT_REC
        assert [i.scope for i in installs] == ["local", "local"]
        assert project_dir == PROJ
        assert any("uninstalled at local scope" in line for line in action)
        assert entry_of(new, NOT_REC)["install"] == "manual"

    def test_a_removal_needs_no_confirmation_beyond_the_recorded_answer(self, world):
        # The user's ruling: acting on a recorded answer never asks again. The
        # uninstall runs in the same call, with nothing to approve.
        world.installs[NOT_REC] = [local_install()]
        run({"plugins": [tier_entry(NOT_REC, "not_recommended")]},
            selected_state([(NOT_REC, False, None)]))
        assert [u[0] for u in world.uninstalls] == [NOT_REC]

    def test_want_true_or_unanswered_never_uninstalls(self, world):
        world.installs[NOT_REC] = [local_install()]
        manifest = {"plugins": [tier_entry(NOT_REC, "not_recommended")]}
        run(manifest, selected_state([(NOT_REC, True, None)]))
        run(manifest, selected_state([]))
        assert world.uninstalls == []

    def test_confirmed_user_scope_install_is_uninstalled(self, world):
        world.installs[NOT_REC] = [user_install()]
        run({"plugins": [tier_entry(NOT_REC, "not_recommended")]},
            selected_state([(NOT_REC, False, "user")]))
        assert [i.scope for i in world.uninstalls[0][1]] == ["user"]

    def test_unconfirmed_user_install_is_visible_and_never_ok(self, world):
        world.installs[NOT_REC] = [local_install(), user_install()]
        _, failures, action, ok, _ = run(
            {"plugins": [tier_entry(NOT_REC, "not_recommended")]},
            selected_state([(NOT_REC, False, None)]))

        assert failures == []
        assert [i.scope for i in world.uninstalls[0][1]] == ["local"]
        still = [line for line in action if "still installed" in line]
        assert len(still) == 1 and "double-check" in still[0]
        assert ok == []

    def test_project_install_is_reported_every_pass_and_never_removed(
            self, world, tmp_path):
        project = tmp_path / "proj"
        (project / ".claude").mkdir(parents=True)
        settings = project / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"norec@mkt": True}}))
        before = settings.read_bytes()
        world.installs[NOT_REC] = [project_install()]
        manifest = {"plugins": [tier_entry(NOT_REC, "not_recommended")]}
        state = selected_state([(NOT_REC, False, None)])

        for _ in range(2):  # the line recurs: nothing is ever recorded as done
            _, failures, action, ok, _ = run(manifest, state, str(project))
            assert failures == []
            assert any("still installed" in l and "shared settings.json" in l
                       for l in action)
            assert ok == []

        assert world.uninstalls == []
        assert settings.read_bytes() == before

    def test_failed_uninstall_is_a_failure_and_recurs_while_installed(self, world):
        world.installs[NOT_REC] = [local_install()]
        world.uninstall_results[(NOT_REC, "local")] = [
            LifecycleResult(passed=False, ref=NOT_REC, message="uninstall failed (boom)",
                            detail="raw cli output")]
        manifest = {"plugins": [tier_entry(NOT_REC, "not_recommended")]}
        state = selected_state([(NOT_REC, False, None)])

        for _ in range(2):  # the inventory still shows it, so the order repeats
            _, failures, action, _, quiet = run(manifest, state)
            assert len(failures) == 1
            failure = failures[0]
            assert failure["type"] == "plugin" and failure["ref"] == NOT_REC
            assert "uninstall" in failure["agent_msg"]
            assert "claude plugin uninstall norec@mkt --scope local" in failure["agent_msg"]
            # Checkable, not self-certifying: names the tracked file and makes
            # no claim of user agreement as authority.
            assert ".claude/bootstrap.json" in failure["agent_msg"]
            assert "recorded profile answer" not in failure["agent_msg"]
            assert "only if they agree" in failure["agent_msg"]
            assert any("uninstall at local scope failed" in l for l in action)
            assert any("raw cli output" in q for q in quiet)
        assert len(world.uninstalls) == 2

    def test_claude_cli_unavailable_stands_down_without_a_failure(
            self, world, monkeypatch):
        monkeypatch.setattr(lifecycle, "resolve_claude_cli", lambda: None)
        world.installs[NOT_REC] = [local_install()]
        _, failures, action, _, _ = run(
            {"plugins": [tier_entry(NOT_REC, "not_recommended")]},
            selected_state([(NOT_REC, False, None)]))
        assert failures == []
        assert world.uninstalls == []
        assert any("uninstall(s) pending" in l for l in action)


# --------------------------------------------------------------------------- #
# Installs and the rewritten plugins list
# --------------------------------------------------------------------------- #


class TestRewrittenPlugins:
    def test_wanted_recommended_plugin_is_ordered_installed_at_local_scope(self, world):
        manifest = {"plugins": [tier_entry(REC, "recommended")]}
        new, failures, _, _, _ = run(manifest, selected_state([(REC, True, None)]))
        entry = entry_of(new, REC)
        assert (entry["install"], entry["scope"]) == ("auto", "local")
        assert entry[engine._TIER_INSTALL_KEY] is True
        assert failures == []
        assert world.uninstalls == []

    def test_required_profile_plugin_is_ordered_installed(self, world):
        new, *_ = run({"plugins": [tier_entry(REQ, "required")]}, selected_state())
        assert entry_of(new, REQ)["install"] == "auto"

    def test_installed_wanted_plugin_is_only_kept_updated(self, world):
        world.installs[REC] = [local_install()]
        new, *_ = run({"plugins": [tier_entry(REC, "recommended")]},
                      selected_state([(REC, True, None)]))
        entry = entry_of(new, REC)
        assert entry["install"] == "manual"
        assert engine._TIER_INSTALL_KEY not in entry

    def test_declined_recommended_plugin_is_not_installed(self, world):
        new, *_ = run({"plugins": [tier_entry(REC, "recommended")]},
                      selected_state([(REC, False, None)]))
        assert entry_of(new, REC)["install"] == "manual"

    def test_untiered_and_malformed_entries_are_untouched_and_input_not_mutated(
            self, world):
        plain = {"ref": "mkt:plain", "install": "auto", "scope": "user"}
        weird = "not-a-dict"
        manifest = {"plugins": [plain, tier_entry(REC, "recommended"), weird],
                    "tools": [{"name": "git"}]}
        snapshot = json.dumps(manifest, sort_keys=True)

        new, *_ = run(manifest, selected_state([(REC, True, None)]))

        assert json.dumps(manifest, sort_keys=True) == snapshot
        assert new["plugins"][0] == plain and new["plugins"][2] == weird
        assert new["tools"] == [{"name": "git"}]
        assert new["plugins"][1]["install"] == "auto"

    def test_no_tiered_entries_costs_no_inventory_reads(self, world):
        manifest = {"plugins": [{"ref": "mkt:plain"}]}
        new, failures, *_ = run(manifest, selected_state())
        assert new is manifest and failures == []
        assert world.inventory == []

    @pytest.mark.parametrize("status", ["unselected", "none", "unknown", "no_profiles"])
    def test_no_selected_profile_keeps_tiered_entries_manual_and_reads_nothing(
            self, world, status):
        world.installs[NOT_REC] = [local_install()]
        manifest = {"plugins": [tier_entry(REC, "recommended", install="auto"),
                                tier_entry(NOT_REC, "not_recommended")]}
        new, failures, *_ = run(manifest, plain_state(status))
        assert failures == []
        assert [e["install"] for e in new["plugins"]] == ["manual", "manual"]
        assert world.inventory == [] and world.uninstalls == []

    def test_invalid_profile_forces_manual_without_a_duplicate_failure(self, world):
        manifest = {"plugins": [tier_entry(REC, "recommended", install="auto")]}
        new, failures, _, _, quiet = run(manifest, plain_state("invalid"))
        assert failures == []
        assert entry_of(new, REC)["install"] == "manual"
        assert world.inventory == []
        assert any("kept manual" in q for q in quiet)

    def test_unanswered_question_and_stale_answers_are_logged_quietly(self, world):
        manifest = {"plugins": [tier_entry(REC, "recommended")]}
        _, failures, action, _, quiet = run(
            manifest, selected_state([], answers_stale=True))
        assert failures == [] and action == []
        assert any("different profile" in q for q in quiet)
        assert any("unanswered" in q and REC in q for q in quiet)


# --------------------------------------------------------------------------- #
# Failing loudly without taking the pass down
# --------------------------------------------------------------------------- #


class TestFailures:
    def test_unreadable_inventory_fails_that_plugin_and_installs_nothing_on_a_guess(
            self, world):
        world.installs[REC] = InventoryError("cannot read registry /x: bad json")
        manifest = {"plugins": [tier_entry(REC, "recommended"),
                                tier_entry(REQ, "required")]}

        new, failures, action, _, _ = run(
            manifest, selected_state([(REC, True, None)]))

        assert len(failures) == 1
        assert failures[0]["ref"] == REC and "bad json" in failures[0]["message"]
        assert any("state unreadable" in l for l in action)
        # The unreadable plugin is NOT ordered installed even though it is
        # wanted; the readable one still resolves normally.
        assert entry_of(new, REC)["install"] == "manual"
        assert entry_of(new, REQ)["install"] == "auto"

    def test_unreadable_inventory_never_orders_a_removal(self, world):
        world.installs[NOT_REC] = InventoryError("registry record has scope None")
        _, failures, *_ = run(
            {"plugins": [tier_entry(NOT_REC, "not_recommended")]},
            selected_state([(NOT_REC, False, None)]))
        assert len(failures) == 1
        assert world.uninstalls == []

    def test_malformed_tier_entry_is_one_failure_and_nothing_changes(self, world):
        bad = tier_entry(NOT_REC, "not_recommended")
        del bad["why_skip"]
        manifest = {"plugins": [bad, tier_entry(REQ, "required")]}

        new, failures, action, *_ = run(manifest, selected_state([(NOT_REC, False, None)]))

        assert len(failures) == 1 and "why_skip" in failures[0]["message"]
        assert [e["install"] for e in new["plugins"]] == ["manual", "manual"]
        assert world.uninstalls == []
        assert any("plugin tiers:" in l for l in action)


# --------------------------------------------------------------------------- #
# A project settings.local.json override of a user install (known gap)
# --------------------------------------------------------------------------- #


class TestLocalOverrideOfUserInstall:
    """U1 counts an install live when its own scope's settings enable it, so a
    project settings.local.json ``enabledPlugins[ref] = false`` does not hide a
    user-scope install. The engine keeps that meaning: the plugin IS installed
    (user scope) and the override is the user's per-project disable, which
    ``install: manual`` never touches. Treating it as "not installed" would
    order ``install --scope local`` each pass against a plugin already
    registered (a CLI no-op that never converges) and would overwrite the
    user's deliberate ``false``.
    """

    def _world(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        project = tmp_path / "proj"
        (home / ".claude" / "plugins").mkdir(parents=True)
        (project / ".claude").mkdir(parents=True)
        cache = tmp_path / "cache"
        cache.mkdir()
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"version": 2, "plugins": {"rec@mkt": [
                {"scope": "user", "installPath": str(cache), "version": "1"}]}}))
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"rec@mkt": True}}))
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"enabledPlugins": {"rec@mkt": False}}))
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        return project

    def test_user_install_with_a_local_false_override_still_counts_as_installed(
            self, tmp_path, monkeypatch):
        project = self._world(tmp_path, monkeypatch)
        manifest = {"plugins": [tier_entry(REC, "recommended")]}

        new, failures, *_ = run(
            manifest, selected_state([(REC, True, None)]), project_dir=str(project))

        assert failures == []
        assert entry_of(new, REC)["install"] == "manual"   # no install ordered
        assert engine._TIER_INSTALL_KEY not in entry_of(new, REC)


# --------------------------------------------------------------------------- #
# _phase_plugins
# --------------------------------------------------------------------------- #


class _Ctx:
    """Minimal _ManifestContext stand-in capturing what the phase reported."""

    def __init__(self, manifest, project_dir=PROJ):
        self.manifest = manifest
        self.project_dir = project_dir
        self.actions, self.oks, self.quiets, self.failures = [], [], [], []
        self.unusable_marketplaces = set()

    def action(self, message, display=None, detail=None):
        self.actions.append(message)

    def ok(self, message):
        self.oks.append(message)

    def quiet(self, message):
        self.quiets.append(message)

    def fail(self, entry, display=None, detail=None, **failure):
        self.actions.append(entry)
        self.failures.append(dict(failure, _entry=entry))


@pytest.fixture
def phase(monkeypatch):
    """Patch every lifecycle call _phase_plugins can make; record the mutating ones."""
    log = {"install": [], "scope_sync": [], "enable_at_scope": [], "other": []}
    state = {"installed": False, "enabled_at_scope": True}

    def passed(ref, message="stub"):
        return LifecycleResult(passed=True, ref=ref, message=message)

    def check_installed(ref):
        return LifecycleResult(passed=state["installed"], ref=ref, message="stub")

    def install(ref, scope="user", project_dir=None):
        log["install"].append((ref, scope, project_dir))
        return passed(ref, "installed")

    class _Scope:
        added = refused = False
        passed = True
        message = "scope in sync"

    monkeypatch.setattr(lifecycle, "resolve_claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(lifecycle, "check_plugin_installed", check_installed)
    monkeypatch.setattr(lifecycle, "install_plugin", install)
    monkeypatch.setattr(
        lifecycle, "check_plugin_enabled_at_scope",
        lambda ref, scope, project_dir=None: LifecycleResult(
            passed=state["enabled_at_scope"], ref=ref, message="stub"))
    monkeypatch.setattr(
        lifecycle, "enable_plugin_at_scope",
        lambda ref, scope, project_dir=None: (
            log["enable_at_scope"].append((ref, scope)) or passed(ref)))
    monkeypatch.setattr(
        lifecycle, "ensure_registry_scope",
        lambda ref, scope: (log["scope_sync"].append((ref, scope)) or _Scope()))
    monkeypatch.setattr(
        lifecycle, "check_plugin_version",
        lambda ref: type("V", (), {"up_to_date": True, "installed_version": "1",
                                   "latest_version": "1"})())
    monkeypatch.setattr(lifecycle, "pinned_marketplace_sha", lambda m: "")
    monkeypatch.setattr(
        lifecycle, "enable_plugin_in_claude",
        lambda ref: (log["other"].append(("enable", ref)) or passed(ref)))
    monkeypatch.setattr(
        lifecycle, "disable_plugin_at_scope",
        lambda ref, scope, project_dir=None: (
            log["other"].append(("disable", ref)) or passed(ref)))
    monkeypatch.setattr(
        lifecycle, "update_plugin",
        lambda ref, scope="user", project_dir=None: (
            log["other"].append(("update", ref)) or passed(ref)))
    return log, state


class TestPhasePlugins:
    def test_missing_entry_with_enabled_false_is_never_installed(self, phase):
        log, state = phase
        state["installed"] = False
        ctx = _Ctx({"plugins": [{"ref": "mkt:off", "enabled": False, "install": "auto"}]})

        engine._phase_plugins(ctx)

        assert log["install"] == []
        assert ctx.failures == []
        assert any("enabled: false" in line for line in ctx.oks)

    def test_missing_entry_enabled_by_default_is_still_installed(self, phase):
        log, state = phase
        ctx = _Ctx({"plugins": [{"ref": "mkt:on", "install": "auto"}]})

        engine._phase_plugins(ctx)

        assert [c[0] for c in log["install"]] == ["mkt:on"]

    def test_installed_entry_with_enabled_false_is_still_disabled(self, phase):
        log, state = phase
        state["installed"] = True
        ctx = _Ctx({"plugins": [{"ref": "mkt:off", "enabled": False}]})

        engine._phase_plugins(ctx)

        assert ("disable", "mkt:off") in log["other"]
        assert log["install"] == []

    def test_tier_ordered_install_runs_even_when_the_registry_holds_a_record(
            self, phase):
        log, state = phase
        state["installed"] = True      # another project's record / a dormant one
        ctx = _Ctx({"plugins": [{
            "ref": REC, "install": "auto", "scope": "local",
            engine._TIER_INSTALL_KEY: True}]})

        engine._phase_plugins(ctx)

        assert log["install"] == [(REC, "local", PROJ)]

    def test_plain_auto_entry_trusts_the_registry_record(self, phase):
        log, state = phase
        state["installed"] = True
        ctx = _Ctx({"plugins": [{"ref": REC, "install": "auto", "scope": "local"}]})

        engine._phase_plugins(ctx)

        assert log["install"] == []

    def test_local_scope_never_runs_the_registry_scope_sync(self, phase):
        log, state = phase
        state["installed"] = True
        ctx = _Ctx({"plugins": [{"ref": REC, "install": "auto", "scope": "local"}]})

        engine._phase_plugins(ctx)

        assert log["scope_sync"] == []

    def test_user_scope_still_runs_the_registry_scope_sync(self, phase):
        log, state = phase
        state["installed"] = True
        ctx = _Ctx({"plugins": [{"ref": REC, "install": "auto", "scope": "user"}]})

        engine._phase_plugins(ctx)

        assert log["scope_sync"] == [(REC, "user")]


# --------------------------------------------------------------------------- #
# Wiring: a real engine pass
# --------------------------------------------------------------------------- #


FAKE_CLAUDE = (
    "import json, os, sys\n"
    "with open(os.environ['FAKE_CLAUDE_LOG'], 'a') as log:\n"
    "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
)


def _fake_claude(tmp_path):
    """A stand-in `claude` that only records its argv (never the real CLI)."""
    import sys

    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    if os.name == "nt":
        shim = tmp_path / "fake_claude.cmd"
        shim.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        shim = tmp_path / "fake_claude"
        shim.write_text('#!/bin/sh\nexec "%s" "%s" "$@"\n' % (sys.executable, script))
        shim.chmod(0o755)
    return shim


def _pass_world(tmp_path, *, answers, installs):
    """An isolated HOME and project with a selected profile and tiered plugins."""
    from bootstrap.link_compat import link_tree
    from bootstrap.test_engine_personal import BOOTSTRAP_ROOT, make_minimal_root

    home = tmp_path / "home"
    project = tmp_path / "project"
    data_dir = tmp_path / "data" / "plugins-kit" / "bootstrap"
    (home / ".claude" / "plugins").mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    data_dir.mkdir(parents=True)
    root = make_minimal_root(tmp_path)
    link_tree(tmp_path / "bootstrap_minimal" / "scripts",
              os.path.join(BOOTSTRAP_ROOT, "scripts"))
    cache = tmp_path / "cache"
    cache.mkdir()

    records = {}
    enabled = {}
    for ref, scope in installs:
        cli_ref = "%s@%s" % (ref.split(":")[1], ref.split(":")[0])
        record = {"scope": scope, "installPath": str(cache), "version": "1"}
        if scope != "user":
            record["projectPath"] = str(project)
        records.setdefault(cli_ref, []).append(record)
        enabled[cli_ref] = True
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": records}))
    (project / ".claude" / "settings.local.json").write_text(
        json.dumps({"enabledPlugins": enabled}))
    (project / ".claude" / "bootstrap.json").write_text(json.dumps({
        "profiles": {"designer": {}},
        "plugins": [tier_entry(NOT_REC, "not_recommended"),
                    tier_entry(REC, "recommended")],
    }))
    (project / ".claude" / "bootstrap.local.json").write_text(json.dumps({
        "profile": "designer",
        "plugin_choices": {"profile": "designer", "answers": {
            ref: {"want": want, "decided": "2026-10-16"}
            for ref, want in answers.items()}},
    }))

    env = dict(os.environ)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "BOOTSTRAP_PLUGIN_ROOT": root, "BOOTSTRAP_MARKETPLACE": "plugins-kit",
        "CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data"),
        "CLAUDE_REAL_BIN": str(_fake_claude(tmp_path)),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude_calls.log"),
    })
    return type("World", (), {"home": home, "project": project, "root": root,
                              "data_dir": data_dir, "env": env,
                              "log": tmp_path / "claude_calls.log"})()


def _engine_pass(world):
    import subprocess
    import sys
    from bootstrap.test_engine_personal import ENGINE_SCRIPT

    return subprocess.run(
        [sys.executable, ENGINE_SCRIPT, "--plugin-root", world.root,
         "--data-dir", str(world.data_dir), "--project-dir", str(world.project),
         "--console"],
        capture_output=True, text=True, cwd=world.project, env=world.env,
        stdin=subprocess.DEVNULL)


def _claude_calls(world):
    if not world.log.exists():
        return []
    return [json.loads(line) for line in world.log.read_text().splitlines() if line]


class TestEnginePassWiring:
    def test_a_pass_uninstalls_a_plugin_the_recorded_answer_declines(self, tmp_path):
        world = _pass_world(
            tmp_path, answers={NOT_REC: False}, installs=[(NOT_REC, "local")])

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert ["plugin", "uninstall", "norec@mkt", "--scope", "local"] in _claude_calls(world)
        assert "norec" in completed.stdout and "uninstalled" in completed.stdout

    def test_a_pass_without_a_declining_answer_removes_nothing(self, tmp_path):
        world = _pass_world(
            tmp_path, answers={NOT_REC: True}, installs=[(NOT_REC, "local")])

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert not any(c[:2] == ["plugin", "uninstall"] for c in _claude_calls(world))

    def test_a_blocked_project_install_is_visible_and_not_removed(self, tmp_path):
        world = _pass_world(
            tmp_path, answers={NOT_REC: False}, installs=[(NOT_REC, "project")])
        settings = world.project / ".claude" / "settings.json"
        settings.write_text(json.dumps({"enabledPlugins": {"norec@mkt": True}}))
        before = settings.read_bytes()

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "still installed" in completed.stdout
        assert not any(c[:2] == ["plugin", "uninstall"] for c in _claude_calls(world))
        assert settings.read_bytes() == before


class TestUninstallEntryWiring:
    """`uninstall: true` entries through a real engine pass (fake `claude`)."""

    def _add_local_entries(self, world, *entries, profile=None):
        path = world.project / ".claude" / "bootstrap.local.json"
        data = json.loads(path.read_text())
        data["plugins"] = list(entries)
        if profile is not None:
            data["profile"] = profile
            data.pop("plugin_choices", None)
        path.write_text(json.dumps(data))

    def test_a_pass_uninstalls_the_local_install_of_a_marked_plugin(self, tmp_path):
        world = _pass_world(tmp_path, answers={}, installs=[("mkt:gone", "local")])
        self._add_local_entries(world, {"ref": "mkt:gone", "uninstall": True})

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert ["plugin", "uninstall", "gone@mkt", "--scope", "local"] in _claude_calls(world)
        assert not any(c[:2] == ["plugin", "install"] for c in _claude_calls(world))
        assert "gone" in completed.stdout and "uninstalled" in completed.stdout

    def test_a_marked_plugin_with_no_install_runs_nothing_and_stays_quiet(self, tmp_path):
        world = _pass_world(tmp_path, answers={}, installs=[])
        self._add_local_entries(world, {"ref": "mkt:gone", "uninstall": True})

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert _claude_calls(world) == []
        assert "gone" not in completed.stdout

    def test_a_marked_plugin_installed_only_at_user_scope_gets_a_notice(self, tmp_path):
        world = _pass_world(tmp_path, answers={}, installs=[("mkt:gone", "user")])
        (world.home / ".claude" / "settings.json").write_text(
            json.dumps({"enabledPlugins": {"gone@mkt": True}}))
        self._add_local_entries(world, {"ref": "mkt:gone", "uninstall": True})

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert not any(c[:2] == ["plugin", "uninstall"] for c in _claude_calls(world))
        assert "gone: still installed" in completed.stdout

    def test_a_marked_plugin_the_profile_requires_is_a_warning_not_an_uninstall(
            self, tmp_path):
        world = _pass_world(tmp_path, answers={}, installs=[(REQ, "local")])
        project_manifest = world.project / ".claude" / "bootstrap.json"
        manifest = json.loads(project_manifest.read_text())
        manifest["plugins"].append(tier_entry(REQ, "required"))
        project_manifest.write_text(json.dumps(manifest))
        self._add_local_entries(world, {"ref": REQ, "uninstall": True})

        completed = _engine_pass(world)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert not any(c[:2] == ["plugin", "uninstall"] for c in _claude_calls(world))
        assert "req: uninstall conflict" in completed.stdout


class TestPhasePluginsUninstallEntry:
    def test_an_uninstall_entry_is_never_installed_by_the_plugin_phase(self, phase):
        log, state = phase
        state["installed"] = False
        ctx = _Ctx({"plugins": [{"ref": "mkt:gone", "install": "manual",
                                 "uninstall": True}]})

        engine._phase_plugins(ctx)

        assert log["install"] == [] and log["other"] == []
        assert ctx.failures == []
        assert any("uninstall: true" in line for line in ctx.oks)
