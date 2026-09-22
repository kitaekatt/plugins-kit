"""T15: bootstrap-owned consumers try the deterministic interpreter FIRST.

The forced form (plan v3 section 4A-B; python-interpreter.md "Default vs.
forced"): a launcher or hook bootstrap owns runs its deterministic interpreter
(the standalone install, or the plugin venv for a venv-first shim) whenever
that file exists; ``$BOOTSTRAP_PYTHON`` is only the next fallback, and only
when it resolves inside ``~/.local/share/python-standalone``; then the PATH
chain. test_shell_interpreter_fallback.py pins the fallback tier (variable
used when the deterministic file is absent, ignored when it points outside).
This file pins the ORDER: with the deterministic file present, a perfectly
valid ``$BOOTSTRAP_PYTHON`` is still not used.

It also pins the ``.cmd`` twins' prefix test against the value bootstrap
actually persists (forward slashes, ``C:/...``): accepted inside the
standalone directory in any case or slash direction, refused outside it or
with a ``..`` segment. Those run under cmd.exe on Windows only, with ``.cmd``
fakes, since a fake ``python.exe`` cannot be executed by cmd.

check-editor-build-fresh.sh resolves its interpreter inside a detached
subshell that subprocess cannot observe on Windows (see
test_shell_interpreter_fallback.py), so its order is checked statically, as
is every ``.cmd`` twin's guard.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from bootstrap import test_shell_hook as sh

REPO_ROOT = Path(__file__).resolve().parents[2]
WINDOWS = os.name == "nt"
needs_bash = pytest.mark.skipif(sh.BASH is None, reason="bash not available")
windows_only = pytest.mark.skipif(not WINDOWS, reason="cmd.exe is Windows-only")

STANDALONE_REL = ".local/share/python-standalone"


def _bash_is_windows() -> bool:
    if sh.BASH is None:
        return False
    out = subprocess.run([sh.BASH, "-c", "uname -s"], capture_output=True,
                         text=True, timeout=60).stdout
    return out.upper().startswith(("MINGW", "MSYS", "CYGWIN"))


BASH_WINDOWS = _bash_is_windows()


def _stub(path: Path, marker_file: Path, label: str) -> Path:
    """An executable that appends ``label`` to ``marker_file`` and exits 0.

    A shebang script: Git Bash executes it even when it is named python.exe.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "{label}" >> "{marker_file.as_posix()}"\nexit 0\n',
        encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _standalone(home: Path) -> Path:
    rel = "python/python.exe" if BASH_WINDOWS else "python/bin/python3"
    return home / STANDALONE_REL / rel


def _display_standalone(home: Path) -> Path:
    # bootstrap-display.sh mirrors session-bootstrap.sh's WANT_PYTHON.
    return (home / STANDALONE_REL / "python" / "python.exe" if BASH_WINDOWS
            else home / ".local" / "bin" / "python3")


def _plugin_venv(home: Path, plugin: str) -> Path:
    rel = "Scripts/python.exe" if BASH_WINDOWS else "bin/python"
    return home / ".claude" / "plugins" / "data" / "plugins-kit" / plugin / ".venv" / rel


def _run(script: Path, home: Path, env_extra: dict, *, cwd: Path) -> subprocess.CompletedProcess:
    env = sh._env(cwd, str(home), **env_extra)
    return subprocess.run([sh.BASH, str(script)], input="", capture_output=True,
                          text=True, env=env, cwd=str(cwd), timeout=120)


# (consumer script, deterministic-path factory, extra env factory)
CONSUMERS = {
    "hue-kit": ("plugins/hue-kit/bin/hue-kit", _standalone, None),
    "secrets-kit": ("plugins/secrets-kit/bin/secrets-kit", _standalone, None),
    "job-kit": ("plugins/job-kit/bin/job-kit",
                lambda h: _plugin_venv(h, "job-kit"), None),
    "llm-scripting-kit": ("plugins/llm-scripting-kit/bin/llm-scripting-kit",
                          lambda h: _plugin_venv(h, "llm-scripting-kit"), None),
    "llm-scripting-kit-standalone": ("plugins/llm-scripting-kit/bin/llm-scripting-kit",
                                     _standalone, None),
    "bootstrap-display": ("plugins/bootstrap/hooks/userpromptsubmit/bootstrap-display.sh",
                          _display_standalone, "display"),
    "diagnose-python-venv": ("plugins/bootstrap/scripts/diagnose-python-venv.sh",
                             _standalone, "diagnose"),
    "repair-registry": ("plugins/bootstrap-stuck-fix/hooks/sessionstart/repair-registry.sh",
                        _standalone, "repair"),
}


