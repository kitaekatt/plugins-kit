"""The task-kit SessionStart hook that asks Databench to ensure its service."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "task-kit"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
SCRIPT = PLUGIN / "hooks" / "sessionstart" / "databench-ensure.sh"
LOG_RELATIVE = Path("plugins-kit") / "task-kit" / "sessions" / "session-start-hook.log"
SOURCES = ("startup", "resume", "clear")
posix_only = pytest.mark.skipif(os.name == "nt", reason="the hook is a POSIX shell script")


def hook_input(source: str) -> str:
    return json.dumps({"hook_event_name": "SessionStart", "source": source})


def make_tools(
    path: Path,
    *,
    marker: Path | None = None,
    status: int = 0,
    stderr_line: str | None = None,
) -> Path:
    path.mkdir()
    for name in ("cat", "date", "mkdir", "sed", "tr"):
        resolved = shutil.which(name)
        assert resolved is not None, f"required test utility is absent: {name}"
        (path / name).symlink_to(resolved)
    if marker is not None:
        databench = path / "databench"
        databench.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {shlex.quote(str(marker))}\n"
            + (f"printf '%s\\n' {shlex.quote(stderr_line)} >&2\n" if stderr_line is not None else "")
            + f"exit {status}\n",
            encoding="utf-8",
        )
        databench.chmod(databench.stat().st_mode | stat.S_IXUSR)
    return path


def run_hook(script: Path, source: str, *, path: Path, data_root: Path) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("sh")
    assert shell is not None, "sh is required for the hook test"
    env = dict(os.environ)
    env.update(PATH=str(path), CLAUDE_BOOTSTRAP_DATA_ROOT=str(data_root))
    return subprocess.run(
        [shell, str(script)],
        input=hook_input(source),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def log_lines(data_root: Path) -> list[str]:
    log = data_root / LOG_RELATIVE
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_hooks_json_runs_one_async_ensure_for_each_supported_source():
    document = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    session_start = document["hooks"]["SessionStart"]
    matchers = [group["matcher"] for group in session_start]
    assert sorted(matchers) == sorted(SOURCES)
    groups = {group["matcher"]: group["hooks"] for group in session_start}
    command = 'bash "${CLAUDE_PLUGIN_ROOT}/hooks/sessionstart/databench-ensure.sh"'
    for source in SOURCES:
        entries = [entry for entry in groups[source] if entry["command"] == command]
        (entry,) = entries
        assert entry == {"type": "command", "command": command, "async": True}


def test_script_is_ascii_portable_sh():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("<<<", "read -p", "local ", "function ", "declare ", "mapfile", "readarray", "${!", "=(",
                   "${BASH_", "source ", "$RANDOM", "&>"):
        assert banned not in code, f"bash-only construct {banned!r} in databench-ensure.sh"
    assert not re.search(r"(^|\s)\[\[\s", code, re.MULTILINE), "the [[ ]] test is bash-only"
    text.encode("ascii")


@pytest.mark.parametrize("shell_name", ("bash", "sh", "zsh"))
def test_script_parses_under_supported_shells(shell_name: str):
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    subprocess.run([shell, "-n", str(SCRIPT)], check=True)


@posix_only
@pytest.mark.parametrize("source", SOURCES)
def test_databench_ensure_is_called_once(source: str, tmp_path: Path):
    marker = tmp_path / "calls"
    tools = make_tools(tmp_path / "tools", marker=marker)
    done = run_hook(SCRIPT, source, path=tools, data_root=tmp_path / "data")
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""
    assert marker.read_text(encoding="utf-8").splitlines() == ["ensure"]
    assert log_lines(tmp_path / "data") == []


@posix_only
def test_absent_databench_exits_zero_and_logs_one_line(tmp_path: Path):
    tools = make_tools(tmp_path / "tools")
    done = run_hook(SCRIPT, "startup", path=tools, data_root=tmp_path / "data")
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""
    lines = log_lines(tmp_path / "data")
    assert len(lines) == 1
    assert lines[0].endswith(" databench-ensure.sh: databench is not on PATH")


@posix_only
def test_failing_databench_exits_zero_and_logs_one_line(tmp_path: Path):
    marker = tmp_path / "calls"
    tools = make_tools(tmp_path / "tools", marker=marker, status=7)
    done = run_hook(SCRIPT, "resume", path=tools, data_root=tmp_path / "data")
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""
    assert marker.read_text(encoding="utf-8").splitlines() == ["ensure"]
    lines = log_lines(tmp_path / "data")
    assert len(lines) == 1
    assert lines[0].endswith(" databench-ensure.sh: databench ensure exited 7")


@posix_only
def test_databench_stderr_is_appended_to_log(tmp_path: Path):
    marker = tmp_path / "calls"
    tools = make_tools(
        tmp_path / "tools",
        marker=marker,
        status=1,
        stderr_line="databench failed to ensure service",
    )
    done = run_hook(SCRIPT, "startup", path=tools, data_root=tmp_path / "data")
    assert done.returncode == 0
    assert done.stdout == "" and done.stderr == ""
    assert "databench failed to ensure service" in log_lines(tmp_path / "data")


@posix_only
@pytest.mark.parametrize("source", ("fork", "compact", ""))
def test_other_sources_do_nothing(source: str, tmp_path: Path):
    marker = tmp_path / "calls"
    tools = make_tools(tmp_path / "tools", marker=marker)
    done = run_hook(SCRIPT, source, path=tools, data_root=tmp_path / "data")
    assert done.returncode == 0
    assert not marker.exists()
    assert log_lines(tmp_path / "data") == []
