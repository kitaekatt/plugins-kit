"""`bootstrap profile [set|clear]` -- the CLI face of bootstrap_lib.profiles.

Drives the CLI through its argparse entry (`cli.main([...])`), never through
the module functions directly, so a wiring mistake in `main()` is caught the
same way a real invocation would hit it. Each test redirects HOME and the
bootstrap data root so nothing here can touch a developer's real machine.
"""

import importlib.util
import json
import os

import pytest

from bootstrap_lib import engine as bootstrap_engine
from bootstrap_lib import proc_lock, profiles as bootstrap_profiles

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins", "bootstrap", "scripts",
)


def _load_cli():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_cli_profile", os.path.join(SCRIPTS, "bootstrap_cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


class _ExitedProcess:
    returncode = 0

    def poll(self):
        return 0


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


ONE_PROFILE = {"profiles": {"engineer": {"description": "Engineering tools."}}}
TWO_PROFILES = {
    "profiles": {
        "engineer": {"description": "Engineering tools."},
        "designer": {"description": "Design tools."},
    }
}
INVALID_PROFILES = {"profiles": {"engineer": {"extends": ["ghost"]}}}
FOUR_PROFILES = {
    "profiles": {
        "engineer": {"description": "Engineering tools."},
        "designer": {"description": "Design tools."},
        "writer": {"description": "Writing tools."},
        "ops": {"description": "Ops tools.", "extends": ["engineer"]},
    }
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fresh HOME, project dir, and single-marketplace data root."""
    home = tmp_path / "home"
    project = tmp_path / "proj"
    data_root = tmp_path / "data"
    home.mkdir()
    project.mkdir()
    (data_root / "mkt-a" / "bootstrap").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(data_root))
    monkeypatch.delenv("BOOTSTRAP_MARKETPLACE", raising=False)
    # Never let a test accidentally spawn or wait on the real converge step
    # unless it explicitly arranges for one.
    monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "")
    # Git exclusion is bootstrap_lib.profiles' own tested behaviour
    # (tests/bootstrap/test_profiles.py); a real `git` subprocess call here
    # would also collide with a test that monkeypatches subprocess.Popen for
    # the converge step, since both go through the SAME subprocess module.
    monkeypatch.setattr(cli.bootstrap_profiles, "ensure_vcs_excluded",
                         lambda project_dir: None)

    return type("Env", (), {
        "home": home, "project": project, "data_root": data_root,
        "data_dir": data_root / "mkt-a" / "bootstrap",
    })()


def user_local(env):
    return env.home / ".claude" / "bootstrap.local.json"


def project_local(env):
    return env.project / ".claude" / "bootstrap.local.json"


def project_manifest(env):
    return env.project / ".claude" / "bootstrap.json"


# --------------------------------------------------------------------------
# bare `profile` -- status
# --------------------------------------------------------------------------

class TestStatus:

    def test_no_profiles_declared_anywhere(self, env, capsys):
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        assert "status: no_profiles" in capsys.readouterr().out

    def test_unselected_lists_available_profiles(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "status: unselected" in out
        assert "engineer" in out
        assert "Engineering tools." in out

    def test_selected_shows_source_and_chain(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        _write_json(project_local(env), {"profile": "engineer"})
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "status: selected" in out
        assert "engineer" in out
        assert "applied chain: engineer" in out

    def test_unknown_selection_is_reported_with_a_warning(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        _write_json(project_local(env), {"profile": "ghost"})
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "status: unknown" in out
        assert "warning:" in out

    def test_invalid_profiles_are_reported_as_errors(self, env, capsys):
        _write_json(project_manifest(env), INVALID_PROFILES)
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "status: invalid" in out
        assert "error:" in out

    def test_json_includes_question_and_null_under_no_profiles(self, env, capsys):
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "no_profiles"
        assert payload["question"] is None

    def test_json_question_mode_is_first_run_when_unselected(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "unselected"
        assert payload["question"] is not None
        assert payload["question"]["options"][0]["label"] == "Not now"

    def test_json_question_mode_is_switch_when_selected(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        _write_json(project_local(env), {"profile": "engineer"})
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["question"]["options"][0]["label"] == "Keep current"

    # -- overflow fields: profile_listing / needs_typed_choice --------------

    def test_overflow_fields_under_no_profiles(self, env, capsys):
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["profile_listing"] == ""
        assert payload["needs_typed_choice"] is False
        assert payload["question"] is None

    def test_overflow_fields_at_or_under_the_threshold(self, env, capsys):
        _write_json(project_manifest(env), TWO_PROFILES)
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["needs_typed_choice"] is False
        assert "engineer" in payload["profile_listing"]
        assert "designer" in payload["profile_listing"]
        # Below the threshold, the question still names each profile.
        labels = [opt["label"] for opt in payload["question"]["options"]]
        assert "engineer" in labels and "designer" in labels

    def test_overflow_fields_past_the_threshold(self, env, capsys):
        _write_json(project_manifest(env), FOUR_PROFILES)
        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["needs_typed_choice"] is True
        listing = payload["profile_listing"]
        for name in ("engineer", "designer", "writer", "ops"):
            assert name in listing
        assert "ops (extends engineer)" in listing
        # Past the threshold the question names no profile directly -- only
        # the lead option and the typed-choice label.
        labels = [opt["label"] for opt in payload["question"]["options"]]
        assert labels == ["Not now", bootstrap_profiles.TYPED_CHOICE_LABEL]

    def test_human_output_uses_the_shared_listing_helper(self, env, monkeypatch, capsys):
        """Pins the reuse, not just the resulting text.

        A future edit that inlines its own rendering again -- rather than
        calling `render_profile_listing` -- must show up here, not just agree
        with the JSON path by coincidence.
        """
        _write_json(project_manifest(env), ONE_PROFILE)
        calls = []
        real = bootstrap_profiles.render_profile_listing

        def spy(state):
            calls.append(state.status)
            return real(state)

        monkeypatch.setattr(cli.bootstrap_profiles, "render_profile_listing", spy)
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 0
        assert calls == ["unselected"]
        assert "engineer" in capsys.readouterr().out


# --------------------------------------------------------------------------
# CLI/engine agreement -- the point of calling _load_layered_manifests_ex
# directly rather than re-deriving the layer list.
# --------------------------------------------------------------------------

class TestResolvesThroughTheEngine:

    def test_status_resolves_through_the_real_engine_function(
            self, env, monkeypatch, capsys):
        """Pins the call path, not just the outcome.

        Monkeypatches `bootstrap_lib.engine._load_layered_manifests_ex`
        itself (not anything CLI-local) so a future rewrite of
        `_load_profile_state` that stops calling it -- e.g. reintroducing a
        local re-derivation of the layer list -- shows up here as a failure,
        rather than as two implementations quietly agreeing by luck.
        """
        calls = []

        def fake_ex(project_dir, data_dir=None):
            calls.append((str(project_dir), data_dir))
            return {}, [], bootstrap_profiles.ProfileState(
                status="no_profiles", write_target="/nowhere/bootstrap.local.json")

        monkeypatch.setattr(bootstrap_engine, "_load_layered_manifests_ex", fake_ex)

        rc = cli.main(["profile", "--json", "--project-dir", str(env.project)])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "no_profiles"
        assert payload["write_target"] == "/nowhere/bootstrap.local.json"
        # The deprecated legacy layer is left out of the profile report:
        # data_dir must be None, never the CLI's own plugin data dir.
        assert calls == [(str(env.project), None)]

    def test_engine_missing_the_helper_fails_loudly(self, env, capsys):
        rc = cli.main(["profile", "--project-dir", str(env.project)])
        # Sanity: with the real engine present this succeeds.
        assert rc == 0
        capsys.readouterr()

        import builtins
        real_import = builtins.__import__

        def blocking_import(name, *a, **k):
            if name == "bootstrap_lib.engine":
                raise ImportError("simulated: engine predates profile support")
            return real_import(name, *a, **k)

        import pytest as _pytest
        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(builtins, "__import__", blocking_import)
            rc = cli.main(["profile", "--project-dir", str(env.project)])
        assert rc == 1
        assert "_load_layered_manifests_ex" in capsys.readouterr().err


# --------------------------------------------------------------------------
# `profile set`
# --------------------------------------------------------------------------

class TestSet:

    def test_refuses_while_a_pass_holds_the_lock_and_writes_nothing(
            self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        before = json.dumps({"tools": []}, indent=2) + "\n"
        target = project_local(env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(before)

        with proc_lock.engine_lock(str(env.data_dir)):
            rc = cli.main([
                "profile", "set", "engineer",
                "--project-dir", str(env.project), "--project",
            ])

        assert rc == 2
        assert "running" in capsys.readouterr().err
        assert target.read_text() == before, (
            "the lock-held refusal must leave the target file byte-identical")

    def test_set_none_is_always_allowed_even_under_an_invalid_state(
            self, env, monkeypatch, capsys):
        _write_json(project_manifest(env), INVALID_PROFILES)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "")

        rc = cli.main([
            "profile", "set", "none", "--project-dir", str(env.project),
        ])
        assert rc == 1  # no plugin tree to converge -- see below
        # 'none' was still written even though the plugin tree could not be
        # found to converge it (checked directly, since the write happens
        # before that failure). The project layer declares 'profiles' (D1),
        # so the default target is project-local even though the declaration
        # itself is invalid.
        assert json.loads(project_local(env).read_text())["profile"] == "none"

    def test_set_unknown_profile_exits_1_without_writing(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        rc = cli.main([
            "profile", "set", "ghost", "--project-dir", str(env.project),
        ])
        assert rc == 1
        assert "not a declared profile" in capsys.readouterr().err
        assert not project_local(env).exists()
        assert not user_local(env).exists()

    def test_write_preserves_unrelated_keys(self, env, monkeypatch, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        target = project_local(env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"tools": [{"name": "uv"}]}, indent=2) + "\n")

        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _ExitedProcess())

        rc = cli.main([
            "profile", "set", "engineer",
            "--project-dir", str(env.project), "--project",
        ])
        assert rc == 0
        written = json.loads(target.read_text())
        assert written["profile"] == "engineer"
        assert written["tools"] == [{"name": "uv"}]

    def test_user_flag_overrides_the_default_write_target(
            self, env, monkeypatch, capsys):
        # A project layer declares profiles, so the default target (D1) is
        # project-local; --user must override that.
        _write_json(project_manifest(env), ONE_PROFILE)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _ExitedProcess())

        rc = cli.main([
            "profile", "set", "engineer", "--user",
            "--project-dir", str(env.project),
        ])
        assert rc == 0
        assert json.loads(user_local(env).read_text())["profile"] == "engineer"
        assert not project_local(env).exists()

    def test_project_flag_overrides_the_default_write_target(
            self, env, monkeypatch, capsys):
        # With no project layer declaring profiles, the default target is
        # user-local; --project must override that.
        _write_json(env.home / ".claude" / "bootstrap.json", ONE_PROFILE)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **k: _ExitedProcess())

        rc = cli.main([
            "profile", "set", "engineer", "--project",
            "--project-dir", str(env.project),
        ])
        assert rc == 0
        assert json.loads(project_local(env).read_text())["profile"] == "engineer"

    def test_converge_step_receives_the_named_project_dir(
            self, env, monkeypatch, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            seen["stdin"] = kw.get("stdin")
            return _ExitedProcess()

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

        rc = cli.main([
            "profile", "set", "engineer", "--project-dir", str(env.project),
        ])
        assert rc == 0
        cmd = seen["cmd"]
        assert cmd[cmd.index("--project-dir") + 1] == str(env.project)
        # The same engine pass `bootstrap run` launches, not a separate runner.
        assert cmd == cli._engine_cmd("/plug", str(env.data_dir), env.project)
        assert seen["stdin"] is cli.subprocess.DEVNULL

    def test_project_dir_before_set_is_honored_not_discarded(self, env, capsys):
        """--project-dir given BEFORE `set` must not be silently dropped.

        argparse parses a subparser into the SAME namespace but then applies
        the subparser's own defaults; a subparser-level `default=None` on
        `--project-dir` unconditionally overwrote whatever the outer
        `profile` parser had already parsed for that dest, so
        `profile --project-dir X set foo` silently fell back to Path.cwd()
        while `profile set foo --project-dir X` worked. Both spellings are
        advertised by the help text, so both must resolve to X.
        """
        _write_json(project_manifest(env), ONE_PROFILE)
        rc = cli.main([
            "profile", "--project-dir", str(env.project), "set", "engineer",
        ])
        assert rc == 1  # env's fixture stubs find_plugin_root to "" -- converge fails
        assert "no bootstrap plugin tree" in capsys.readouterr().err
        # The write itself must have landed in env.project, not Path.cwd().
        assert json.loads(project_local(env).read_text())["profile"] == "engineer"

    def test_no_plugin_tree_reports_written_but_not_converged(self, env, capsys):
        _write_json(project_manifest(env), ONE_PROFILE)
        rc = cli.main([
            "profile", "set", "engineer", "--project-dir", str(env.project),
        ])
        assert rc == 1
        assert "no bootstrap plugin tree" in capsys.readouterr().err
        assert json.loads(project_local(env).read_text())["profile"] == "engineer"


# --------------------------------------------------------------------------
# `profile clear`
# --------------------------------------------------------------------------

class TestClear:

    def test_refuses_while_a_pass_holds_the_lock(self, env, capsys):
        target = project_local(env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"profile": "engineer"}, indent=2) + "\n")

        with proc_lock.engine_lock(str(env.data_dir)):
            rc = cli.main([
                "profile", "clear", "--project", "--project-dir", str(env.project),
            ])

        assert rc == 2
        assert "running" in capsys.readouterr().err
        assert json.loads(target.read_text())["profile"] == "engineer"

    def test_removes_the_key_and_preserves_the_rest(self, env, capsys):
        target = project_local(env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(
            {"profile": "engineer", "tools": [{"name": "uv"}]}, indent=2) + "\n")

        rc = cli.main([
            "profile", "clear", "--project", "--project-dir", str(env.project),
        ])
        assert rc == 0
        written = json.loads(target.read_text())
        assert "profile" not in written
        assert written["tools"] == [{"name": "uv"}]
        assert "next bootstrap pass" in capsys.readouterr().out

    def test_project_dir_before_clear_is_honored_not_discarded(self, env, capsys):
        """Same discard bug as `set`, driven through `clear` instead."""
        target = project_local(env)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"profile": "engineer"}, indent=2) + "\n")

        rc = cli.main([
            "profile", "--project-dir", str(env.project), "clear", "--project",
        ])
        assert rc == 0
        assert "profile" not in json.loads(target.read_text())

    def test_clear_with_no_existing_file_is_a_no_op_success(self, env, capsys):
        rc = cli.main([
            "profile", "clear", "--user", "--project-dir", str(env.project),
        ])
        assert rc == 0
        assert not user_local(env).exists()


# ==========================================================================
# Plugin-tier contract: status capabilities, choices, apply, clear --all
# ==========================================================================

from bootstrap_lib import marketplace_lifecycle, plugin_tiers  # noqa: E402

EXIT_REFUSED = cli.EXIT_REFUSED
CAPABILITIES = ["choices_for", "apply", "clear_all", "unset"]

PROTOTYPING = "mkt:prototyping"
ENG_ADV = "mkt:engineering-advanced"
SANDBOX = "mkt:claude-sandbox"


def _tiered(ref, tier, why_install, why_skip):
    return {"ref": ref, "tier": tier, "install": "manual",
            "why_install": why_install, "why_skip": why_skip}


# Base: one not-recommended plugin every profile inherits. designer: one
# recommended and one not-recommended of its own. engineer: nothing extra.
TIERED_MANIFEST = {
    "plugins": [_tiered(SANDBOX, "not_recommended",
                        "Sandboxes agent shells.", "Rarely needed here.")],
    "profiles": {
        "designer": {
            "description": "Design tools.",
            "plugins": [
                _tiered(PROTOTYPING, "recommended",
                        "Mock up ideas fast.", "Adds commands you may not use."),
                _tiered(ENG_ADV, "not_recommended",
                        "Deep engineering tooling.", "Engineering only."),
            ],
        },
        "engineer": {"description": "Engineering tools."},
    },
}


def _registry_path(env):
    return env.home / ".claude" / "plugins" / "installed_plugins.json"


def _cli_ref(ref):
    marketplace, name = ref.split(":")
    return "%s@%s" % (name, marketplace)


def _install(env, ref, scope, *, enabled=True):
    """Register `ref` at `scope` for env.project and (when `enabled`) enable it."""
    path = _registry_path(env)
    registry = (json.loads(path.read_text())
                if path.exists() else {"version": 2, "plugins": {}})
    where = env.home / "cache" / ref.replace(":", "_") / scope
    where.mkdir(parents=True, exist_ok=True)
    record = {"scope": scope, "installPath": str(where), "version": "1.0.0"}
    if scope != "user":
        record["projectPath"] = str(env.project)
    registry["plugins"].setdefault(_cli_ref(ref), []).append(record)
    _write_json(path, registry)
    settings = {
        "user": env.home / ".claude" / "settings.json",
        "project": env.project / ".claude" / "settings.json",
        "local": env.project / ".claude" / "settings.local.json",
    }[scope]
    current = json.loads(settings.read_text()) if settings.exists() else {}
    current.setdefault("enabledPlugins", {})[_cli_ref(ref)] = enabled
    _write_json(settings, current)


@pytest.fixture
def tiered(env):
    _write_json(project_manifest(env), TIERED_MANIFEST)
    return env


@pytest.fixture
def passes(monkeypatch):
    """Replace the convergence pass; record each launch. Never runs a pass."""
    calls = []
    monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")

    def fake_launch(plugin_root, data_dir, project_dir, forward=()):
        calls.append((plugin_root, data_dir, str(project_dir)))
        return 0

    monkeypatch.setattr(cli, "_launch_pass", fake_launch)
    return calls


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------
# `profile status --json` and the capabilities list
# --------------------------------------------------------------------------

class TestStatusCapabilities:

    def test_status_is_an_alias_of_bare_profile_json(self, tiered, capsys):
        assert cli.main(["profile", "--json", "--project-dir", str(tiered.project)]) == 0
        bare = _json_out(capsys)
        assert cli.main(["profile", "status", "--json",
                         "--project-dir", str(tiered.project)]) == 0
        assert _json_out(capsys) == bare

    def test_capabilities_list_is_reported(self, tiered, capsys):
        assert cli.main(["profile", "status", "--json",
                         "--project-dir", str(tiered.project)]) == 0
        assert _json_out(capsys)["capabilities"] == CAPABILITIES

    def test_capability_is_dropped_when_the_library_lacks_its_symbol(
            self, tiered, monkeypatch, capsys):
        monkeypatch.delattr(plugin_tiers, "resolve_plugin_tiers")
        monkeypatch.delattr(bootstrap_profiles, "write_selection_with_answers")
        assert cli.main(["profile", "status", "--json",
                         "--project-dir", str(tiered.project)]) == 0
        assert _json_out(capsys)["capabilities"] == ["clear_all", "unset"]

    def test_status_never_reads_the_registry_or_runs_a_pass(
            self, tiered, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("status must not touch the installed inventory")

        monkeypatch.setattr(marketplace_lifecycle, "plugin_installs", boom)
        monkeypatch.setattr(cli, "_launch_pass", boom)
        monkeypatch.setattr(cli.subprocess, "Popen", boom)
        assert cli.main(["profile", "status", "--json",
                         "--project-dir", str(tiered.project)]) == 0
        assert _json_out(capsys)["status"] == "unselected"

    def test_project_dir_before_status_is_honored(self, tiered, capsys):
        assert cli.main(["profile", "--project-dir", str(tiered.project),
                         "status", "--json"]) == 0
        assert _json_out(capsys)["status"] == "unselected"


# --------------------------------------------------------------------------
# `profile choices --for`
# --------------------------------------------------------------------------

def _choices(env, name, capsys):
    rc = cli.main(["profile", "choices", "--for", name, "--json",
                   "--project-dir", str(env.project)])
    return rc, json.loads(capsys.readouterr().out)


class TestChoices:

    def test_json_shape_and_order(self, tiered, capsys):
        _install(tiered, SANDBOX, "local")
        _install(tiered, ENG_ADV, "user")

        rc, payload = _choices(tiered, "designer", capsys)

        assert rc == 0
        assert payload["profile"] == "designer"
        assert payload["chain"] == ["designer"]
        # Recommended questions first, then not-recommended, each in manifest
        # order (base entries precede the profile's own).
        assert [q["ref"] for q in payload["questions"]] == [PROTOTYPING, SANDBOX, ENG_ADV]
        recommended, sandbox, eng = payload["questions"]
        assert recommended == {
            "ref": PROTOTYPING, "tier": "recommended",
            "why_lines": [{"kind": "why_install", "text": "Mock up ideas fast."},
                          {"kind": "why_skip", "text": "Adds commands you may not use."}],
            "why_install": "Mock up ideas fast.",
            "why_skip": "Adds commands you may not use.",
            "scopes": [], "needs_user_scope_confirm": False,
        }
        # A not-recommended plugin lists the skip reason first.
        assert [l["kind"] for l in sandbox["why_lines"]] == ["why_skip", "why_install"]
        assert sandbox["scopes"] == ["local"]
        assert sandbox["needs_user_scope_confirm"] is False
        assert eng["scopes"] == ["user"]
        assert eng["needs_user_scope_confirm"] is True

    def test_installed_recommended_and_uninstalled_not_recommended_are_not_asked(
            self, tiered, capsys):
        _install(tiered, PROTOTYPING, "local")
        rc, payload = _choices(tiered, "designer", capsys)
        assert rc == 0
        assert payload["questions"] == []

    def test_a_dormant_record_counts_as_not_installed(self, tiered, capsys):
        _install(tiered, PROTOTYPING, "project", enabled=False)
        rc, payload = _choices(tiered, "designer", capsys)
        assert [q["ref"] for q in payload["questions"]] == [PROTOTYPING]

    def test_base_entries_apply_to_a_profile_with_no_plugins_of_its_own(
            self, tiered, capsys):
        _install(tiered, SANDBOX, "local")
        rc, payload = _choices(tiered, "engineer", capsys)
        assert [q["ref"] for q in payload["questions"]] == [SANDBOX]

    def test_is_read_only_runs_no_pass_and_works_while_a_pass_runs(
            self, tiered, monkeypatch, capsys):
        _install(tiered, SANDBOX, "local")
        local = project_local(tiered)
        _write_json(local, {"profile": "engineer", "tools": []})
        before = local.read_bytes()

        def boom(*a, **k):
            raise AssertionError("choices must not launch a pass")

        monkeypatch.setattr(cli, "_launch_pass", boom)
        monkeypatch.setattr(cli.subprocess, "Popen", boom)
        with proc_lock.engine_lock(str(tiered.data_dir)):
            rc, payload = _choices(tiered, "designer", capsys)

        assert rc == 0 and payload["questions"]
        assert local.read_bytes() == before
        assert not user_local(tiered).exists()

    @pytest.mark.parametrize("bound", ["engineer", "designer"])
    def test_recorded_answers_do_not_hide_questions(self, tiered, bound, capsys):
        """choices describes a fresh run, whatever was answered before."""
        _install(tiered, SANDBOX, "local")
        _write_json(project_local(tiered), {
            "profile": bound,
            "plugin_choices": {"profile": bound, "answers": {
                SANDBOX: {"want": False, "decided": "2026-10-01"}}}})
        rc, payload = _choices(tiered, "designer", capsys)
        assert SANDBOX in [q["ref"] for q in payload["questions"]]
        assert payload["warnings"] == []

    @pytest.mark.parametrize("name", ["ghost", "none", "unset", "Bad Name"])
    def test_an_unselectable_name_is_refused_with_exit_3(self, tiered, name, capsys):
        rc, payload = _choices(tiered, name, capsys)
        assert rc == EXIT_REFUSED
        assert payload["refused"] in ("not_a_profile", "unknown_profile")

    def test_invalid_declarations_are_refused(self, env, capsys):
        _write_json(project_manifest(env), INVALID_PROFILES)
        rc, payload = _choices(env, "engineer", capsys)
        assert rc == EXIT_REFUSED
        assert payload["refused"] == "invalid_declaration"

    def test_an_unreadable_registry_fails_loudly(self, tiered, capsys):
        _registry_path(tiered).parent.mkdir(parents=True, exist_ok=True)
        _registry_path(tiered).write_text("{not json")
        rc = cli.main(["profile", "choices", "--for", "designer", "--json",
                       "--project-dir", str(tiered.project)])
        assert rc == 1
        assert "cannot read registry" in capsys.readouterr().err

    def test_human_output_lists_questions_in_tier_order(self, tiered, capsys):
        _install(tiered, ENG_ADV, "user")
        rc = cli.main(["profile", "choices", "--for", "designer",
                       "--project-dir", str(tiered.project)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.index(PROTOTYPING) < out.index(ENG_ADV)
        assert "installed at: user" in out
        assert "user-scope double-check" in out


# --------------------------------------------------------------------------
# `profile apply`
# --------------------------------------------------------------------------

def _apply(env, capsys, answers=None, profile="designer"):
    argv = ["profile", "apply", "--profile", profile,
            "--project-dir", str(env.project)]
    if answers is not None:
        argv += ["--answers", json.dumps(answers)]
    rc = cli.main(argv)
    return rc, capsys.readouterr()


class TestApply:

    def test_writes_profile_and_answers_in_one_file_and_launches_the_pass(
            self, tiered, passes, capsys):
        _install(tiered, SANDBOX, "local")
        local = project_local(tiered)
        _write_json(local, {"tools": [{"name": "uv"}]})

        rc, _out = _apply(tiered, capsys, {
            PROTOTYPING: {"want": True}, SANDBOX: {"want": False}})

        assert rc == 0
        written = json.loads(local.read_text())
        assert written["profile"] == "designer"
        assert written["tools"] == [{"name": "uv"}]
        assert written["plugin_choices"]["profile"] == "designer"
        answers = written["plugin_choices"]["answers"]
        assert answers[PROTOTYPING]["want"] is True
        assert answers[SANDBOX]["want"] is False
        assert all(a["decided"] for a in answers.values())
        assert passes == [("/plug", str(tiered.data_dir), str(tiered.project))]

    def test_a_pass_started_after_apply_sees_profile_and_answers_together(
            self, tiered, monkeypatch, capsys):
        _install(tiered, SANDBOX, "local")
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "FINAL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(cli, "POLL_INTERVAL", 0.0)
        seen = {}

        def fake_popen(cmd, **kw):
            _m, _e, state = bootstrap_engine._load_layered_manifests_ex(
                str(tiered.project))
            seen["status"] = state.status
            seen["selected"] = state.selected
            seen["answers"] = bootstrap_profiles.answers_by_ref(state)
            seen["lock_holder"] = cli.holder(str(tiered.data_dir))
            return _ExitedProcess()

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

        rc, _out = _apply(tiered, capsys, {SANDBOX: {"want": False}})

        assert rc == 0
        assert seen["status"] == "selected" and seen["selected"] == "designer"
        assert seen["answers"] == {SANDBOX: {"want": False, "scope": None}}
        # The lock is released BEFORE the pass starts, so the pass can take it.
        assert seen["lock_holder"] is None

    def test_pass_exit_code_is_returned(self, tiered, monkeypatch, capsys):
        monkeypatch.setattr(cli, "find_plugin_root", lambda m, f="": "/plug")
        monkeypatch.setattr(cli, "_launch_pass", lambda *a, **k: 1)
        rc, _out = _apply(tiered, capsys)
        assert rc == 1
        assert json.loads(project_local(tiered).read_text())["profile"] == "designer"

    def test_no_answers_clears_stale_answers(self, tiered, passes, capsys):
        _write_json(project_local(tiered), {
            "profile": "designer",
            "plugin_choices": {"profile": "designer", "answers": {
                SANDBOX: {"want": False, "decided": "2026-10-01"}}}})
        rc, _out = _apply(tiered, capsys)
        assert rc == 0
        written = json.loads(project_local(tiered).read_text())
        assert written["profile"] == "designer"
        assert "plugin_choices" not in written

    def test_answers_may_come_from_a_file(self, tiered, passes, tmp_path, capsys):
        answers = tmp_path / "answers.json"
        answers.write_text(json.dumps({PROTOTYPING: {"want": True}}))
        rc = cli.main(["profile", "apply", "--profile", "designer",
                       "--answers", str(answers), "--project-dir", str(tiered.project)])
        assert rc == 0
        recorded = json.loads(project_local(tiered).read_text())["plugin_choices"]
        assert list(recorded["answers"]) == [PROTOTYPING]

    def test_no_plugin_tree_reports_written_but_not_converged(self, tiered, capsys):
        rc, out = _apply(tiered, capsys)
        assert rc == 1
        assert "no bootstrap plugin tree" in out.err
        assert json.loads(project_local(tiered).read_text())["profile"] == "designer"

    # -- the lock ----------------------------------------------------------

    def test_refuses_while_a_pass_holds_the_lock_and_writes_nothing(
            self, tiered, passes, capsys):
        local = project_local(tiered)
        _write_json(local, {"profile": "engineer", "tools": []})
        before = local.read_bytes()

        with proc_lock.engine_lock(str(tiered.data_dir)):
            rc, out = _apply(tiered, capsys, {PROTOTYPING: {"want": True}})

        assert rc == 2
        assert "running" in out.err
        assert local.read_bytes() == before, "file must be byte-identical"
        assert passes == []

    def test_lock_held_with_no_prior_file_creates_nothing(self, tiered, passes, capsys):
        with proc_lock.engine_lock(str(tiered.data_dir)):
            rc, _out = _apply(tiered, capsys)
        assert rc == 2
        assert not project_local(tiered).exists()

    @staticmethod
    def _interleave(monkeypatch, between):
        """Run `between()` after the CLI has started and before it holds the
        lock: the window a concurrent writer can land in."""
        real = cli._engine_lock
        calls = []

        def racing(data_dir):
            calls.append(data_dir)
            between()
            return real(data_dir)

        monkeypatch.setattr(cli, "_engine_lock", racing)
        return calls

    def test_interleaved_writer_to_the_local_file_is_not_lost(
            self, tiered, passes, monkeypatch, capsys):
        local = project_local(tiered)
        _write_json(local, {"profile": "engineer", "tools": []})

        def concurrent_writer():
            _write_json(local, {"profile": "engineer", "tools": [{"name": "late"}]})

        calls = self._interleave(monkeypatch, concurrent_writer)
        rc, _out = _apply(tiered, capsys, {PROTOTYPING: {"want": True}})

        assert calls, "the CLI must go through the lock"
        assert rc == 0
        written = json.loads(local.read_text())
        assert written["tools"] == [{"name": "late"}], "the concurrent write was lost"
        assert written["profile"] == "designer"
        assert PROTOTYPING in written["plugin_choices"]["answers"]

    def test_validation_reads_state_under_the_lock_not_before(
            self, tiered, passes, monkeypatch, capsys):
        """A profile removed between the CLI starting and its lock is refused."""
        def concurrent_writer():
            _write_json(project_manifest(tiered), {
                "profiles": {"engineer": {"description": "Engineering tools."}}})

        self._interleave(monkeypatch, concurrent_writer)
        rc, out = _apply(tiered, capsys, {PROTOTYPING: {"want": True}})

        assert rc == EXIT_REFUSED
        assert json.loads(out.out)["refused"] == "unknown_profile"
        assert not project_local(tiered).exists()
        assert passes == []

    def test_a_user_install_added_in_the_window_needs_the_double_check(
            self, tiered, passes, monkeypatch, capsys):
        self._interleave(monkeypatch, lambda: _install(tiered, ENG_ADV, "user"))
        rc, out = _apply(tiered, capsys, {ENG_ADV: {"want": False}})

        assert rc == EXIT_REFUSED
        assert json.loads(out.out)["refused"] == "user_scope_confirmation_required"
        assert not project_local(tiered).exists()

    # -- validation --------------------------------------------------------

    def test_a_no_on_a_user_install_without_confirmation_is_refused(
            self, tiered, passes, capsys):
        _install(tiered, ENG_ADV, "user")
        _install(tiered, SANDBOX, "local")
        local = project_local(tiered)
        _write_json(local, {"profile": "engineer"})
        before = local.read_bytes()

        rc, out = _apply(tiered, capsys, {
            ENG_ADV: {"want": False}, SANDBOX: {"want": False}})

        assert rc == EXIT_REFUSED
        payload = json.loads(out.out)
        assert payload["refused"] == "user_scope_confirmation_required"
        assert payload["refs"] == [ENG_ADV]
        assert local.read_bytes() == before
        assert passes == []

    def test_a_confirmed_user_scope_no_is_recorded_with_its_scope(
            self, tiered, passes, capsys):
        _install(tiered, ENG_ADV, "user")
        rc, _out = _apply(tiered, capsys, {ENG_ADV: {"want": False, "scope": "user"}})
        assert rc == 0
        recorded = json.loads(project_local(tiered).read_text())
        assert recorded["plugin_choices"]["answers"][ENG_ADV]["scope"] == "user"

    def test_keeping_a_user_install_needs_no_confirmation(self, tiered, passes, capsys):
        _install(tiered, ENG_ADV, "user")
        rc, _out = _apply(tiered, capsys, {ENG_ADV: {"want": True}})
        assert rc == 0

    @pytest.mark.parametrize("answers, reason", [
        ({"mkt:nope": {"want": True}}, "unknown_answer_ref"),
        ({PROTOTYPING: {"want": "yes"}}, "malformed_answer"),
        ({PROTOTYPING: True}, "malformed_answer"),
        ({PROTOTYPING: {"want": True, "scope": "user"}}, "malformed_answer"),
        ({SANDBOX: {"want": False, "scope": "project"}}, "malformed_answer"),
    ])
    def test_bad_answers_are_refused_before_anything_is_written(
            self, tiered, passes, answers, reason, capsys):
        rc, out = _apply(tiered, capsys, answers)
        assert rc == EXIT_REFUSED
        assert json.loads(out.out)["refused"] == reason
        assert not project_local(tiered).exists()
        assert passes == []

    @pytest.mark.parametrize("value", ["{not json", "[1, 2]", "no-such-file.json"])
    def test_unusable_answers_argument_is_refused(self, tiered, passes, value):
        rc = cli.main(["profile", "apply", "--profile", "designer",
                       "--answers", value, "--project-dir", str(tiered.project)])
        assert rc == EXIT_REFUSED
        assert not project_local(tiered).exists()

    @pytest.mark.parametrize("name", ["ghost", "none", "unset"])
    def test_an_unselectable_profile_is_refused_and_nothing_is_written(
            self, tiered, passes, name, capsys):
        rc, _out = _apply(tiered, capsys, None, name)
        assert rc == EXIT_REFUSED
        assert not project_local(tiered).exists()
        assert passes == []

    def test_a_malformed_local_file_is_never_rewritten(self, tiered, passes, capsys):
        local = project_local(tiered)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("{broken")
        rc, _out = _apply(tiered, capsys)
        assert rc == EXIT_REFUSED
        assert local.read_text() == "{broken"

    def test_user_level_profile_does_not_change_where_apply_writes(
            self, tiered, passes, capsys):
        _write_json(user_local(tiered), {"profile": "engineer"})
        rc, _out = _apply(tiered, capsys)
        assert rc == 0
        assert json.loads(user_local(tiered).read_text()) == {"profile": "engineer"}
        assert json.loads(project_local(tiered).read_text())["profile"] == "designer"


# --------------------------------------------------------------------------
# `profile clear --all`
# --------------------------------------------------------------------------

class TestClearAll:

    def test_sets_unset_removes_answers_keeps_the_rest_and_runs_no_pass(
            self, tiered, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("clear --all must not launch a pass")

        monkeypatch.setattr(cli, "_launch_pass", boom)
        monkeypatch.setattr(cli.subprocess, "Popen", boom)
        _write_json(project_local(tiered), {
            "profile": "designer", "tools": [{"name": "uv"}],
            "plugin_choices": {"profile": "designer", "answers": {
                SANDBOX: {"want": False, "decided": "2026-10-01"}}}})

        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(tiered.project)])

        assert rc == 0
        # The designer profile's recommended plugin is not listed by the base,
        # so clearing it marks that plugin for uninstall; the base plugin and
        # the not-recommended one (still asked about, never installed) stay out.
        assert json.loads(project_local(tiered).read_text()) == {
            "profile": "unset", "tools": [{"name": "uv"}],
            "plugins": [{"ref": PROTOTYPING, "uninstall": True}]}

    def test_creates_the_file_when_none_existed(self, tiered, capsys):
        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(tiered.project)])
        assert rc == 0
        assert json.loads(project_local(tiered).read_text()) == {"profile": "unset"}

    def test_refuses_while_a_pass_holds_the_lock_and_writes_nothing(
            self, tiered, capsys):
        local = project_local(tiered)
        _write_json(local, {"profile": "designer"})
        before = local.read_bytes()

        with proc_lock.engine_lock(str(tiered.data_dir)):
            rc = cli.main(["profile", "clear", "--all",
                           "--project-dir", str(tiered.project)])

        assert rc == 2
        assert "running" in capsys.readouterr().err
        assert local.read_bytes() == before

    def test_user_level_profile_is_masked_and_reported(self, tiered, capsys):
        _write_json(user_local(tiered), {"profile": "designer"})
        cli.main(["profile", "status", "--json", "--project-dir", str(tiered.project)])
        assert _json_out(capsys)["selected"] == "designer"

        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(tiered.project)])

        assert rc == 0
        assert "masked" in capsys.readouterr().err
        cli.main(["profile", "status", "--json", "--project-dir", str(tiered.project)])
        status = _json_out(capsys)
        assert status["status"] == "unselected" and status["selected"] is None
        # The user-level file itself is untouched.
        assert json.loads(user_local(tiered).read_text()) == {"profile": "designer"}

    def test_no_warning_when_there_is_no_user_level_profile(self, tiered, capsys):
        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(tiered.project)])
        assert rc == 0
        assert "masked" not in capsys.readouterr().err

    def test_a_malformed_local_file_is_not_rewritten(self, tiered, capsys):
        local = project_local(tiered)
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text("{broken")
        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(tiered.project)])
        assert rc == 1
        assert local.read_text() == "{broken"

    @pytest.mark.parametrize("other", ["--user", "--project"])
    def test_all_excludes_the_target_flags(self, tiered, other):
        with pytest.raises(SystemExit) as exc:
            cli.main(["profile", "clear", "--all", other,
                      "--project-dir", str(tiered.project)])
        assert exc.value.code == 2

    def test_plain_clear_is_unchanged(self, tiered, capsys):
        _write_json(project_local(tiered), {"profile": "designer"})
        rc = cli.main(["profile", "clear", "--project", "--project-dir", str(tiered.project)])
        assert rc == 0
        assert "profile" not in json.loads(project_local(tiered).read_text())


# --------------------------------------------------------------------------
# The candidate reader and the engine reader must not drift apart.
# --------------------------------------------------------------------------

class TestLayerReaderParity:

    @pytest.mark.parametrize("local, user", [
        (None, None),
        (None, {"profile": "designer"}),
        ({"profile": "designer"}, None),
        ({"profile": "unset"}, {"profile": "engineer"}),
        ({"profile": "designer", "plugin_choices": {
            "profile": "designer",
            "answers": {SANDBOX: {"want": False, "decided": "2026-10-01"}}}}, None),
        ({"profile": "engineer", "plugin_choices": {
            "profile": "designer", "answers": {}}}, {"profile": "designer"}),
    ])
    def test_cli_layers_resolve_like_the_engines(self, tiered, local, user):
        if local is not None:
            _write_json(project_local(tiered), local)
        if user is not None:
            _write_json(user_local(tiered), user)

        layers, problems = cli._read_layers(tiered.project)
        mine = bootstrap_profiles.resolve_layers(
            layers, project_dir=str(tiered.project), home=str(tiered.home))
        theirs = bootstrap_engine._load_layered_manifests_ex(str(tiered.project))

        assert problems == []
        assert mine[0] == theirs[0]
        assert mine[1] == theirs[2]


# --------------------------------------------------------------------------
# Switching profiles writes `uninstall: true` entries to the project-local file
# --------------------------------------------------------------------------

BASEP = "mkt:basep"
PARENT_REQ = "mkt:parent-req"
DES_REQ = "mkt:des-req"
DES_REC = "mkt:des-rec"
SHARED = "mkt:shared"
ENG_REQ = "mkt:eng-req"

# parent <- designer, other; engineer is standalone. basep is in the BASE list
# (install auto) AND tiered by designer; shared is recommended by designer and
# only not_recommended by engineer (listed, so never marked); sandbox is a
# base not-recommended plugin.
SWITCH_MANIFEST = {
    "plugins": [
        {"ref": BASEP, "install": "auto"},
        _tiered(SANDBOX, "not_recommended", "Sandboxes agent shells.", "Rarely needed."),
    ],
    "profiles": {
        "parent": {"plugins": [_tiered(PARENT_REQ, "required", "Core.", "Core missing.")]},
        "designer": {
            "extends": ["parent"],
            "plugins": [
                _tiered(DES_REQ, "required", "Design core.", "Design missing."),
                _tiered(DES_REC, "recommended", "Mock up fast.", "Extra commands."),
                _tiered(SHARED, "recommended", "Shared tool.", "Not needed."),
                _tiered(BASEP, "recommended", "Base tool.", "Not needed."),
                _tiered(ENG_ADV, "not_recommended", "Deep tooling.", "Engineering only."),
            ],
        },
        "engineer": {
            "plugins": [
                _tiered(ENG_REQ, "required", "Engineering core.", "Missing."),
                _tiered(SHARED, "not_recommended", "Shared tool.", "Not part of it."),
            ],
        },
        "other": {
            "extends": ["parent"],
            "plugins": [_tiered(DES_REQ, "recommended", "Design core.", "Not needed.")],
        },
    },
}


@pytest.fixture
def switch(env, passes):
    _write_json(project_manifest(env), SWITCH_MANIFEST)
    return env


def _uninstall_refs(env):
    data = json.loads(project_local(env).read_text())
    return [e["ref"] for e in data.get("plugins", []) if e.get("uninstall") is True]


class TestSwitchWritesUninstallEntries:

    def test_apply_marks_what_the_old_chain_required_or_recommended(
            self, switch, capsys):
        _write_json(project_local(switch), {
            "profile": "designer", "tools": [{"name": "uv"}],
            "plugins": [{"ref": "mkt:keep", "install": "manual"}]})

        rc, out = _apply(switch, capsys, profile="engineer")

        assert rc == 0
        written = json.loads(project_local(switch).read_text())
        assert written["profile"] == "engineer"
        assert written["tools"] == [{"name": "uv"}]
        # Parent first (extends), then designer; the base plugin, the plugin
        # engineer still lists (as not_recommended) and the not-recommended
        # ones are all left alone.
        assert written["plugins"] == [
            {"ref": "mkt:keep", "install": "manual"},
            {"ref": PARENT_REQ, "uninstall": True},
            {"ref": DES_REQ, "uninstall": True},
            {"ref": DES_REC, "uninstall": True},
        ]
        assert "uninstall entries written to" in out.out
        for ref in (PARENT_REQ, DES_REQ, DES_REC):
            assert ref in out.out
        for ref in (BASEP, SHARED, SANDBOX, ENG_ADV):
            assert ref not in out.out

    def test_a_self_registered_user_layer_entry_does_not_block_the_uninstall(
            self, switch, capsys):
        # bootstrap self-registers each dependent plugin in ~/.claude/
        # bootstrap.local.json as {"ref", "install": "manual"}; that listing in
        # the merged base must not suppress the switch's uninstall entry.
        _write_json(user_local(switch), {"plugins": [
            {"ref": PARENT_REQ, "install": "manual"},
            {"ref": DES_REQ, "install": "manual"}]})
        _write_json(project_local(switch), {"profile": "designer"})

        rc, _out = _apply(switch, capsys, profile="engineer")

        assert rc == 0
        refs = _uninstall_refs(switch)
        assert PARENT_REQ in refs and DES_REQ in refs

    def test_apply_honors_extends_of_the_new_profile(self, switch, capsys):
        # other extends parent and recommends des-req: parent-req and des-req
        # are listed in the new chain, so only the rest is marked.
        _write_json(project_local(switch), {"profile": "designer"})
        rc, _out = _apply(switch, capsys, profile="other")
        assert rc == 0
        assert _uninstall_refs(switch) == [DES_REC, SHARED]

    def test_apply_drops_entries_for_what_the_new_profile_wants(self, switch, capsys):
        _write_json(project_local(switch), {
            "profile": "engineer",
            "plugins": [
                {"ref": DES_REQ, "uninstall": True},
                {"ref": DES_REC, "uninstall": True},
                {"ref": "mkt:keep", "uninstall": True},
                {"ref": PARENT_REQ, "uninstall": True, "min_version": "1.0"},
            ]})

        rc, out = _apply(switch, capsys, profile="designer")

        assert rc == 0
        written = json.loads(project_local(switch).read_text())
        # Wanted plugins lose their entry; a hand-written entry with other keys
        # loses only the mark; an unrelated uninstall entry stays; and the
        # engineer profile just left is retired in the same write.
        assert written["plugins"] == [
            {"ref": "mkt:keep", "uninstall": True},
            {"ref": PARENT_REQ, "min_version": "1.0"},
            {"ref": ENG_REQ, "uninstall": True},
        ]
        assert "uninstall entries removed from" in out.out
        assert DES_REQ in out.out and DES_REC in out.out

    def test_reapplying_the_same_profile_writes_no_entries(self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})
        rc, out = _apply(switch, capsys, profile="designer")
        assert rc == 0
        assert "plugins" not in json.loads(project_local(switch).read_text())
        assert "uninstall entries" not in out.out

    def test_first_selection_marks_nothing(self, switch, capsys):
        rc, out = _apply(switch, capsys, profile="designer")
        assert rc == 0
        assert "plugins" not in json.loads(project_local(switch).read_text())
        assert "uninstall entries" not in out.out

    def test_switching_back_and_forth_does_not_pile_up_entries(self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})
        _apply(switch, capsys, profile="engineer")
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC]
        _apply(switch, capsys, profile="designer")
        # Back on designer: its own plugins lose their entries, engineer's get one.
        assert _uninstall_refs(switch) == [ENG_REQ]
        _apply(switch, capsys, profile="engineer")
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC]

    def test_set_to_another_profile_writes_the_entries_in_the_same_file(
            self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})

        rc = cli.main(["profile", "set", "engineer", "--project-dir", str(switch.project)])

        assert rc == 0
        written = json.loads(project_local(switch).read_text())
        assert written["profile"] == "engineer"
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC]
        assert "uninstall entries written to" in capsys.readouterr().out

    def test_set_none_marks_everything_the_base_does_not_list(self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})
        rc = cli.main(["profile", "set", "none", "--project-dir", str(switch.project)])
        assert rc == 0
        # shared is no longer listed at all once no profile is applied.
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC, SHARED]
        assert json.loads(project_local(switch).read_text())["profile"] == "none"

    def test_set_drops_entries_the_new_profile_wants(self, switch, capsys):
        _write_json(project_local(switch), {
            "profile": "engineer",
            "plugins": [{"ref": ENG_REQ, "uninstall": True}]})
        rc = cli.main(["profile", "set", "engineer", "--project-dir", str(switch.project)])
        assert rc == 0
        assert _uninstall_refs(switch) == []

    def test_set_with_user_target_still_writes_entries_to_the_project_file(
            self, switch, capsys):
        _write_json(user_local(switch), {"profile": "designer"})

        rc = cli.main(["profile", "set", "engineer", "--user",
                       "--project-dir", str(switch.project)])

        assert rc == 0
        assert json.loads(user_local(switch).read_text()) == {"profile": "engineer"}
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC]
        assert "profile" not in json.loads(project_local(switch).read_text())

    def test_set_user_target_masked_by_a_project_selection_marks_nothing(
            self, switch, capsys):
        # The project-local selection wins, so this project's profile is
        # unchanged by a user-level switch.
        _write_json(project_local(switch), {"profile": "designer"})
        _write_json(user_local(switch), {"profile": "designer"})
        rc = cli.main(["profile", "set", "engineer", "--user",
                       "--project-dir", str(switch.project)])
        assert rc == 0
        assert _uninstall_refs(switch) == []

    def test_clear_all_marks_what_the_base_does_not_list_and_runs_no_pass(
            self, switch, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("clear --all must not launch a pass")

        monkeypatch.setattr(cli, "_launch_pass", boom)
        _write_json(project_local(switch), {
            "profile": "designer",
            "plugins": [{"ref": "mkt:keep", "install": "manual"}]})

        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(switch.project)])

        assert rc == 0
        written = json.loads(project_local(switch).read_text())
        assert written["profile"] == "unset"
        assert written["plugins"][0] == {"ref": "mkt:keep", "install": "manual"}
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC, SHARED]
        out = capsys.readouterr().out
        assert "uninstall entries written to" in out
        assert "none runs now" in out

    def test_clear_all_with_nothing_selected_marks_nothing(self, switch, capsys):
        rc = cli.main(["profile", "clear", "--all", "--project-dir", str(switch.project)])
        assert rc == 0
        assert json.loads(project_local(switch).read_text()) == {"profile": "unset"}

    def test_the_tracked_manifest_and_settings_are_never_written(self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})
        manifest_before = project_manifest(switch).read_bytes()

        _apply(switch, capsys, profile="engineer")
        cli.main(["profile", "clear", "--all", "--project-dir", str(switch.project)])

        assert project_manifest(switch).read_bytes() == manifest_before
        assert not (switch.project / ".claude" / "settings.json").exists()

    def test_a_malformed_plugins_value_refuses_without_writing(self, switch, capsys):
        local = project_local(switch)
        _write_json(local, {"profile": "designer", "plugins": {}})
        before = local.read_bytes()

        rc, out = _apply(switch, capsys, profile="engineer")

        assert rc == 1
        assert "'plugins' is not a list" in out.err
        assert local.read_bytes() == before

    def test_plain_clear_marks_what_the_base_does_not_list_and_runs_no_pass(
            self, switch, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("clear must not launch a pass")

        monkeypatch.setattr(cli, "_launch_pass", boom)
        _write_json(project_local(switch), {
            "profile": "designer",
            "plugins": [{"ref": "mkt:keep", "install": "manual"}]})

        rc = cli.main(["profile", "clear", "--project-dir", str(switch.project)])

        assert rc == 0
        written = json.loads(project_local(switch).read_text())
        assert "profile" not in written
        assert written["plugins"][0] == {"ref": "mkt:keep", "install": "manual"}
        # No profile is selected afterwards, so shared (also recommended by
        # designer) is no longer listed either.
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC, SHARED]
        assert "uninstall entries written to" in capsys.readouterr().out

    def test_plain_clear_with_a_user_level_selection_underneath_marks_nothing(
            self, switch, capsys):
        # Removing the project's key re-exposes the user-level selection, which
        # is the same profile, so this project's effective profile is unchanged.
        _write_json(project_local(switch), {"profile": "designer"})
        _write_json(user_local(switch), {"profile": "designer"})
        rc = cli.main(["profile", "clear", "--project", "--project-dir", str(switch.project)])
        assert rc == 0
        assert "plugins" not in json.loads(project_local(switch).read_text())

    def test_plain_clear_to_a_different_user_level_profile_marks_the_difference(
            self, switch, capsys):
        _write_json(project_local(switch), {"profile": "designer"})
        _write_json(user_local(switch), {"profile": "engineer"})
        rc = cli.main(["profile", "clear", "--project", "--project-dir", str(switch.project)])
        assert rc == 0
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC]

    def test_plain_clear_user_target_writes_entries_to_the_project_file(
            self, switch, capsys):
        _write_json(user_local(switch), {"profile": "designer"})
        rc = cli.main(["profile", "clear", "--user", "--project-dir", str(switch.project)])
        assert rc == 0
        assert "profile" not in json.loads(user_local(switch).read_text())
        assert _uninstall_refs(switch) == [PARENT_REQ, DES_REQ, DES_REC, SHARED]
