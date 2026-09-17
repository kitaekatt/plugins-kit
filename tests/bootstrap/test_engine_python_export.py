"""The engine exports BOOTSTRAP_PYTHON / BOOTSTRAP_PROJECT_PYTHON where it runs commands.

These drive engine._main in-process (the harness from
test_engine_ran_version.py: empty self_setup, isolated HOME) and
layered_bootstrap.run_layered_bootstrap, so they observe the WIRING rather
than the helper: a helper-level test stays green when the engine stops
calling it. The conftest autouse fixture clears both names around every test.

Every project lives under the isolated HOME so the walk-up (which stops after
checking $HOME) never reaches a venv on the developer's machine.
"""

import json
import os
import socket
import sys
import types
from pathlib import Path

import pytest

import bootstrap_lib
from bootstrap_lib import engine, interpreter_env, session_env, venv_check
from bootstrap_lib.interpreter_env import (
    ENGINE_VAR,
    FACT_ID,
    ISOLATION_ENV,
    LAYERED_KEY,
    PROJECT_VAR,
    RECORD_SUBDIR,
    normalize_path,
    project_key,
    shell_path,
    standalone_python,
)
from bootstrap_lib.platform_detect import detect_os

from bootstrap.test_engine_ran_version import _fake_root
from bootstrap.test_interpreter_env import (
    fake_venv_python,
    git_bash_or_skip,
    make_exe,
    make_venv,
    norm,
)

PROBE = "bp-python-probe"
PROBE_CHECK = 'test -x "$BOOTSTRAP_PYTHON" && "$BOOTSTRAP_PYTHON" -c "import zipfile"'
PROJECT_VENV = {"check_imports": []}  # non-empty: an empty dict is falsy and skipped
PYPROJECT = '[project]\nname = "bp-probe"\nversion = "0.1.0"\n'
DISCOVERY_LINE = (
    f"python: {ENGINE_VAR} and {PROJECT_VAR} are exported -- invoke Python "
    f"through them, never bare python/python3 (/bootstrap fact {FACT_ID})")


@pytest.fixture(autouse=True)
def _no_ambient_interpreter_env(monkeypatch):
    """`uv run` exports VIRTUAL_ENV, which the normative rule ranks second."""
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CLAUDE_ENV_FILE",
                 "CLAUDE_BOOTSTRAP_DATA_ROOT"):
        monkeypatch.delenv(name, raising=False)
    session_env.reset()
    yield
    session_env.reset()


