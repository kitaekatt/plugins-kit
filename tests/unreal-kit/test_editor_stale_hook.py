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
import subprocess
import sys
from pathlib import Path

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
        """The hook runs under `uv run --no-project python` (no pyyaml), where
        ue_runner_config falls back to _parse_yaml_simple. That parser must
        handle the quoted flat keys write_project_config emits."""
        cfg = tmp_path / "config.yaml"
        cfg.write_text('engine_dir: "C:/UE5/Engine"\nuproject: "C:/P/G.uproject"\n', encoding="utf-8")
        data = _parse_yaml_simple(cfg)
        assert data["engine_dir"] == "C:/UE5/Engine"
        assert data["uproject"] == "C:/P/G.uproject"


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
        assert not _sysmsg(proj).exists()

    def test_installed_build_clears_leftover_marker(self, tmp_path):
        engine = _make_engine(tmp_path, stale=True, installed=True)
        proj = tmp_path / "proj"
        proj.mkdir()
        _write_config(proj / PROJECT_CONFIG_NAME, engine)
        _marker(proj).parent.mkdir(parents=True, exist_ok=True)
        _marker(proj).write_text("")

        result = _run_hook(proj)
        assert result.returncode == 0, result.stderr
        assert not _marker(proj).exists()
