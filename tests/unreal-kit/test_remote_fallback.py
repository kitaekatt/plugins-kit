"""Tests for remote-to-commandlet fallback on script errors."""

import sys
import types
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


class _FakeRemoteBoundary:
    """Small upyrc-shaped boundary that exercises the real _try_remote."""

    def __init__(self, *, execute_error=None, result=None, exit_error=None, pongs=None):
        self.execute_error = execute_error
        self.result = result
        self.exit_error = exit_error
        self.pongs = pongs
        self.events = []

    def module(self):
        boundary = self

        class RemoteExecutionConfig:
            def __init__(self, multicast_group, multicast_bind_address):
                self.multicast_group = multicast_group
                self.multicast_bind_address = multicast_bind_address

        class PingMessage:
            def __init__(self, config):
                self.config = config

            def send(self, sock):
                boundary.events.append("ping")

            def raw_receive(self, sock):
                boundary.events.append("pong")
                return boundary.pongs if boundary.pongs is not None else [
                    {"source": "node", "data": {"project_root": boundary.expected_root}}
                ]

        class OpenConnectionMessage:
            def __init__(self, node_id, config):
                self.node_id = node_id
                self.config = config

            def send(self, sock):
                boundary.events.append("open")

        class PythonRemoteCommandConnection:
            def __init__(self, node_id, config):
                self.node_id = node_id
                self.config = config

            def __init_subclass__(cls, **kwargs):
                return super().__init_subclass__(**kwargs)

        class PythonRemoteConnection:
            def __init__(self, config):
                self.config = config
                self.mcastsock = object()
                self.remote_command_connection = None

            def __enter__(self):
                self.open_connection()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                boundary.events.append("exit")
                if boundary.exit_error is not None:
                    raise boundary.exit_error
                return False

            def open_connection(self):
                ping = PingMessage(self.config)
                ping.send(self.mcastsock)
                pongs = list(ping.raw_receive(self.mcastsock))
                if not pongs:
                    raise ConnectionError("Connection failed.")
                match = None
                for pong in pongs:
                    if pong["data"].get("project_root") == boundary.expected_root:
                        match = pong
                        break
                if match is None:
                    raise ConnectionError("No matching project")
                self.unreal_node_id = match["source"]
                OpenConnectionMessage(self.unreal_node_id, self.config).send(self.mcastsock)
                self.connection_created = True

            def execute_python_command(self, script_path, exec_type, raise_exc):
                boundary.events.append("dispatch")
                if boundary.execute_error is not None:
                    raise boundary.execute_error
                return boundary.result

        return types.SimpleNamespace(
            RemoteExecutionConfig=RemoteExecutionConfig,
            PingMessage=PingMessage,
            OpenConnectionMessage=OpenConnectionMessage,
            PythonRemoteConnection=PythonRemoteConnection,
            PythonRemoteCommandConnection=PythonRemoteCommandConnection,
            ExecTypes=types.SimpleNamespace(EXECUTE_FILE="file"),
            ConnectionError=ConnectionError,
        )


def _install_fake_upyrc(monkeypatch, boundary, config):
    boundary.expected_root = str(Path(config.uproject).parent)
    upyre = boundary.module()
    monkeypatch.setitem(sys.modules, "upyrc", types.SimpleNamespace(upyre=upyre))
    monkeypatch.setitem(sys.modules, "upyrc.upyre", upyre)


def _remote_result(*, success=True):
    return types.SimpleNamespace(success=success, result="remote result")


