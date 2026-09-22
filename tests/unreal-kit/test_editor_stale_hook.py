"""Tests for hooks/pretooluse/detect-editor-stale.py.

U6: the hook must resolve the per-project config through the canonical
resolver in lib/ue_runner_config.py (current + legacy paths, walking up from
cwd) instead of re-implementing a one-key parser and its own legacy list.

U10: the dll-vs-Build.version mtime heuristic is only valid for source
builds; installed (Launcher/binary) engines ship Engine/Build/
InstalledBuild.txt and must never be flagged stale.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit"
_HOOK = _PLUGIN_DIR / "hooks" / "pretooluse" / "detect-editor-stale.py"
_LIB_DIR = _PLUGIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from ue_runner_config import PROJECT_CONFIG_NAME, _parse_yaml_simple


def _run_hook(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps({"cwd": str(cwd)}),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _make_engine(root: Path, stale: bool, installed: bool = False) -> Path:
    """Create a fake engine tree. stale=True -> dll older than Build.version."""
    engine = root / "Engine"
    dll = engine / "Binaries" / "Win64" / "UnrealEditor-BuildSettings.dll"
    version = engine / "Build" / "Build.version"
    dll.parent.mkdir(parents=True, exist_ok=True)
    version.parent.mkdir(parents=True, exist_ok=True)
    dll.write_text("dll")
    version.write_text("{}")
    now = 1_700_000_000
    if stale:
        os.utime(dll, (now - 1000, now - 1000))
        os.utime(version, (now, now))
    else:
        os.utime(dll, (now, now))
        os.utime(version, (now - 1000, now - 1000))
    if installed:
        (engine / "Build" / "InstalledBuild.txt").write_text("")
    return engine


def _write_config(path: Path, engine_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = str(engine_dir).replace("\\", "/")
    path.write_text(f'engine_dir: "{safe}"\n', encoding="utf-8")


def _marker(cwd: Path) -> Path:
    return cwd / ".local-data" / "plugins-kit" / "unreal-kit" / "editor-stale.flag"


def _sysmsg(cwd: Path) -> Path:
    return cwd / ".local-data" / "claude-ui-kit" / "systemmessage.unreal-kit.txt"


def _diagnostic(cwd: Path) -> Path:
    return cwd / ".local-data" / "plugins-kit" / "unreal-kit" / "editor-stale-detector.log"


def _seed_stale_assertion(cwd: Path) -> None:
    _marker(cwd).parent.mkdir(parents=True, exist_ok=True)
    _marker(cwd).write_text("prior", encoding="utf-8")
    _sysmsg(cwd).parent.mkdir(parents=True, exist_ok=True)
    _sysmsg(cwd).write_text("Editor needs rebuild", encoding="utf-8")


def _wait_for_text(path: Path, text: str, timeout: float = 2.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            content = path.read_text(encoding="utf-8")
            if text in content:
                return content
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for {text!r} in {path}")


class TestStaleDetection:
    def test_stale_source_build_writes_marker(self, tmp_path):
        engine = _make_engine(tmp_path, stale=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert _marker(proj).is_file()
        assert _sysmsg(proj).read_text(encoding="utf-8") == "Editor needs rebuild"

    def test_fresh_source_build_removes_marker(self, tmp_path):
        engine = _make_engine(tmp_path, stale=False)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)
        _marker(proj).parent.mkdir(parents=True, exist_ok=True)
        _marker(proj).write_text("")
        _sysmsg(proj).parent.mkdir(parents=True, exist_ok=True)
        _sysmsg(proj).write_text("Editor needs rebuild")

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert not _marker(proj).exists()
        assert not _sysmsg(proj).exists()

    def test_no_config_is_a_noop(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert not _marker(proj).exists()


class TestSharedConfigResolution:
    """U6 regressions: resolution must match ue_runner_config exactly."""

    def test_legacy_claude_yaml_is_honored(self, tmp_path):
        engine = _make_engine(tmp_path, stale=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / ".claude" / "unreal-kit.yaml", engine)

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert _marker(proj).is_file()

    def test_config_in_parent_directory_is_found(self, tmp_path):
        """The old hook only looked at the exact cwd; the canonical resolver
        walks up. A cwd nested under the project root must still resolve."""
        engine = _make_engine(tmp_path, stale=True)
        proj = tmp_path / "proj"
        sub = proj / "Content" / "Python"
        sub.mkdir(parents=True)
        _write_config(proj / PROJECT_CONFIG_NAME, engine)

        result = _run_hook(sub)
        assert result.returncode == 0, result.stderr
        # Marker lands under the hook's cwd (the payload cwd), as before.
        assert _marker(sub).is_file()

    def test_simple_parser_reads_quoted_engine_dir(self, tmp_path):
        """The hook runs under the deterministic standalone/BOOTSTRAP_PYTHON
        interpreter (no pyyaml), where ue_runner_config falls back to
        _parse_yaml_simple. That parser must handle the quoted flat keys
        write_project_config emits."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('engine_dir: "C:/UE5/Engine"\nuproject: "C:/P/G.uproject"\n', encoding="utf-8")
        data = _parse_yaml_simple(cfg)
        assert data["engine_dir"] == "C:/UE5/Engine"
        assert data["uproject"] == "C:/P/G.uproject"

    def test_malformed_config_reports_unknown_and_clears_marker(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        config = proj / PROJECT_CONFIG_NAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("remote_execution: [broken\n", encoding="utf-8")
        marker = _marker(proj)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("prior", encoding="utf-8")

        result = _run_hook(proj)
        assert result.returncode == 0
        assert "UNKNOWN" in result.stderr
        assert not marker.exists()
        assert not _sysmsg(proj).exists()
        assert "UNKNOWN" in _diagnostic(proj).read_text(encoding="utf-8")

    def test_invalid_engine_path_type_reports_unknown(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        config = proj / PROJECT_CONFIG_NAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("engine_dir: 42\n", encoding="utf-8")

        result = _run_hook(proj)
        assert result.returncode == 0
        assert "UNKNOWN" in result.stderr


class TestInstalledBuildGate:
    """U10 regressions: installed engines are never flagged stale."""

    def test_installed_build_never_flags_stale(self, tmp_path):
        engine = _make_engine(tmp_path, stale=True, installed=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert not _marker(proj).exists()

    def test_installed_build_clears_leftover_marker(self, tmp_path):
        engine = _make_engine(tmp_path, stale=True, installed=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)
        _seed_stale_assertion(proj)

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert not _marker(proj).exists()


@pytest.mark.parametrize("case", ["absent_config", "invalid_config", "absent_dll", "absent_build_version", "unsupported_layout"])
def test_unknown_detector_state_clears_stale_assertion_and_records_diagnostic(tmp_path, case):
    """Unknown inputs must not leave a stale warning that looks authoritative."""
    proj = tmp_path / "project with spaces"
    proj.mkdir()
    _seed_stale_assertion(proj)

    if case == "invalid_config":
        config = proj / PROJECT_CONFIG_NAME
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text("remote_execution: [broken\n", encoding="utf-8")
    elif case == "absent_dll":
        engine = _make_engine(tmp_path, stale=True)
        (engine / "Binaries" / "Win64" / "UnrealEditor-BuildSettings.dll").unlink()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)
    elif case == "absent_build_version":
        engine = _make_engine(tmp_path, stale=True)
        (engine / "Build" / "Build.version").unlink()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)
    elif case == "unsupported_layout":
        engine = tmp_path / "Engine"
        (engine / "Build").mkdir(parents=True)
        _write_config(proj / PROJECT_CONFIG_NAME, engine)

    result = _run_hook(proj)

    assert result.returncode == 0
    assert "UNKNOWN" in result.stderr
    assert not _marker(proj).exists()
    assert not _sysmsg(proj).exists()
    assert "UNKNOWN" in _diagnostic(proj).read_text(encoding="utf-8")


def test_direct_zsh_matching_input_handles_spaced_and_escaped_cwd(tmp_path):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh is not installed")
    proj = tmp_path / "project with spaces"
    proj.mkdir()
    payload = json.dumps({"tool_name": "mcp__unreal-engine__save", "cwd": str(proj)})
    result = subprocess.run(
        [zsh, str(_PLUGIN_DIR / "hooks/pretooluse/check-editor-build-fresh.sh")],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path / "home"), "BOOTSTRAP_PYTHON": str(tmp_path / "missing-python")},
        timeout=5,
    )
    assert result.returncode == 0, result.stderr


