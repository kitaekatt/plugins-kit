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
        # The legacy layer is deliberately excluded from a terminal resolution
        # (layered_bootstrap.py calls _load_layered_manifests_ex the same
        # way): data_dir must be None, never the CLI's own plugin data dir.
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
            return _ExitedProcess()

        monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

        rc = cli.main([
            "profile", "set", "engineer", "--project-dir", str(env.project),
        ])
        assert rc == 0
        cmd = seen["cmd"]
        assert cmd[cmd.index("--project-dir") + 1] == str(env.project)

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

    def test_clear_with_no_existing_file_is_a_no_op_success(self, env, capsys):
        rc = cli.main([
            "profile", "clear", "--user", "--project-dir", str(env.project),
        ])
        assert rc == 0
        assert not user_local(env).exists()
