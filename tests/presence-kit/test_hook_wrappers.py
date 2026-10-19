"""presence-kit Stop wiring: hooks/hooks.json and hooks/stop/presence-stop.sh.

Contract: dev/tasks/presence-kit/contracts.md sections 1 and 2; design.md "Hooks".
The wrapper is portable sh: it resolves the interpreter like unreal-kit's
check-editor-build-fresh.sh (the deterministic standalone install first, then
BOOTSTRAP_PYTHON only when it resolves inside the standalone directory, no bare
interpreter name), prints a systemMessage and exits 0 when none is found, and
otherwise execs `presence.py stop` with stdin and stdout passed through.

The behavioral tests run the real wrapper under every available POSIX shell with
a throwaway HOME whose "interpreters" are shell-script stubs (a shebang script
runs even when named python.exe under Git Bash), so a revert of the real script
shows up as the wrong marker. One end-to-end test runs the real interpreter
through the wrapper when the machine has one; it uses a temp data root and a
temp config dir only.
"""
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "presence-kit"
WRAPPER = PLUGIN / "hooks" / "stop" / "presence-stop.sh"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SHELLS = [name for name in ("sh", "bash", "dash", "zsh") if shutil.which(name)]
STANDALONE_REL = Path(".local") / "share" / "python-standalone"


def _uname(shell):
    return subprocess.run([shell, "-c", "uname -s"], capture_output=True, text=True, timeout=60).stdout.strip()


WINDOWS_SHELL = _uname(SHELLS[0]).upper().startswith(("MINGW", "MSYS", "CYGWIN")) if SHELLS else False
STANDALONE_EXE = Path("python") / ("python.exe" if WINDOWS_SHELL else Path("bin") / "python3")

needs_shell = pytest.mark.skipif(not SHELLS, reason="no POSIX shell available")


# ------------------------------------------------------------------ hooks.json

def test_hooks_json_wires_only_the_stop_hook():
    """UserPromptSubmit is unit K3: no entry, no stub."""
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    assert list(hooks) == ["hooks"]
    assert list(hooks["hooks"]) == ["Stop"]
    (group,) = hooks["hooks"]["Stop"]
    assert list(group) == ["hooks"]
    (entry,) = group["hooks"]
    assert entry == {
        "type": "command",
        "command": 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/stop/presence-stop.sh"',
    }


def test_hooks_json_command_resolves_to_the_wrapper():
    command = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]["Stop"][0]["hooks"][0]["command"]
    rel = command.split("${CLAUDE_PLUGIN_ROOT}/", 1)[1].rstrip('"')
    assert (PLUGIN / rel) == WRAPPER and WRAPPER.is_file()


# ------------------------------------------------------------- static wrapper

def _text():
    return WRAPPER.read_text(encoding="ascii")


def test_wrapper_is_ascii_portable_sh():
    text = _text()
    assert text.startswith("#!/bin/sh\n")
    assert "\r" not in text
    for forbidden in ("BASH_SOURCE", "BASH_REMATCH", "[[", "declare ", "local -", "read -p", "${BASH"):
        assert forbidden not in text, forbidden
    assert "set -u" in text
    assert "exec " in text and " stop" in text


def test_wrapper_names_no_absolute_path():
    for line in _text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert "C:" not in line and "/Users/" not in line and "/home/" not in line, line