class TestRemoteFallbackOnScriptError:
    """Remote SCRIPT errors must NOT auto-retry via commandlet (U2): the script
    already ran remotely, so a fallback re-executes its side effects. Fallback
    on script error is opt-in via fallback_on_error; connection failures
    (_try_remote -> None) always fall back."""

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_auto_mode_does_not_fall_back_on_script_error(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """Auto-detect mode: remote script error -> NO commandlet retry (U2).

        The remote run already executed the script's side effects; a silent
        commandlet retry would re-execute them.
        """
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        # Remote connected but script errored
        mock_remote.return_value = RunResult(
            success=False, mode="remote", elapsed=5.4,
            error="Remote execution error: 'NoneType' object has no attribute 'get'",
        )

        result = run_ue_script(str(script), force_mode=None, config=cfg)

        assert result.success is False
        assert result.mode == "remote"
        # The error should tell the user how to opt into a retry
        assert "--fallback-on-error" in result.error
        mock_commandlet.assert_not_called()

    @patch("ue_runner._run_commandlet")
    @patch("ue_runner._try_remote")
    @patch("ue_runner._resolve_project")
    def test_fallback_on_error_opts_into_commandlet_retry(
        self, mock_resolve, mock_remote, mock_commandlet, tmp_path
    ):
        """fallback_on_error=True: remote script error -> retries via commandlet."""
        script = tmp_path / "test.py"
        script.write_text("import unreal")
        cfg = _make_valid_config(tmp_path)
        mock_resolve.return_value = cfg

        mock_remote.return_value = RunResult(
            success=False, mode="remote", elapsed=5.4,
            error="Remote execution error: 'NoneType' object has no attribute 'get'",
        )
        mock_commandlet.return_value = RunResult(
            success=True, mode="commandlet", elapsed=45.0,
            output_file="/fake/output.yaml",
        )

        result = run_ue_script(
            str(script), force_mode=None, config=cfg, fallback_on_error=True
        )

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


class TestRemoteDispatchAmbiguity:
    """Once dispatch starts, transport loss is an unknown completion."""

    @pytest.mark.parametrize(
        "execute_error",
        [TimeoutError("timed out"), RuntimeError("connection failed")],
    )
    def test_post_dispatch_exception_is_not_replayed(
        self, execute_error, tmp_path, monkeypatch
    ):
        cfg = _make_valid_config(tmp_path)
        script = Path(cfg.uproject).parent / "test.py"
        script.write_text("pass")
        monkeypatch.chdir(script.parent)
        boundary = _FakeRemoteBoundary(execute_error=execute_error)
        _install_fake_upyrc(monkeypatch, boundary, cfg)

        with patch("ue_runner._run_commandlet") as commandlet:
            result = run_ue_script(
                str(script), config=cfg, project=cfg.uproject,
                fallback_on_error=True
            )

        assert result.success is False
        assert result.mode == "remote"
        assert result.completion_unknown is True
        assert "unknown" in result.error.lower()
        assert boundary.events.count("dispatch") == 1
        commandlet.assert_not_called()

    def test_context_exit_timeout_is_not_replayed(self, tmp_path, monkeypatch):
        cfg = _make_valid_config(tmp_path)
        script = Path(cfg.uproject).parent / "test.py"
        script.write_text("pass")
        monkeypatch.chdir(script.parent)
        boundary = _FakeRemoteBoundary(
            result=_remote_result(), exit_error=TimeoutError("context timeout")
        )
        _install_fake_upyrc(monkeypatch, boundary, cfg)

        with patch("ue_runner._run_commandlet") as commandlet:
            result = run_ue_script(
                str(script), config=cfg, project=cfg.uproject,
                fallback_on_error=True
            )

        assert result.success is False
        assert result.completion_unknown is True
        assert boundary.events.count("dispatch") == 1
        commandlet.assert_not_called()

    def test_connection_failure_before_dispatch_falls_back(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_valid_config(tmp_path)
        script = Path(cfg.uproject).parent / "test.py"
        script.write_text("pass")
        monkeypatch.chdir(script.parent)
        boundary = _FakeRemoteBoundary(pongs=[])
        _install_fake_upyrc(monkeypatch, boundary, cfg)

        with patch("ue_runner._run_commandlet") as commandlet:
            commandlet.return_value = RunResult(success=True, mode="commandlet")
            result = run_ue_script(str(script), config=cfg, project=cfg.uproject)

        assert result.success is True
        assert result.mode == "commandlet"
        assert "dispatch" not in boundary.events
        commandlet.assert_called_once()

    def test_mismatched_project_pong_falls_back_without_dispatch(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_valid_config(tmp_path)
        script = Path(cfg.uproject).parent / "test.py"
        script.write_text("pass")
        monkeypatch.chdir(script.parent)
        boundary = _FakeRemoteBoundary(
            pongs=[{"source": "other", "data": {"project_root": "/other/project"}}]
        )
        _install_fake_upyrc(monkeypatch, boundary, cfg)

        with patch("ue_runner._run_commandlet") as commandlet:
            commandlet.return_value = RunResult(success=True, mode="commandlet")
            result = run_ue_script(str(script), config=cfg, project=cfg.uproject)

        assert result.success is True
        assert "dispatch" not in boundary.events
        commandlet.assert_called_once()

    def test_returned_script_failure_can_use_documented_opt_in_retry(
        self, tmp_path, monkeypatch
    ):
        cfg = _make_valid_config(tmp_path)
        script = Path(cfg.uproject).parent / "test.py"
        script.write_text("pass")
        monkeypatch.chdir(script.parent)
        boundary = _FakeRemoteBoundary(result=_remote_result(success=False))
        _install_fake_upyrc(monkeypatch, boundary, cfg)

        with patch("ue_runner._run_commandlet") as commandlet:
            commandlet.return_value = RunResult(success=True, mode="commandlet")
            result = run_ue_script(
                str(script), config=cfg, project=cfg.uproject,
                fallback_on_error=True
            )

        assert result.success is True
        assert result.mode == "commandlet"
        assert boundary.events.count("dispatch") == 1
        commandlet.assert_called_once()

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
