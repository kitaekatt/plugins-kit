"""Tests for per-project config: find, write, and load from
.local-data/plugins-kit/unreal-kit/config.yaml (PROJECT_CONFIG_NAME)."""

import json
import sys
from pathlib import Path

import pytest

# Add lib/ to path
_LIB_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "unreal-kit" / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from ue_runner_config import (
    PROJECT_CONFIG_NAME,
    RunnerConfig,
    _GLOBAL_CONFIG_PATH,
    find_project_config,
    load_config,
    write_project_config,
)


def _make_uproject(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"Modules": [{"Name": "Test"}]}), encoding="utf-8")


def _write_yaml(path: Path, data: dict):
    """Write simple YAML key: "value" file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{k}: "{v}"' for k, v in data.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestFindProjectConfig:
    """find_project_config should walk up from CWD looking for PROJECT_CONFIG_NAME."""

    def test_finds_config_in_cwd(self, tmp_path, monkeypatch):
        config_file = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(config_file, {"uproject": "test.uproject"})
        monkeypatch.chdir(tmp_path)

        result = find_project_config()
        assert result is not None
        assert result == config_file

    def test_finds_config_in_parent(self, tmp_path, monkeypatch):
        config_file = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(config_file, {"uproject": "test.uproject"})
        subdir = tmp_path / "Content" / "Python"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)

        result = find_project_config()
        assert result is not None
        assert result == config_file

    def test_returns_none_when_no_config(self, tmp_path, monkeypatch):
        # Deep path so walking up stays inside tmp_path
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j" / "k"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        result = find_project_config()
        assert result is None

    def test_explicit_start_path(self, tmp_path):
        config_file = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(config_file, {"uproject": "test.uproject"})
        subdir = tmp_path / "Source" / "MyGame"
        subdir.mkdir(parents=True)

        result = find_project_config(start=subdir)
        assert result is not None
        assert result == config_file


class TestWriteProjectConfig:
    """write_project_config should create the config dir and write YAML."""

    def test_creates_config(self, tmp_path):
        data = {"uproject": "C:\\Projects\\MyGame\\MyGame.uproject", "engine_dir": "C:\\UE5\\Engine"}
        result = write_project_config(tmp_path, data)

        assert result == tmp_path / PROJECT_CONFIG_NAME
        assert result.is_file()

        content = result.read_text(encoding="utf-8")
        assert "C:/Projects/MyGame/MyGame.uproject" in content
        assert "C:/UE5/Engine" in content
        # Backslashes should be converted to forward slashes
        assert "\\" not in content

    def test_creates_config_dir(self, tmp_path):
        config_dir = (tmp_path / PROJECT_CONFIG_NAME).parent
        assert not config_dir.exists()

        write_project_config(tmp_path, {"uproject": "test.uproject"})
        assert config_dir.is_dir()

    def test_overwrites_existing(self, tmp_path):
        write_project_config(tmp_path, {"uproject": "old.uproject"})
        write_project_config(tmp_path, {"uproject": "new.uproject"})

        content = (tmp_path / PROJECT_CONFIG_NAME).read_text(encoding="utf-8")
        assert "new.uproject" in content
        assert "old.uproject" not in content


class TestLoadConfig:
    """load_config should prefer per-project config over global."""

    def test_uses_per_project_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(config_file, {
            "uproject": "/projects/GameA/GameA.uproject",
            "engine_dir": "/projects/GameA/Engine",
        })
        monkeypatch.chdir(tmp_path)

        config = load_config()
        assert config.uproject == "/projects/GameA/GameA.uproject"
        assert config.engine_dir == "/projects/GameA/Engine"

    def test_falls_back_to_global_config(self, tmp_path, monkeypatch):
        """When no per-project config exists, fall back to global."""
        # Deep path so walking up stays inside tmp_path
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j" / "k"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        # Write a global config
        global_path = tmp_path / "global_config.yaml"
        _write_yaml(global_path, {
            "uproject": "/global/path.uproject",
            "engine_dir": "/global/engine",
        })

        # Patch _GLOBAL_CONFIG_PATH to point to our test file
        import ue_runner_config
        monkeypatch.setattr(ue_runner_config, "_GLOBAL_CONFIG_PATH", global_path)

        config = load_config()
        assert config.uproject == "/global/path.uproject"

    def test_explicit_config_path_overrides_all(self, tmp_path, monkeypatch):
        # Per-project config exists
        config_file = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(config_file, {"uproject": "/per-project/path.uproject"})
        monkeypatch.chdir(tmp_path)

        # But explicit path is different
        explicit = tmp_path / "explicit.yaml"
        _write_yaml(explicit, {"uproject": "/explicit/path.uproject"})

        config = load_config(config_path=explicit)
        assert config.uproject == "/explicit/path.uproject"

    def test_defaults_when_nothing_found(self, tmp_path, monkeypatch):
        """When no config files exist, use hardcoded defaults."""
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "i" / "j" / "k"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        import ue_runner_config
        monkeypatch.setattr(ue_runner_config, "_GLOBAL_CONFIG_PATH", tmp_path / "nonexistent.yaml")

        config = load_config()
        assert config.uproject == ""
        assert config.engine_dir == ""
        # Remote config defaults should still be present
        assert config.remote.multicast_port == 6766