def test_matching_hook_extracts_escaped_spaced_cwd_for_detector(tmp_path):
    """A JSON escaped separator must still reach the detector at the CWD."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not installed")
    proj = tmp_path / "project \\with spaces"
    proj.mkdir()
    payload = json.dumps({"tool_name": "mcp__unreal-engine__save", "cwd": str(proj)})
    home = tmp_path / "home"
    standalone = home / ".local" / "share" / "python-standalone" / "python" / "bin" / "python3"
    standalone.parent.mkdir(parents=True)
    standalone.symlink_to(sys.executable)

    result = subprocess.run(
        [bash, str(_PLUGIN_DIR / "hooks/pretooluse/check-editor-build-fresh.sh")],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert "no project config was found" in _wait_for_text(
        _diagnostic(proj), "no project config was found"
    )


def test_detached_wrapper_retains_detector_stderr(tmp_path):
    home = tmp_path / "home"
    interpreter = home / ".local" / "share" / "python-standalone" / "python" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nprintf '%s\\n' sentinel-import-failure >&2\nexit 23\n", encoding="utf-8")
    interpreter.chmod(0o755)
    proj = tmp_path / "project with spaces"
    proj.mkdir()
    payload = json.dumps({"tool_name": "mcp__unreal-engine__save", "cwd": str(proj)})

    result = subprocess.run(
        ["bash", str(_PLUGIN_DIR / "hooks/pretooluse/check-editor-build-fresh.sh")],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home)},
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    log = _wait_for_text(_diagnostic(proj), "detector exited with status 23")
    assert "sentinel-import-failure" in log


def test_wrapper_logs_interpreter_resolution_failure(tmp_path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not installed")
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    for name in ("cat", "tr", "grep", "sed", "uname", "dirname", "pwd", "mkdir"):
        source = shutil.which(name)
        if source:
            (tool_bin / name).symlink_to(source)
    bare_python = tool_bin / "python3"
    bare_python.write_text(
        f"#!/bin/sh\nprintf '%s\\n' bare-fallback-used > {tmp_path / 'bare-fallback'}\nexit 23\n",
        encoding="utf-8",
    )
    bare_python.chmod(0o755)
    home = tmp_path / "home"
    proj = tmp_path / "project"
    proj.mkdir()
    payload = json.dumps({"tool_name": "mcp__unreal-engine__save", "cwd": str(proj)})

    result = subprocess.run(
        [bash, str(_PLUGIN_DIR / "hooks/pretooluse/check-editor-build-fresh.sh")],
        input=payload,
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(home), "PATH": str(tool_bin)},
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "bare-fallback").exists()
    assert "no approved detector interpreter was found" in _wait_for_text(
        _diagnostic(proj), "no approved detector interpreter"
    )


def test_wrapper_redirects_detached_subshell_streams_before_work():
    source = (_PLUGIN_DIR / "hooks/pretooluse/check-editor-build-fresh.sh").read_text(encoding="utf-8")
    assert 'exec </dev/null >>"$LOG" 2>&1' in source
