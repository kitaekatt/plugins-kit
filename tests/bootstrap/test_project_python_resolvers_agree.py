"""T14: every implementation of the project-interpreter rule agrees.

One directory tree per case runs through all five implementations of the
normative resolution rule (interface-v3 section 1; references/
python-interpreter.md):

* engine -- ``interpreter_env.default_project_python`` with the opt-out the
  engine derives from the project layers (``engine._project_python_opt_out``
  over ``engine._interpreter_env_layers``), no record;
* CLI -- ``bootstrap_cli._resolve_project_python`` (no record);
* bash -- ``bootstrap_resolve_project_python`` from shell/project-python.sh;
* prelude -- session-bootstrap.sh's interpreter block, run through U9's
  harness with no record, reading the line it writes to CLAUDE_ENV_FILE;
* PowerShell -- ``Resolve-BootstrapProjectPython`` (skipped without one).

Each result is normalized the way ``interpreter_env.normalize_path`` does
(forward slashes, upper-case drive letter, ``/c/`` mapped to ``C:/``) and
must be identical; an opted-out tree is ``None`` everywhere (the bash
resolver prints an empty line and returns 1, the prelude writes no project
line, PowerShell returns an empty string). zsh is not installed on this host
or on CI Ubuntu, so the zsh branch is covered statically in
test_shell_hook.py only.

Executability: Python accepts any existing file on Windows, while Git Bash's
``-x`` wants an ``.exe`` name, an ``MZ`` header, or a shebang, so every fake
interpreter here is ``Scripts/python.exe`` with an ``MZ`` header on Windows
and an executable ``bin/python`` elsewhere.

The start directory is the project root in every opt-out case: the engine
reads the project layers of ``--project-dir`` only, while the terminal
implementations walk upward to the nearest directory that opts out or holds a
venv; the two agree when the walk starts at the project root.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from bootstrap_lib import engine, interpreter_env
from bootstrap_lib.interpreter_env import normalize_path, standalone_python

from bootstrap import test_sessionstart_interpreter_env as prelude
from bootstrap import test_shell_hook as sh

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "plugins" / "bootstrap" / "scripts" / "bootstrap_cli.py"
WINDOWS = os.name == "nt"

pytestmark = pytest.mark.skipif(
    sh.BASH is None or prelude.BASH is None, reason="bash not available")


def _load_cli():
    spec = importlib.util.spec_from_file_location("bootstrap_cli_t14", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def _opt_out(directory: Path, leaf: str = "bootstrap.json") -> None:
    (directory / ".claude").mkdir(parents=True, exist_ok=True)
    (directory / ".claude" / leaf).write_text(
        json.dumps({"project_python": False}, indent=2) + "\n", encoding="utf-8")


def _venv(path: Path, *, cfg: bool = True) -> Path:
    return sh._make_venv(path, cfg=cfg)


# Each builder takes (root, home) and returns (start, extra_env, expected),
# where expected is a Path, "standalone", or None (opted out).

def _case_venv(root, home):
    return home / "proj", {}, _venv(home / "proj" / ".venv")


def _case_nested_venv(root, home):
    python = _venv(home / "proj" / ".venv")
    _venv(home / "proj" / "src" / ".venv", cfg=False)  # no pyvenv.cfg: skipped
    start = home / "proj" / "src" / "pkg"
    start.mkdir(parents=True)
    return start, {}, python


def _case_virtual_env(root, home):
    _venv(home / "proj" / ".venv")
    active = sh._make_interp(root / "active")  # an activation needs no cfg
    return home / "proj", {"VIRTUAL_ENV": str(root / "active")}, active


def _case_uv_relative(root, home):
    _venv(home / "proj" / ".venv")
    python = _venv(home / "proj" / "envs" / "dev")
    return home / "proj", {"UV_PROJECT_ENVIRONMENT": "envs/dev"}, python


def _case_uv_absolute(root, home):
    _venv(home / "proj" / ".venv")
    python = _venv(root / "uvabs")
    return home / "proj", {"UV_PROJECT_ENVIRONMENT": str(root / "uvabs")}, python


def _case_opt_out(root, home):
    _venv(home / "proj" / ".venv")
    _opt_out(home / "proj")
    return home / "proj", {}, None


def _case_opt_out_local_layer(root, home):
    _venv(home / "proj" / ".venv")
    _opt_out(home / "proj", "bootstrap.local.json")
    return home / "proj", {}, None


def _case_opt_out_outranks_virtual_env(root, home):
    _opt_out(home / "proj")
    sh._make_interp(root / "active")
    return home / "proj", {"VIRTUAL_ENV": str(root / "active")}, None


def _case_no_venv_fallback(root, home):
    (home / "empty").mkdir(parents=True)
    return home / "empty", {}, "standalone"


def _case_home_stop(root, home):
    _venv(root / ".venv")  # above HOME: never reached
    start = home / "deep" / "er"
    start.mkdir(parents=True)
    return start, {}, "standalone"


def _case_home_itself_is_checked(root, home):
    return home, {}, _venv(home / ".venv")


def _case_user_layer_opt_out_ignored_at_home(root, home):
    _opt_out(home)  # ~/.claude/bootstrap.json is the USER layer
    python = _venv(home / ".venv")
    return home, {}, python


def _case_user_layer_opt_out_ignored_below_home(root, home):
    _opt_out(home)
    (home / "proj").mkdir(parents=True)
    return home / "proj", {}, "standalone"


CASES = {
    name[len("_case_"):]: builder
    for name, builder in sorted(globals().items())
    if name.startswith("_case_")
}


def _norm(value):
    if value is None or value == "":
        return None
    return normalize_path(str(value))


def _engine(start, env, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    notes = []
    user, project = engine._interpreter_env_layers(str(start), None)
    opted_out = engine._project_python_opt_out(user, project, notes)
    value, _source = interpreter_env.default_project_python(
        str(start), opted_out=opted_out, record_dir=None, env=env, home=str(home))
    return value


def _cli(start, env, home, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return cli._resolve_project_python(str(start), env=env)[0]


def _bash(start, extra, home, sandbox):
    value, rc = sh._resolve(sandbox, sh._fwd(start), str(home), **extra)
    assert rc in ("rc=0", "rc=1"), rc
    if rc == "rc=1":
        assert value == "", "an opted-out tree prints an empty line"
        return None
    return value


def _prelude(scaffold, start, extra):
    s = dataclasses.replace(scaffold, project=start)
    s.seed_guard()
    result = prelude._run(s, env=prelude._env(s, **extra))
    assert result.returncode == 0, result.stderr
    assert s.exports("BOOTSTRAP_PYTHON"), "the prelude always writes the engine name"
    lines = s.exports("BOOTSTRAP_PROJECT_PYTHON")
    assert len(lines) <= 1, lines
    if not lines:
        return None
    assert lines[0].startswith("'") and lines[0].endswith("'"), lines[0]
    return lines[0][1:-1]


def _powershell(start, extra, home, sandbox):
    env = sh._env(sandbox, str(home), BOOTSTRAP_PP_NO_REGISTER="1", **extra)
    command = (
        f". '{sh.PS_TEMPLATE}'; "
        f"$r = Resolve-BootstrapProjectPython -Path '{start}'; "
        "'value=[' + $r + ']'"
    )
    args = [sh.POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive"]
    if WINDOWS:
        args += ["-ExecutionPolicy", "Bypass"]
    run = subprocess.run(
        args + ["-Command", command], capture_output=True, text=True, env=env,
        cwd=str(sandbox), timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    value = sh._values(run.stdout)["value"]
    assert value.startswith("[") and value.endswith("]"), value
    return value[1:-1] or None


@pytest.mark.parametrize("case", sorted(CASES))
def test_resolvers_agree(case, tmp_path, monkeypatch):
    scaffold = prelude._scaffold(tmp_path)
    home = scaffold.home
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "BOOTSTRAP_PYTHON",
                 "BOOTSTRAP_PROJECT_PYTHON", "CLAUDE_ENV_FILE"):
        monkeypatch.delenv(name, raising=False)

    start, extra, expected = CASES[case](tmp_path, home)
    if expected == "standalone":
        expected = standalone_python(str(home))
    want = _norm(expected)
    env = dict(extra)  # the Python resolvers read only these names

    results = {
        "engine": _norm(_engine(start, env, home, monkeypatch)),
        "cli": _norm(_cli(start, env, home, monkeypatch)),
        "bash": _norm(_bash(start, extra, home, sandbox)),
        "prelude": _norm(_prelude(scaffold, start, extra)),
    }
    if sh.POWERSHELL is not None:
        results["powershell"] = _norm(_powershell(start, extra, home, sandbox))

    assert results == {name: want for name in results}, (case, want, results)


def test_case_table_covers_every_source():
    """Guard against a vacuous table: the cases cover every source, and at
    least one expects each of a venv, the fallback, and an opt-out."""
    names = set(CASES)
    for required in ("venv", "nested_venv", "virtual_env", "uv_relative",
                     "uv_absolute", "opt_out", "no_venv_fallback", "home_stop",
                     "user_layer_opt_out_ignored_at_home"):
        assert required in names, required
