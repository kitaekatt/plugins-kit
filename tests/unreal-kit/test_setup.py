"""Tests for ue_runner.py --setup (run_setup).

U7 regression: --setup used to re-implement the bootstrap manifest by hand --
writing UserEngine.ini settings (duplicating bootstrap.json's ini_settings)
and checking host deps with a raw `pip install` recommendation (against the
repo's bootstrap-only dependency convention). It is now scoped to the one
genuinely interactive step: .uproject disambiguation + per-project config
write; ini settings and deps are bootstrap's job.
"""

import sys
from pathlib import Path
from unittest.mock import patch

# Add scripts/ and lib/ to path
_SKILL_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "skills" / "ue-python-api"
_PLUGIN_DIR = _SKILL_DIR.parent.parent
_SCRIPTS_DIR = _SKILL_DIR / "scripts"
_LIB_DIR = _PLUGIN_DIR / "lib"
for p in (_SCRIPTS_DIR, _LIB_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import ue_runner
from ue_runner import run_setup
from ue_runner_config import PROJECT_CONFIG_NAME, RunnerConfig

_UE_RUNNER_SRC = (_SCRIPTS_DIR / "ue_runner.py").read_text(encoding="utf-8")


def _make_project(tmp_path):
    uproject = tmp_path / "Proj" / "Game.uproject"
    uproject.parent.mkdir(parents=True)
    uproject.write_text("{}")
    engine = tmp_path / "Engine"
    engine.mkdir()
    return uproject, engine


class TestRunSetup:
    def test_writes_project_config_and_succeeds(self, tmp_path, monkeypatch, capsys):
        uproject, engine = _make_project(tmp_path)
        monkeypatch.chdir(uproject.parent)
        # Any unexpected interactive prompt beyond the patched helpers fails loudly.
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected input() prompt")))

        with patch.object(ue_runner, "_ask_uproject_path", return_value=uproject), \
             patch.object(ue_runner, "find_engine_dir", return_value=engine), \
             patch.object(ue_runner, "_confirm", return_value=True):
            ok = run_setup(RunnerConfig())

        assert ok is True
        config_file = uproject.parent / PROJECT_CONFIG_NAME
        assert config_file.is_file()
        content = config_file.read_text(encoding="utf-8")
        assert "Game.uproject" in content
        assert "engine_dir" in content
        # Setup must point at bootstrap for the rest, not do it itself.
        out = capsys.readouterr().out
        assert "bootstrap" in out

    def test_declined_save_returns_false(self, tmp_path, monkeypatch):
        uproject, engine = _make_project(tmp_path)
        monkeypatch.chdir(uproject.parent)

        with patch.object(ue_runner, "_ask_uproject_path", return_value=uproject), \
             patch.object(ue_runner, "find_engine_dir", return_value=engine), \
             patch.object(ue_runner, "_confirm", return_value=False):
            ok = run_setup(RunnerConfig())

        assert ok is False
        assert not (uproject.parent / PROJECT_CONFIG_NAME).exists()


class TestSetupDoesNotDuplicateBootstrap:
    """Static guards: the manifest re-implementation must not creep back."""

    def test_no_pip_install_recommendation(self):
        assert "pip install" not in _UE_RUNNER_SRC

    def test_no_ini_writes(self):
        assert "write_ini_setting" not in _UE_RUNNER_SRC
        assert "ue_ini" not in _UE_RUNNER_SRC