def _prepare(kind, home: Path, tmp_path: Path) -> dict:
    if kind == "display":
        return {"CLAUDE_BOOTSTRAP_DATA_ROOT": str(tmp_path / "data")}
    if kind == "diagnose":
        registry = home / ".claude" / "plugins"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / "installed_plugins.json").write_text('{"plugins": {}}\n')
        return {}
    if kind == "repair":
        scripts = tmp_path / "stuckfix" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "repair_registry.py").write_text("# stand-in\n")
        return {"CLAUDE_PLUGIN_ROOT": str(tmp_path / "stuckfix")}
    return {}


@needs_bash
@pytest.mark.parametrize("name", sorted(CONSUMERS))
def test_deterministic_interpreter_beats_a_valid_variable(name, tmp_path):
    """(i) deterministic present + valid BOOTSTRAP_PYTHON -> deterministic.

    Revert that turns this RED: drop the `[ ! -x "$PY" ]` (or equivalent)
    guard in front of the consumer's BOOTSTRAP_PYTHON block, so a valid
    variable overrides the deterministic file.
    """
    rel, deterministic, kind = CONSUMERS[name]
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "invoked.txt"
    _stub(deterministic(home), marker, "DETERMINISTIC")
    variable = _stub(home / STANDALONE_REL / "alt" / "python3", marker, "VARIABLE")
    extra = _prepare(kind, home, tmp_path)
    run = _run(REPO_ROOT / rel, home, {"BOOTSTRAP_PYTHON": str(variable), **extra},
               cwd=tmp_path)
    assert marker.is_file(), (run.returncode, run.stdout, run.stderr)
    seen = set(marker.read_text(encoding="utf-8").split())
    assert seen == {"DETERMINISTIC"}, (seen, run.stderr)


@needs_bash
def test_variable_tier_still_fires_without_the_deterministic_file(tmp_path):
    """(ii) The same harness sees the variable when the file is absent, so
    the test above is not green merely because the variable never works."""
    home = tmp_path / "home"
    home.mkdir()
    marker = tmp_path / "invoked.txt"
    variable = _stub(home / STANDALONE_REL / "alt" / "python3", marker, "VARIABLE")
    run = _run(REPO_ROOT / "plugins/hue-kit/bin/hue-kit", home,
               {"BOOTSTRAP_PYTHON": str(variable)}, cwd=tmp_path)
    assert marker.read_text(encoding="utf-8").split() == ["VARIABLE"], run.stderr


# ---------------------------------------------------------------- static order


def _index(text: str, needle: str) -> int:
    idx = text.find(needle)
    assert idx >= 0, needle
    return idx


def test_check_editor_build_fresh_order_is_deterministic_first():
    """Static (the detached subshell is not observable): deterministic path,
    validated variable tier, then the explicit unknown outcome."""
    text = (REPO_ROOT / "plugins/unreal-kit/hooks/pretooluse/"
            "check-editor-build-fresh.sh").read_text(encoding="utf-8")
    det = _index(text, '_STANDALONE_PY="${HOME:-}/.local/share/python-standalone/')
    var = _index(text, 'elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]')
    unknown = _index(text, "no approved detector interpreter was found")
    assert det < var < unknown
    assert "command -v python3" not in text


CMD_TWINS = {
    "plugins/hue-kit/bin/hue-kit.cmd": 'if not exist "%PY%" if defined BOOTSTRAP_PYTHON',
    "plugins/secrets-kit/bin/secrets-kit.cmd": 'if not exist "%PY%" if defined BOOTSTRAP_PYTHON',
    "plugins/job-kit/bin/job-kit.cmd": "if not defined PY if defined BOOTSTRAP_PYTHON",
    "plugins/llm-scripting-kit/bin/llm-scripting-kit.cmd": "if not defined PY if defined BOOTSTRAP_PYTHON",
    "plugins/unreal-kit/skills/ue-python-api/scripts/ue-runner.cmd":
        'if not defined PY if defined BOOTSTRAP_PYTHON',
}


