"""Config failures must stop UE callers before discovery or spawning."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit"
for p in (_PLUGIN_DIR / "scripts", _PLUGIN_DIR / "lib", _PLUGIN_DIR / "skills" / "ue-python-api" / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import ue_env_cli
import ue_runner
from ue_runner_config import ConfigError, RunnerConfig


def test_runner_public_run_reports_config_error_without_transport(tmp_path, monkeypatch):
    script = tmp_path / "script.py"
    script.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(ue_runner, "load_config", lambda: (_ for _ in ()).throw(ConfigError("bad.yaml: malformed")))
    with patch.object(ue_runner, "_try_remote") as remote, patch.object(ue_runner, "_run_commandlet") as cmdlet:
        result = ue_runner.run_ue_script(str(script))
    assert not result.success
    assert result.mode == "none"
    assert "bad.yaml" in result.error
    remote.assert_not_called()
    cmdlet.assert_not_called()


def test_env_status_config_error_does_not_probe_or_spawn(capsys):
    args = type("Args", (), {"config": "bad.yaml", "host": "127.0.0.1", "port": 3000})()
    with patch.object(ue_env_cli, "load_config", side_effect=ConfigError("bad.yaml: invalid")), \
         patch.object(ue_env_cli, "find_editor_processes") as find, \
         patch.object(ue_env_cli, "is_mcp_ready") as ready:
        assert ue_env_cli.cmd_status(args) == 2
    assert "bad.yaml" in capsys.readouterr().err
    find.assert_not_called()
    ready.assert_not_called()


def test_env_launch_config_error_does_not_spawn(capsys):
    args = type("Args", (), {"config": "bad.yaml", "host": "127.0.0.1", "port": 3000})()
    args.force = False
    args.map = None
    args.wait_for_mcp = False
    with patch.object(ue_env_cli, "load_config", side_effect=ConfigError("bad.yaml: invalid")), \
         patch.object(ue_env_cli, "launch_editor") as launch:
        assert ue_env_cli.cmd_launch_editor(args) == 2
    assert "bad.yaml" in capsys.readouterr().err
    launch.assert_not_called()