def test_wrapper_passes_the_repo_python_invocation_guard():
    """Run the repo guard's own scanner over the wrapper (the guard reads tracked files only)."""
    path = REPO / "tests" / "repo-scripts" / "test_python_invocation_standard.py"
    spec = importlib.util.spec_from_file_location("python_invocation_standard_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.scan_shell_text("plugins/presence-kit/hooks/stop/presence-stop.sh", _text()) == []


def test_wrapper_does_not_use_the_guarded_launch_form():
    """The guarded `:?` form would make presence-kit an interpreter-contract caller of test_interpreter_floor."""
    assert "BOOTSTRAP_PYTHON:?" not in _text()
    assert "BOOTSTRAP_PROJECT_PYTHON" not in _text()
    assert "command -v " not in _text()


def test_wrapper_interpreter_order_is_deterministic_first():
    """Static order, as test_forced_interpreter_order pins it for the unreal-kit exemplar."""
    text = _text()

    def index(needle):
        assert needle in text, needle
        return text.index(needle)

    det = index('_STANDALONE_PY="${HOME:-}/.local/share/python-standalone/')
    var = index('elif [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]')
    unknown = index("no approved interpreter was found")
    run = index('exec "$_PRESENCE_PY"')
    assert det < var < unknown < run


# ------------------------------------------------------------ behavior (stubs)

def _stub(path: Path, label: str) -> Path:
    """An executable that records its argv and stdin to $STUB_OUT, prints a marker, exits $STUB_EXIT."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/sh\n"
        '{ printf "LABEL=%s\\n" "' + label + '"; for a in "$@"; do printf "ARG=%s\\n" "$a"; done; '
        'printf "STDIN="; cat; } > "$STUB_OUT"\n'
        'printf \'{"stub": "' + label + '"}\\n\'\n'
        'exit "${STUB_EXIT:-0}"\n',
        encoding="ascii", newline="\n",
    )
    path.chmod(0o755)
    return path


def _env(home: Path, out: Path, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON", "CLAUDE_PLUGIN_ROOT")}
    env.update(HOME=str(home), USERPROFILE=str(home), STUB_OUT=str(out))
    env.update(extra)
    return env


def _run(shell, home, out, stdin="", *, cwd=None, script=WRAPPER, **extra):
    return subprocess.run(
        [shell, str(script)], input=stdin, capture_output=True, text=True,
        env=_env(home, out, **extra), cwd=str(cwd or home), timeout=120,
    )


def _marker(out: Path):
    lines = out.read_text(encoding="utf-8").split("\n")
    labels = [x[len("LABEL="):] for x in lines if x.startswith("LABEL=")]
    args = [x[len("ARG="):] for x in lines if x.startswith("ARG=")]
    stdin = out.read_text(encoding="utf-8").split("STDIN=", 1)[1]
    return labels, args, stdin


def _is_presence_py(arg: str) -> bool:
    """The script argument names this plugin's scripts/presence.py, with no `..` segment
    (Git Bash may spell the drive `/d/...`, so compare the tail)."""
    posix = arg.replace("\\", "/")
    return posix.endswith("/plugins/presence-kit/scripts/presence.py") and "/../" not in posix


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "home"
    path.mkdir()
    return path


@needs_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_deterministic_interpreter_runs_presence_stop_with_stdin_and_stdout_passed_through(shell, home, tmp_path):
    out = tmp_path / "out.txt"
    _stub(home / STANDALONE_REL / STANDALONE_EXE, "DETERMINISTIC")
    payload = '{"session_id": "s", "prompt_id": "p"}\n'
    run = _run(shell, home, out, payload)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {"stub": "DETERMINISTIC"}
    labels, args, stdin = _marker(out)
    assert labels == ["DETERMINISTIC"]
    assert len(args) == 2 and args[1] == "stop"
    assert _is_presence_py(args[0])
    assert stdin == payload


@needs_shell
@pytest.mark.parametrize("code", [1, 3])
def test_exit_status_passes_through(code, home, tmp_path):
    out = tmp_path / "out.txt"
    _stub(home / STANDALONE_REL / STANDALONE_EXE, "DETERMINISTIC")
    run = _run(SHELLS[0], home, out, "{}", STUB_EXIT=str(code))
    assert run.returncode == code


@needs_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_deterministic_interpreter_beats_a_valid_variable(shell, home, tmp_path):
    out = tmp_path / "out.txt"
    _stub(home / STANDALONE_REL / STANDALONE_EXE, "DETERMINISTIC")
    variable = _stub(home / STANDALONE_REL / "alt" / "python3", "VARIABLE")
    run = _run(shell, home, out, "{}", BOOTSTRAP_PYTHON=variable.as_posix())
    assert run.returncode == 0, run.stderr
    assert _marker(out)[0] == ["DETERMINISTIC"]


@needs_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_variable_inside_the_standalone_directory_is_the_fallback(shell, home, tmp_path):
    out = tmp_path / "out.txt"
    variable = _stub(home / STANDALONE_REL / "alt" / "python3", "VARIABLE")
    run = _run(shell, home, out, "{}", BOOTSTRAP_PYTHON=variable.as_posix())
    assert run.returncode == 0, run.stderr
    assert _marker(out)[0] == ["VARIABLE"]


def _assert_no_interpreter_message(run, out):
    assert run.returncode == 0, run.stderr
    assert not out.exists()
    assert run.stdout.isascii() and run.stdout.endswith("\n") and run.stdout.count("\n") == 1
    obj = json.loads(run.stdout)
    assert list(obj) == ["systemMessage"]
    assert "no approved interpreter was found" in obj["systemMessage"]


@needs_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_missing_interpreter_prints_a_system_message_and_exits_0(shell, home, tmp_path):
    out = tmp_path / "out.txt"
    run = _run(shell, home, out, "{}")
    _assert_no_interpreter_message(run, out)


@needs_shell
@pytest.mark.parametrize("where", ["outside", "dotdot", "standalone-dir-absent"])
def test_variable_outside_the_standalone_directory_is_refused(where, home, tmp_path):
    out = tmp_path / "out.txt"
    outside = _stub(home / "elsewhere" / "python3", "OUTSIDE")
    if where == "outside":
        (home / STANDALONE_REL / "alt").mkdir(parents=True)
        value = outside
    elif where == "dotdot":
        (home / STANDALONE_REL / "alt").mkdir(parents=True)
        value = home / STANDALONE_REL / "alt" / ".." / ".." / ".." / ".." / "elsewhere" / "python3"
    else:
        value = outside      # no standalone directory at all: an empty prefix must not match everything
    run = _run(SHELLS[0], home, out, "{}", BOOTSTRAP_PYTHON=value.as_posix())
    _assert_no_interpreter_message(run, out)


@needs_shell
@pytest.mark.parametrize("shell", SHELLS)
def test_wrapper_finds_presence_py_from_any_cwd_and_by_relative_path(shell, home, tmp_path):
    out = tmp_path / "out.txt"
    _stub(home / STANDALONE_REL / STANDALONE_EXE, "DETERMINISTIC")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    absolute = _run(shell, home, out, "{}", cwd=elsewhere)
    assert absolute.returncode == 0, absolute.stderr
    rel = os.path.relpath(WRAPPER, PLUGIN)
    relative = subprocess.run(
        [shell, rel], input="{}", capture_output=True, text=True,
        env=_env(home, out), cwd=str(PLUGIN), timeout=120,
    )
    assert relative.returncode == 0, relative.stderr
    labels, args, _stdin = _marker(out)
    assert labels == ["DETERMINISTIC"] and _is_presence_py(args[0])


# --------------------------------------------------------- real interpreter

def _real_interpreter():
    path = Path.home() / STANDALONE_REL / STANDALONE_EXE
    return path if path.is_file() else None


@needs_shell
@pytest.mark.skipif(_real_interpreter() is None, reason="the bootstrap standalone interpreter is not installed")
@pytest.mark.parametrize("attended", ["1", "0", None])
def test_wrapper_runs_the_real_stop_hook_end_to_end(attended, tmp_path):
    """Real interpreter, real modules, fixture payload; temp data root and config dir only."""
    root = tmp_path
    (root / "proj").mkdir()
    payload = json.loads((FIXTURES / "a1-03-Stop.json").read_text(encoding="ascii"))
    transcript = root / "t.jsonl"
    transcript.write_bytes((FIXTURES / "a1-03-Stop.transcript.jsonl").read_bytes())
    payload.update(transcript_path=str(transcript), cwd=str(root / "proj"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "BOOTSTRAP_"))}
    env.update(
        CLAUDE_BOOTSTRAP_DATA_ROOT=str(root / "data"),
        CLAUDE_CONFIG_DIR=str(root / "cfg"),
        CLAUDE_PROJECT_DIR=str(root / "proj"),
    )
    if attended is not None:
        env["CLAUDE_CODE_SESSION_ATTENDED"] = attended
    run = subprocess.run([SHELLS[0], str(WRAPPER)], input=json.dumps(payload), capture_output=True,
                         text=True, env=env, cwd=str(root), timeout=120)
    assert run.returncode == 0, run.stderr
    assert run.stderr == ""
    if attended == "1":
        output = json.loads(run.stdout)
        assert output["hookSpecificOutput"]["hookEventName"] == "Stop"
        assert "waiting-guard" in output["hookSpecificOutput"]["additionalContext"]
    else:
        assert run.stdout == ""
        assert not (root / "data").exists()
