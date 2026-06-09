"""Tests for remote-to-commandlet fallback on script errors."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts/ and lib/ to path
_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "skills" / "ue-python-api"
_PLUGIN_DIR = _SKILL_DIR.parent.parent
_SCRIPTS_DIR = _SKILL_DIR / "scripts"
_LIB_DIR = _PLUGIN_DIR / "lib"
for p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from ue_runner import run_ue_script, RunResult
from ue_runner_config import RunnerConfig


def _make_valid_config(tmp_path):
    """Create a RunnerConfig with real paths so validate() passes."""
    engine_dir = tmp_path / "Engine"
    exe = engine_dir / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.touch()

    uproject = tmp_path / "Project" / "Test.uproject"
    uproject.parent.mkdir(parents=True, exist_ok=True)
    uproject.write_text("{}")

    return RunnerConfig(
        engine_dir=str(engine_dir),
        uproject=str(uproject),
    )


class TestRemoteFallbackOnScriptError:
    """When remote connects but script errors, auto-mode should retry via commandlet."""

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_auto_mode_falls_back_on_script_error(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """Auto-detect mode: remote script error -> falls back to commandlet."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        # Remote connected but script errored
        mock_remote.return_value = RunResult(
            success=False, mode="remote", elapsed=5.4,
            error="Remote execution error: 'NoneType' object has no attribute 'get'",
        )
        # Commandlet succeeds
        mock_commandlet.return_value = RunResult(
            success=True, mode="commandlet", elapsed=45.0,
            output_file="/fake/output.yaml",
        )

        result = run_ue_script(str(script), force_mode=None, config=cfg)

        assert result.success is True
        assert result.mode == "commandlet"
        mock_commandlet.assert_called_once()

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_forced_remote_does_not_fall_back(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """--mode remote: script error is final, no commandlet fallback."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        mock_remote.return_value = RunResult(
            success=False, mode="remote", elapsed=5.4,
            error="Remote execution error: 'NoneType' object has no attribute 'get'",
        )

        result = run_ue_script(str(script), force_mode="remote", config=cfg)

        assert result.success is False
        assert result.mode == "remote"
        mock_commandlet.assert_not_called()

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_connection_failure_still_falls_back(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """Connection failure (None return) still falls back -- existing behavior."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        mock_remote.return_value = None  # editor not reachable
        mock_commandlet.return_value = RunResult(
            success=True, mode="commandlet", elapsed=30.0,
        )

        result = run_ue_script(str(script), force_mode=None, config=cfg)

        assert result.success is True
        assert result.mode == "commandlet"

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_remote_success_returns_immediately(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """Remote success: returns result, no commandlet attempt."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        mock_remote.return_value = RunResult(
            success=True, mode="remote", elapsed=1.2,
            output_file="/fake/output.yaml",
        )

        result = run_ue_script(str(script), force_mode=None, config=cfg)

        assert result.success is True
        assert result.mode == "remote"
        mock_commandlet.assert_not_called()

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_no_fallback_when_config_invalid(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """Script error + invalid config: return remote error (commandlet would also fail)."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        # Config with nonexistent paths — validate() returns errors
        cfg = RunnerConfig(engine_dir="/nonexistent", uproject="/nonexistent/T.uproject")
        mock_resolve.return_value = cfg

        mock_remote.return_value = RunResult(
            success=False, mode="remote", elapsed=5.4,
            error="Remote execution error: 'NoneType' object has no attribute 'get'",
        )

        result = run_ue_script(str(script), force_mode=None, config=cfg)

        # Should return the remote error, not attempt commandlet with bad config
        assert result.success is False
        assert result.mode == "remote"
        mock_commandlet.assert_not_called()
