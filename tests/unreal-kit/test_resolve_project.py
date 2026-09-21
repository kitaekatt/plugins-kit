"""Tests for project resolution in ue_runner — the fix for multi-project environments."""

import json
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

from ue_runner import _resolve_project, main, run_ue_script
from ue_runner_config import RunnerConfig


def _make_uproject(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"Modules": [{"Name": "Test"}]}), encoding="utf-8")


def _make_engine(engine_dir: Path):
    exe = engine_dir / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("fake", encoding="utf-8")


class TestResolveProject:
    """_resolve_project should pick the right project based on context."""

    def test_explicit_project_overrides_everything(self, tmp_path):
        """--project flag takes priority over config and auto-detection."""
        # Config points to project A
        proj_a = tmp_path / "ProjectA" / "ProjectA.uproject"
        _make_uproject(proj_a)

        # --project points to project B
        proj_b = tmp_path / "ProjectB" / "ProjectB.uproject"
        _make_uproject(proj_b)
        _make_engine(tmp_path / "ProjectB" / "Engine")

        config = RunnerConfig(uproject=str(proj_a), engine_dir="")
        result = _resolve_project(config, str(tmp_path / "some_script.py"), explicit_project=str(proj_b))

        assert result.uproject == str(proj_b.resolve())

    def test_cwd_overrides_config(self, tmp_path, monkeypatch):
        """CWD-based discovery takes priority over config file values."""
        # Config points to a different project (the bug scenario)
        spirit = tmp_path / "othergame" / "OtherGame.uproject"
        _make_uproject(spirit)

        # CWD is in StackOBot
        stackobot_dir = tmp_path / "StackOBot"
        stackobot = stackobot_dir / "StackOBot.uproject"
        _make_uproject(stackobot)
        _make_engine(stackobot_dir / "Engine")
        monkeypatch.chdir(stackobot_dir)

        config = RunnerConfig(uproject=str(spirit), engine_dir="")
        script = str(stackobot_dir / "tmp" / "test_api.py")
        result = _resolve_project(config, script)

        assert "StackOBot" in result.uproject

    def test_script_path_discovery(self, tmp_path, monkeypatch):
        """When CWD has no project, fall back to script's location."""
        # Keep CWD beyond the resolver's six-parent search limit.  This also
        # prevents the test fixture's sibling project from being discovered
        # through a shared temporary-directory ancestor.
        unrelated = tmp_path.joinpath(*("empty" for _ in range(8)))
        unrelated.mkdir(parents=True)
        monkeypatch.chdir(unrelated)

        # Script lives inside a project
        proj = tmp_path / "MyGame"
        _make_uproject(proj / "MyGame.uproject")
        script = proj / "tmp" / "test.py"
        script.parent.mkdir(parents=True)
        script.write_text("pass")

        config = RunnerConfig(uproject="", engine_dir="")
        result = _resolve_project(config, str(script))

        assert "MyGame" in result.uproject

    def test_falls_back_to_config_when_no_discovery(self, tmp_path, monkeypatch):
        """If nothing is discoverable, keep config values."""
        # Deep path so walking up 6 levels stays inside tmp_path
        empty = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "empty"
        empty.mkdir(parents=True)
        monkeypatch.chdir(empty)

        config = RunnerConfig(
            uproject="/some/configured/path.uproject",
            engine_dir="/some/configured/engine",
        )
        # Script also in a deep isolated path so script-path walk can't escape
        script = str(empty / "detached_script.py")
        result = _resolve_project(config, script)

        assert result.uproject == "/some/configured/path.uproject"
        assert result.engine_dir == "/some/configured/engine"

    def test_engine_dir_re_resolved_on_discovery(self, tmp_path, monkeypatch):
        """When a project is discovered, engine_dir is re-resolved from it."""
        proj = tmp_path / "depot" / "MyGame"
        _make_uproject(proj / "MyGame.uproject")
        _make_engine(tmp_path / "depot" / "Engine")
        monkeypatch.chdir(proj)

        config = RunnerConfig(uproject="", engine_dir="/old/wrong/engine")
        result = _resolve_project(config, str(proj / "script.py"))

        assert "MyGame" in result.uproject
        assert result.engine_dir != "/old/wrong/engine"
        assert "Engine" in result.engine_dir

    @pytest.mark.parametrize(
        "explicit_project",
        ["nonexistent.uproject", "wrong-suffix.txt"],
    )
    def test_invalid_explicit_project_refuses_instead_of_falling_through(
        self, tmp_path, monkeypatch, explicit_project
    ):
        """An invalid --project target must not select another project."""
        proj = tmp_path / "MyGame"
        _make_uproject(proj / "MyGame.uproject")
        monkeypatch.chdir(proj)

        config = RunnerConfig(uproject="", engine_dir="")
        with pytest.raises(ValueError, match="--project path"):
            _resolve_project(
                config,
                str(proj / "script.py"),
                explicit_project=str(tmp_path / explicit_project),
            )

    def test_invalid_explicit_project_stops_before_either_transport(
        self, tmp_path, monkeypatch
    ):
        """Public execution refuses a bad explicit target before dispatch."""
        proj = tmp_path / "MyGame"
        _make_uproject(proj / "MyGame.uproject")
        monkeypatch.chdir(proj)
        script = proj / "script.py"
        script.write_text("pass")
        config = RunnerConfig(uproject="", engine_dir="")

        with patch("ue_runner._try_remote") as remote, patch(
            "ue_runner._run_commandlet"
        ) as commandlet:
            result = run_ue_script(
                str(script), config=config,
                project=str(tmp_path / "missing.uproject"),
            )

        assert result.success is False
        assert result.mode == "none"
        assert "--project path" in result.error
        remote.assert_not_called()
        commandlet.assert_not_called()

    def test_cli_invalid_explicit_project_stops_before_either_transport(
        self, tmp_path, monkeypatch
    ):
        """The command-line entry point preserves the same fail-closed rule."""
        proj = tmp_path / "MyGame"
        _make_uproject(proj / "MyGame.uproject")
        script = proj / "script.py"
        script.write_text("pass")
        monkeypatch.chdir(proj)
        monkeypatch.setattr("ue_runner.load_config", lambda *_args: RunnerConfig())
        monkeypatch.setattr(
            "sys.argv",
            ["ue_runner.py", str(script), "--project", str(tmp_path / "missing.uproject")],
        )

        with patch("ue_runner._try_remote") as remote, patch(
            "ue_runner._run_commandlet"
        ) as commandlet:
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1
        remote.assert_not_called()
        commandlet.assert_not_called()