def _home(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return home


def _project(tmp_path, manifest, pyproject=True):
    project = _home(tmp_path) / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "bootstrap.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    if pyproject:
        (project / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return project


def _write_user_layer(tmp_path, manifest, name="bootstrap.json"):
    (_home(tmp_path) / ".claude" / name).write_text(
        json.dumps(manifest), encoding="utf-8")


def _run_main(tmp_path, monkeypatch, project, *extra):
    """One verbose, background, HOME-isolated pass; returns bootstrap.log text."""
    root = _fake_root(tmp_path, "0.120.0") if not (tmp_path / "plugin_root").exists() \
        else str(tmp_path / "plugin_root")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    iso_home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(iso_home))
    monkeypatch.setenv("USERPROFILE", str(iso_home))
    # _main's repair_path rewrites this process's PATH; restore it afterwards.
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    monkeypatch.setattr("sys.argv", [
        "bootstrap_engine.py",
        "--plugin-root", root,
        "--data-dir", str(data_dir),
        "--project-dir", str(project),
        "--background",
        "--verbose",  # ok entries reach bootstrap.log
        *extra,
    ])
    engine._main()
    log = data_dir / "bootstrap.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _fake_venv_step(fail=False):
    """Stand-in for _process_venv_def: no uv sync, only the venv interpreter."""
    targets = []

    def fake(venv_def, data_dir, plugin_root, prefix, label, action_entries,
             ok_entries, failures, **_kw):
        targets.append(data_dir)
        fake_venv_python(Path(data_dir) / ".venv")
        if fail:
            action_entries.append("project_venv: FAILED - fake sync failure")
            failures.append({"type": "project_venv", "plugin": "config",
                             "message": "fake sync failure"})
        else:
            ok_entries.append("project_venv: ok - fake venv")

    return fake, targets


def _refuse_venv_step(*_a, **_k):
    raise AssertionError("project_venv must not sync without a pyproject.toml")


def _expected_project_python(target_dir):
    return shell_path(venv_check._find_python(os.path.join(target_dir, ".venv")))


def _engine_default():
    return normalize_path(shell_path(sys.executable))


def _record(tmp_path, key):
    return tmp_path / "data" / RECORD_SUBDIR / key


class TestMainExportsEnginePython:
    def test_main_pass_exports_engine_python_to_tool_check(self, tmp_path, monkeypatch):
        """A layered tool check sees BOOTSTRAP_PYTHON; the log names it."""
        git_bash_or_skip()
        project = _project(
            tmp_path, {"tools": [{"name": PROBE, "check": PROBE_CHECK}]},
            pyproject=False)
        log = _run_main(tmp_path, monkeypatch, project)
        assert f"config: {PROBE}: ok - check command passed" in log, log
        assert f"python: {ENGINE_VAR}={shell_path(sys.executable)}" in log, log


class TestStep3dProjectExport:
    def test_project_var_set_after_successful_project_venv(self, tmp_path, monkeypatch):
        fake, targets = _fake_venv_step()
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        project = _project(tmp_path, {"project_venv": PROJECT_VENV})
        log = _run_main(tmp_path, monkeypatch, project)
        assert len(targets) == 1
        expected = _expected_project_python(targets[0])
        assert os.environ.get(PROJECT_VAR) == expected
        assert (f"config: project_venv: exported {PROJECT_VAR}={expected} (process)"
                in log), log

    def test_unverified_venv_is_not_the_default(self, tmp_path, monkeypatch):
        """A .venv the step skipped (no pyproject.toml, no pyvenv.cfg) is never
        exported; the project default is the engine interpreter."""
        monkeypatch.setattr(engine, "_process_venv_def", _refuse_venv_step)
        monkeypatch.setenv(PROJECT_VAR, "/inherited/project/python")
        project = _project(tmp_path, {"project_venv": PROJECT_VENV}, pyproject=False)
        fake_venv_python(project / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert os.environ.get(PROJECT_VAR) == _engine_default()
        assert "project_venv: ok - no pyproject.toml" in log, log
        assert f"exported {PROJECT_VAR}" not in log, log

    def test_failed_venv_keeps_the_default(self, tmp_path, monkeypatch):
        fake, targets = _fake_venv_step(fail=True)
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        monkeypatch.setenv(PROJECT_VAR, "/inherited/project/python")
        project = _project(tmp_path, {"project_venv": PROJECT_VENV})
        log = _run_main(tmp_path, monkeypatch, project)
        assert len(targets) == 1  # the step ran and left an interpreter behind
        assert os.environ.get(PROJECT_VAR) == _engine_default()
        assert f"exported {PROJECT_VAR}" not in log, log


class TestProjectDefaultWiring:
    """interface-v3 section 3: pass start, Step 3c, Step 3d record."""

    def test_step3c_tools_see_project_default(self, tmp_path, monkeypatch):
        """T10f: a layered tool check runs with the MANIFEST-AWARE default.

        The venv sits under project_venv.subdir, which only Step 3c knows
        (the pass-start resolution reads no manifest and finds nothing).
        """
        git_bash_or_skip()
        out = tmp_path / "seen.txt"
        check = (f"printf '%s' \"$BOOTSTRAP_PROJECT_PYTHON\" > "
                 f"'{out.as_posix()}'")
        project = _project(
            tmp_path, {"tools": [{"name": PROBE, "check": check}],
                       "project_venv": {"subdir": "python"}}, pyproject=False)
        venv_python = make_venv(project / "python" / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert out.read_text(encoding="utf-8") == norm(venv_python), log
        assert f"python: {PROJECT_VAR}={_engine_default()} (engine)" in log
        assert f"python: {PROJECT_VAR}={norm(venv_python)} (venv, manifest-aware)" in log

    def test_pass_start_entry_names_value_and_source(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        log = _run_main(tmp_path, monkeypatch, project)
        assert f"python: {PROJECT_VAR}={_engine_default()} (engine)" in log, log

    def test_invalid_project_python_is_logged_and_ignored(self, tmp_path, monkeypatch):
        """T10d: a non-false project_python is one log line and changes nothing."""
        project = _project(tmp_path, {"project_python": "tools/py.exe"},
                           pyproject=False)
        make_exe(project / "tools" / "py.exe")
        venv_python = make_venv(project / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert log.count("python: project_python accepts only false") == 1, log
        assert os.environ[PROJECT_VAR] == norm(venv_python)
        record = _record(tmp_path, project_key(str(project)))
        assert record.read_text(encoding="utf-8") == norm(venv_python) + "\n"

    def test_opt_out_suppresses_verified_venv_and_is_recorded(self, tmp_path, monkeypatch):
        """An opted-out project exports no project name, even after a verified
        project_venv, and the record carries the opt-out marker."""
        fake, targets = _fake_venv_step()
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        monkeypatch.setenv(PROJECT_VAR, "/inherited/project/python")
        project = _project(tmp_path, {"project_venv": PROJECT_VENV,
                                      "project_python": False})
        make_venv(project / "elsewhere" / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert len(targets) == 1  # the venv is still provisioned
        assert PROJECT_VAR not in os.environ
        assert f"project_venv: {PROJECT_VAR} not exported" in log, log
        assert f"exported {PROJECT_VAR}" not in log, log
        record = _record(tmp_path, project_key(str(project)))
        assert record.read_text(encoding="utf-8") == interpreter_env.RECORD_OPT_OUT + "\n"

    def test_opt_out_hides_name_from_step3c_commands(self, tmp_path, monkeypatch):
        git_bash_or_skip()
        out = tmp_path / "seen.txt"
        check = (f"printf '%s' \"${{BOOTSTRAP_PROJECT_PYTHON-unset}}\" > "
                 f"'{out.as_posix()}'")
        project = _project(
            tmp_path, {"tools": [{"name": PROBE, "check": check}],
                       "project_python": False}, pyproject=False)
        make_venv(project / ".venv")
        _run_main(tmp_path, monkeypatch, project)
        assert out.read_text(encoding="utf-8") == "unset"

    def test_record_written_after_step3d_and_read_by_always_lane(self, tmp_path, monkeypatch):
        """T10g: the full pass records the VERIFIED venv; the always lane reuses it."""
        fake, targets = _fake_venv_step()
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        project = _project(tmp_path, {"project_venv": PROJECT_VENV})
        _run_main(tmp_path, monkeypatch, project)
        expected = _expected_project_python(targets[0])
        record = _record(tmp_path, project_key(str(project)))
        assert record.read_text(encoding="utf-8") == expected + "\n"
        # The fake venv has no pyvenv.cfg, so the walk-up cannot find it: only
        # the record can produce this value in the always lane.
        Path(expected).chmod(0o755)  # a record must name an executable
        monkeypatch.delenv(PROJECT_VAR)
        log = _run_main(tmp_path, monkeypatch, project, "--run-kind", "always")
        assert os.environ[PROJECT_VAR] == expected
        # A verbose always lane logs the value it exported.
        assert f"python: {PROJECT_VAR}={expected} (record)" in log, log

    def test_project_default_exported_before_always_lane(self, tmp_path, monkeypatch):
        """T10e: an always env_checks command sees the RECORDED project value."""
        git_bash_or_skip()
        home = _home(tmp_path)
        out = tmp_path / "always-seen.txt"
        (home / ".claude" / "env.json").write_text(json.dumps({
            "machines": {socket.gethostname(): {"os": detect_os()}},
            "env_checks": [{
                "name": "pp-probe", "cadence": "always",
                "check": ("printf '%s' \"${BOOTSTRAP_PROJECT_PYTHON-unset}\" > "
                          f"'{out.as_posix()}'"),
            }],
        }), encoding="utf-8")
        project = _project(tmp_path, {}, pyproject=False)
        recorded = norm(make_exe(tmp_path / "recorded" / "python.exe"))
        record = _record(tmp_path, "hook-key")
        record.parent.mkdir(parents=True)
        record.write_text(recorded + "\n", encoding="utf-8")
        _run_main(tmp_path, monkeypatch, project,
                  "--run-kind", "always", "--project-key", "hook-key")
        assert out.read_text(encoding="utf-8") == recorded
        # The opt-out marker: the always lane exports no project name at all.
        record.write_text(interpreter_env.RECORD_OPT_OUT + "\n", encoding="utf-8")
        make_venv(project / ".venv")
        _run_main(tmp_path, monkeypatch, project,
                  "--run-kind", "always", "--project-key", "hook-key")
        assert out.read_text(encoding="utf-8") == "unset"

    def test_project_key_flag_names_the_record(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        venv_python = make_venv(project / ".venv")
        _run_main(tmp_path, monkeypatch, project, "--project-key", "abc123")
        assert _record(tmp_path, "abc123").read_text(encoding="utf-8") == (
            norm(venv_python) + "\n")
        assert not _record(tmp_path, project_key(str(project))).exists()

    def test_global_key_never_recorded(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        make_venv(project / ".venv")
        log = _run_main(tmp_path, monkeypatch, project, "--project-key",
                        interpreter_env.GLOBAL_KEY)
        assert not (tmp_path / "data" / RECORD_SUBDIR).exists()
        assert (f"python: project record {interpreter_env.GLOBAL_KEY}: not kept "
                "(no per-project key)") in log, log

    def test_stale_record_removed_when_project_resolves_to_engine(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        record = _record(tmp_path, project_key(str(project)))
        record.parent.mkdir(parents=True)
        record.write_text(norm(make_exe(tmp_path / "old" / "python.exe")) + "\n")
        log = _run_main(tmp_path, monkeypatch, project)
        assert not record.exists()
        assert "removed (project resolves to the engine interpreter)" in log, log

    def test_parse_error_leaves_record_alone(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        (project / ".claude" / "bootstrap.local.json").write_text("{broken")
        record = _record(tmp_path, project_key(str(project)))
        record.parent.mkdir(parents=True)
        stale = norm(make_exe(tmp_path / "old" / "python.exe")) + "\n"
        record.write_text(stale)
        _run_main(tmp_path, monkeypatch, project)
        assert record.read_text() == stale


class TestStep3cInterpreterEnvSettings:
    """Step 3c reads interpreter_env from USER layers and hands it on."""

    @pytest.fixture
    def captured(self, monkeypatch):
        calls = []
        real = engine._process_interpreter_env

        def spy(persist, shell_hook, **kw):
            calls.append((persist, shell_hook, kw["parse_errors"]))
            return real(persist, shell_hook, **kw)

        monkeypatch.setattr(engine, "_process_interpreter_env", spy)
        return calls

    def test_user_layer_opt_out_reaches_step(self, tmp_path, monkeypatch, captured):
        _write_user_layer(tmp_path, {LAYERED_KEY: {"persist": False}})
        project = _project(tmp_path, {}, pyproject=False)
        _run_main(tmp_path, monkeypatch, project)
        assert captured == [(False, True, False)]

    def test_project_layer_is_ignored(self, tmp_path, monkeypatch, captured):
        project = _project(tmp_path, {LAYERED_KEY: {"persist": False,
                                                    "shell_hook": False}},
                           pyproject=False)
        log = _run_main(tmp_path, monkeypatch, project)
        assert captured == [(True, True, False)]
        assert f"python: {LAYERED_KEY} in a project manifest is ignored" in log, log

    def test_user_profile_chain_counts(self, tmp_path, monkeypatch, captured):
        _write_user_layer(tmp_path, {"profiles": {
            "quiet": {LAYERED_KEY: {"shell_hook": False}}}})
        _write_user_layer(tmp_path, {"profile": "quiet"}, name="bootstrap.local.json")
        project = _project(tmp_path, {}, pyproject=False)
        _run_main(tmp_path, monkeypatch, project)
        assert captured == [(True, False, False)]

    def test_parse_error_is_signalled(self, tmp_path, monkeypatch, captured):
        _write_user_layer(tmp_path, {LAYERED_KEY: {"persist": False}})
        (_home(tmp_path) / ".claude" / "bootstrap.local.json").write_text("{broken")
        project = _project(tmp_path, {}, pyproject=False)
        _run_main(tmp_path, monkeypatch, project)
        assert captured == [(False, False, True)]

    def test_step_runs_before_layered_manifest(self, tmp_path, monkeypatch):
        order = []
        monkeypatch.setattr(engine, "_process_interpreter_env",
                            lambda *a, **k: order.append("interp") or ([], [], []))
        real = engine._process_manifest

        def spy(*a, **k):
            order.append("manifest:" + k.get("plugin_name", "?"))
            return real(*a, **k)

        monkeypatch.setattr(engine, "_process_manifest", spy)
        project = _project(tmp_path, {"tools": []}, pyproject=False)
        _run_main(tmp_path, monkeypatch, project)
        assert order[:2] == ["interp", "manifest:config"]


class TestSessionBlock:
    def test_session_env_records_both_names(self, tmp_path, monkeypatch, persist_env):
        """T11e: a persisting pass buffers both names; flush writes both.

        BOOTSTRAP_PYTHON reaches the block only through the persistence step
        (begin_pass is process-only); the project name through the defaults.
        """
        env_file = tmp_path / "sessionstart-hook-0.sh"
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        project = _project(tmp_path, {}, pyproject=False)
        venv_python = make_venv(project / ".venv")
        _run_main(tmp_path, monkeypatch, project)
        standalone = standalone_python(str(persist_env.home))
        assert session_env._pending[ENGINE_VAR] == standalone
        assert session_env._pending[PROJECT_VAR] == norm(venv_python)
        assert session_env.flush() == 2
        text = env_file.read_text(encoding="utf-8")
        assert f"export {ENGINE_VAR}={standalone}\n" in text
        assert f"export {PROJECT_VAR}={norm(venv_python)}\n" in text
        assert len(persist_env.shell_calls) == 1

    def test_opt_out_removes_prelude_line(self, tmp_path, monkeypatch):
        """An opted-out project's name is dropped from the env block, even when
        the hook prelude wrote it before the pass."""
        env_file = tmp_path / "sessionstart-hook-0.sh"
        env_file.write_text(f"export {PROJECT_VAR}=/prelude/python\n", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_ENV_FILE", str(env_file))
        project = _project(tmp_path, {"project_python": False}, pyproject=False)
        make_venv(project / ".venv")
        _run_main(tmp_path, monkeypatch, project)
        session_env.flush()
        text = env_file.read_text(encoding="utf-8")
        assert f"export {PROJECT_VAR}=" not in text


class TestOptOutComesFromProjectLayers:
    """`project_python: false` is a PROJECT opt-out (user ruling): the user
    layer never opts a project out, in the full pass or in `bootstrap run`.
    Cross-implementation agreement for the same boundary: T14
    (test_project_python_resolvers_agree.py)."""

    USER_NOTE = "python: project_python in a user manifest is ignored"

    def test_user_layer_opt_out_is_ignored_with_a_note(self, tmp_path, monkeypatch):
        # Revert that turns this RED: make _project_python_opt_out read
        # user_layers + project_layers.
        _write_user_layer(tmp_path, {"project_python": False})
        project = _project(tmp_path, {}, pyproject=False)
        venv_python = make_venv(project / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert os.environ[PROJECT_VAR] == norm(venv_python)
        assert log.count(self.USER_NOTE) == 1, log
        record = _record(tmp_path, project_key(str(project)))
        assert record.read_text(encoding="utf-8") == norm(venv_python) + "\n"

    def test_project_local_layer_opts_out(self, tmp_path, monkeypatch):
        project = _project(tmp_path, {}, pyproject=False)
        (project / ".claude" / "bootstrap.local.json").write_text(
            json.dumps({"project_python": False}), encoding="utf-8")
        make_venv(project / ".venv")
        log = _run_main(tmp_path, monkeypatch, project)
        assert PROJECT_VAR not in os.environ
        assert self.USER_NOTE not in log

    def test_session_in_home_reads_no_project_layer(self, tmp_path, monkeypatch):
        """A pass whose --project-dir IS the home directory: its
        .claude/bootstrap.json is the user layer, so it cannot opt out."""
        home = _home(tmp_path)
        _write_user_layer(tmp_path, {"project_python": False})
        venv_python = make_venv(home / ".venv")
        log = _run_main(tmp_path, monkeypatch, home)
        assert os.environ[PROJECT_VAR] == norm(venv_python)
        assert log.count(self.USER_NOTE) == 1, log

    def test_layered_run_ignores_user_layer_opt_out(self, tmp_path, monkeypatch):
        from bootstrap_lib.layered_bootstrap import run_layered_bootstrap
        home = _home(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        _write_user_layer(tmp_path, {"project_python": False})
        project = _project(tmp_path, {}, pyproject=False)
        venv_python = make_venv(project / ".venv")
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        data = tmp_path / "data"
        data.mkdir()
        result = run_layered_bootstrap(project, plugin, data, detect_os())
        assert os.environ[PROJECT_VAR] == norm(venv_python)
        assert sum(self.USER_NOTE in str(d) for d in result.details) == 1


class TestStep3cShellHook:
    def test_step3c_invokes_shell_hook_once(self, tmp_path, monkeypatch, persist_env):
        """T16: one full pass calls shell_hook.ensure exactly once, with the
        user layer's shell_hook flag and the injected (redirected) home.

        Revert that turns this RED: delete the `_shell_hook.ensure(...)` call
        in _process_interpreter_env (no call), or pass `enabled=True`.
        """
        _write_user_layer(tmp_path, {LAYERED_KEY: {"shell_hook": False}})
        project = _project(tmp_path, {}, pyproject=False)
        _run_main(tmp_path, monkeypatch, project)
        assert persist_env.shell_calls == [
            {"enabled": False, "home": str(persist_env.home), "documents": None}]

    def test_shell_hook_is_imported_unguarded(self):
        """The ImportError guard the parallel units needed is gone: a broken
        shell_hook module fails loudly instead of logging 'unavailable'."""
        import inspect
        source = inspect.getsource(engine._process_interpreter_env)
        assert "from . import shell_hook as _shell_hook" in source
        assert "except ImportError" not in source
        assert "shell hook unavailable" not in source


# ---------------------------------------------------------------------------
# _process_interpreter_env (persistence + shell hook), driven directly
# ---------------------------------------------------------------------------


@pytest.fixture
def persist_env(tmp_path, monkeypatch):
    """Opt in to persistence under a tmp HOME with a fake standalone python."""
    home = _home(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(ISOLATION_ENV, raising=False)
    make_exe(Path(standalone_python(str(home))))
    calls = []
    fake = types.ModuleType("bootstrap_lib.shell_hook")

    def ensure(data_dir, current_os, *, enabled, home, documents=None):
        calls.append({"enabled": enabled, "home": home, "documents": documents})
        return [], ["shell hook: fake ok"], []

    fake.ensure = ensure
    monkeypatch.setitem(sys.modules, "bootstrap_lib.shell_hook", fake)
    monkeypatch.setattr(bootstrap_lib, "shell_hook", fake, raising=False)
    return types.SimpleNamespace(home=home, data=tmp_path / "data",
                                 shell_calls=calls)


def _interp(persist_env, persist=True, shell_hook=True, current_os="ubuntu", **kw):
    return engine._process_interpreter_env(
        persist, shell_hook, current_os=current_os,
        data_dir=str(persist_env.data), **kw)


class TestProcessInterpreterEnv:
    def test_persist_uses_env_var_machinery(self, persist_env, monkeypatch):
        """T11a: the persisted value is standalone_python(), never sys.executable."""
        from bootstrap_lib import env_var_check
        seen = []
        monkeypatch.setattr(env_var_check, "check_env_var",
                            lambda n, v, o: seen.append(("check", n, v)) or
                            types.SimpleNamespace(passed=len(seen) > 1, message="m"))
        monkeypatch.setattr(env_var_check, "set_env_var",
                            lambda n, v, o: seen.append(("set", n, v)) or (True, "set"))
        want = standalone_python(str(persist_env.home))
        actions, _ok, failures = _interp(persist_env)
        assert failures == []
        assert [s[0] for s in seen] == ["check", "set", "check"]
        assert all(s[1:] == (ENGINE_VAR, want) for s in seen)
        assert want != shell_path(sys.executable)
        assert os.environ[ENGINE_VAR] == want
        assert [str(a) for a in actions] == [DISCOVERY_LINE]
        assert persist_env.shell_calls == [
            {"enabled": True, "home": str(persist_env.home), "documents": None}]

    def test_persist_skipped_when_standalone_missing(self, persist_env, monkeypatch):
        """T11b: no executable standalone file -> nothing persisted."""
        from bootstrap_lib import env_var_check
        os.remove(standalone_python(str(persist_env.home)))
        monkeypatch.setattr(env_var_check, "set_env_var", _refuse_venv_step)
        actions, ok, failures = _interp(persist_env)
        assert (actions, failures) == ([], [])
        assert any("not persisted" in e and "not an executable file" in e for e in ok)
        assert not (persist_env.home / ".bashrc").exists()

    def test_first_persist_displays_once_then_verbose(self, persist_env):
        """T11c: the discoverability line fires on the set branch only."""
        first_actions, _, failures = _interp(persist_env)
        assert failures == []
        assert [str(a) for a in first_actions] == [DISCOVERY_LINE]
        rc = (persist_env.home / ".bashrc").read_text()
        assert f"export {ENGINE_VAR}=" in rc
        second_actions, second_ok, _ = _interp(persist_env)
        assert second_actions == []
        assert any(e.startswith(f"python: {ENGINE_VAR} ok - ") for e in second_ok)

    def test_persist_off_by_config(self, persist_env, monkeypatch):
        """T11d: persist=false never sets; shell_hook=false reaches ensure."""
        from bootstrap_lib import env_var_check
        monkeypatch.setattr(env_var_check, "set_env_var", _refuse_venv_step)
        monkeypatch.setattr(env_var_check, "unset_env_var", _refuse_venv_step)
        actions, ok, failures = _interp(persist_env, persist=False, shell_hook=False)
        assert (actions, failures) == ([], [])
        assert f"python: {ENGINE_VAR} not persisted ({LAYERED_KEY}.persist is false)" in ok
        assert persist_env.shell_calls[0]["enabled"] is False

    def test_persist_false_unsets(self, persist_env):
        """T11g: an earlier persisted value is removed once, then steady."""
        _interp(persist_env)
        assert f"export {ENGINE_VAR}=" in (persist_env.home / ".bashrc").read_text()
        actions, _, failures = _interp(persist_env, persist=False)
        assert failures == []
        assert len(actions) == 1 and "no longer persisted" in str(actions[0])
        assert f"export {ENGINE_VAR}=" not in (persist_env.home / ".bashrc").read_text()
        again, ok, _ = _interp(persist_env, persist=False)
        assert again == []
        assert any("not persisted" in e for e in ok)

    @pytest.mark.parametrize("signal", ["isolation", "data_root", "parse_errors"])
    def test_skipped_entirely(self, persist_env, monkeypatch, signal):
        from bootstrap_lib import env_var_check
        for name in ("export_env_var", "set_env_var", "unset_env_var",
                     "check_env_var", "is_env_var_persisted"):
            monkeypatch.setattr(env_var_check, name, _refuse_venv_step)
        kw = {}
        if signal == "isolation":
            monkeypatch.setenv(ISOLATION_ENV, "1")
        elif signal == "data_root":
            monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(persist_env.data))
        else:
            kw["parse_errors"] = True
        actions, ok, failures = _interp(persist_env, **kw)
        assert (actions, failures) == ([], [])
        assert len(ok) == 1 and "skipped" in ok[0]
        assert persist_env.shell_calls == []

    def test_shell_hook_exception_is_logged(self, persist_env, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("profile locked")

        monkeypatch.setattr(sys.modules["bootstrap_lib.shell_hook"], "ensure", boom)
        actions, _, _ = _interp(persist_env)
        assert any("shell hook FAILED - RuntimeError: profile locked" in str(a)
                   for a in actions)


class TestLayeredRunExports:
    """`bootstrap run` / `profile set` never enter _main."""

    @pytest.fixture
    def layered(self, tmp_path, monkeypatch):
        home = _home(tmp_path)
        data = tmp_path / "data"
        data.mkdir()
        plugin = tmp_path / "plugin"
        plugin.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv(PROJECT_VAR, "/inherited/project/python")
        seen = []
        real = engine._process_manifest

        def spy(*a, **k):
            seen.append((os.environ.get(ENGINE_VAR), os.environ.get(PROJECT_VAR)))
            return real(*a, **k)

        monkeypatch.setattr(engine, "_process_manifest", spy)
        return plugin, data, seen

    def _run(self, tmp_path, plugin, data, manifest=None):
        from bootstrap_lib.layered_bootstrap import run_layered_bootstrap
        project = tmp_path / "home" / "project"
        if not project.exists():
            project = _project(tmp_path, manifest or {"project_venv": PROJECT_VENV})
        return run_layered_bootstrap(project, plugin, data, detect_os())

    def test_layered_run_exports_engine_then_project_python(self, tmp_path, monkeypatch, layered):
        plugin, data, seen = layered
        fake, targets = _fake_venv_step()
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        result = self._run(tmp_path, plugin, data)
        engine_python = shell_path(sys.executable)
        # Manifest commands see the engine interpreter and the project DEFAULT
        # (no qualifying venv yet, so the engine interpreter too).
        assert seen == [(engine_python, _engine_default())]
        assert result.failures == []
        expected = _expected_project_python(targets[0])
        assert os.environ.get(PROJECT_VAR) == expected
        assert f"python: {ENGINE_VAR}={engine_python}" in result.checks
        assert f"python: {PROJECT_VAR}={_engine_default()} (engine)" in result.checks
        assert (f"project_venv: exported {PROJECT_VAR}={expected} (process)"
                in result.checks)
        assert not (data / RECORD_SUBDIR).exists()  # the CLI never records

    def test_layered_run_keeps_default_on_failure(self, tmp_path, monkeypatch, layered):
        plugin, data, seen = layered
        fake, targets = _fake_venv_step(fail=True)
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        result = self._run(tmp_path, plugin, data)
        assert len(targets) == 1
        assert len(result.failures) == 1
        assert os.environ.get(PROJECT_VAR) == _engine_default()
        assert not any(f"exported {PROJECT_VAR}" in c for c in result.checks)

    def test_layered_run_honours_opt_out_and_never_persists(self, tmp_path, monkeypatch, layered):
        plugin, data, seen = layered
        monkeypatch.delenv(ISOLATION_ENV, raising=False)
        monkeypatch.setattr(engine, "_process_interpreter_env", _refuse_venv_step)
        fake, targets = _fake_venv_step()
        monkeypatch.setattr(engine, "_process_venv_def", fake)
        project = _project(tmp_path, {"project_python": False,
                                      "project_venv": PROJECT_VENV})
        make_venv(project / ".venv")
        result = self._run(tmp_path, plugin, data)
        assert len(targets) == 1
        assert seen == [(shell_path(sys.executable), None)]
        assert PROJECT_VAR not in os.environ
        assert any(f"{PROJECT_VAR} not exported" in c for c in result.checks)
        assert not (data / RECORD_SUBDIR).exists()
        assert not (tmp_path / "home" / ".bashrc").exists()

    def test_layered_run_logs_invalid_project_python(self, tmp_path, monkeypatch, layered):
        plugin, data, seen = layered
        project = _project(tmp_path, {"project_python": "tools/py.exe"}, pyproject=False)
        venv_python = make_venv(project / ".venv")
        result = self._run(tmp_path, plugin, data)
        assert seen == [(shell_path(sys.executable), norm(venv_python))]
        assert sum("accepts only false" in str(d) for d in result.details) == 1