@pytest.mark.parametrize("rel", sorted(CMD_TWINS))
def test_cmd_twin_guards_the_variable_tier(rel):
    """Static: every .cmd twin enters its BOOTSTRAP_PYTHON block only when
    the deterministic interpreter is missing, and compares a slash-normalized,
    case-insensitive prefix that refuses "..".

    Revert that turns this RED: restore the pre-U7 comparison
    `if "!BOOTSTRAP_PYTHON!"=="!_STANDALONE_DIR!!_TRIMMED!"`.
    """
    text = (REPO_ROOT / rel).read_text(encoding="ascii")
    guard = _index(text, CMD_TWINS[rel])
    assert text.count("BOOTSTRAP_PYTHON") and guard < text.index('set "_BP=!BOOTSTRAP_PYTHON:/=\\!"')
    assert 'set "_TRIMMED=!_BP:*%_STANDALONE_DIR%=!"' in text
    assert re.search(r'if /I "!_BP!"=="!_STANDALONE_DIR!!_TRIMMED!" '
                     r'if "!_TRIMMED:\.\.=!"=="!_TRIMMED!" set ', text)
    assert '"!BOOTSTRAP_PYTHON!"==' not in text


# ------------------------------------------------------------- cmd.exe runs


def _cmd_fake(path: Path, label: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"@echo {label} %*\r\n".encode("ascii"))
    return path


def _run_cmd(shim: Path, profile: Path, value: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ("BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON")}
    env["USERPROFILE"] = str(profile)
    env["BOOTSTRAP_PYTHON"] = value
    # No python.exe on PATH: a refused value must end in "no interpreter".
    env["PATH"] = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")
    return subprocess.run(["cmd.exe", "/d", "/c", str(shim), "probe-arg"],
                          capture_output=True, text=True, env=env, timeout=60)


@windows_only
@pytest.mark.parametrize("variant", ["forward", "backslash", "lower-drive"])
def test_cmd_twin_accepts_the_persisted_value_form(variant, tmp_path):
    """Deterministic absent + a value inside the standalone directory, in
    the forward-slash form bootstrap persists -> used.

    Revert that turns this RED (forward, lower-drive): restore the pre-U7
    comparison, which never matched a forward-slash value.
    """
    profile = tmp_path / "profile"
    fake = _cmd_fake(profile / ".local" / "share" / "python-standalone" / "alt" / "python.cmd",
                     "MARKER_VARIABLE")
    value = {
        "forward": fake.as_posix(),
        "backslash": str(fake),
        "lower-drive": fake.as_posix()[0].lower() + fake.as_posix()[1:],
    }[variant]
    run = _run_cmd(REPO_ROOT / "plugins/hue-kit/bin/hue-kit.cmd", profile, value)
    assert "MARKER_VARIABLE" in run.stdout, (run.stdout, run.stderr)


@windows_only
@pytest.mark.parametrize("where", ["outside", "dotdot"])
def test_cmd_twin_refuses_values_outside_the_directory(where, tmp_path):
    profile = tmp_path / "profile"
    standalone = profile / ".local" / "share" / "python-standalone"
    (standalone / "x").mkdir(parents=True)
    outside = _cmd_fake(tmp_path / "elsewhere" / "python.cmd", "MARKER_OUTSIDE")
    escaped = _cmd_fake(profile / ".local" / "share" / "evil" / "python.cmd", "MARKER_ESCAPED")
    value = {
        "outside": outside.as_posix(),
        "dotdot": str(standalone / "x") + r"\..\..\evil\python.cmd",
    }[where]
    assert os.path.isfile(value)
    run = _run_cmd(REPO_ROOT / "plugins/hue-kit/bin/hue-kit.cmd", profile, value)
    assert "MARKER" not in run.stdout, (run.stdout, run.stderr)
    assert "no Python interpreter found" in run.stderr
    assert escaped.is_file()
