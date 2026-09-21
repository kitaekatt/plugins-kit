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
    ConfigError,
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
    import yaml
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


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

    def test_layers_merge_project_over_global_skill_and_defaults(self, tmp_path, monkeypatch):
        project = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(project, {
            "uproject": "/project/Game.uproject",
            "remote_execution": {"multicast_port": 7000},
        })
        global_path = tmp_path / "global.yaml"
        _write_yaml(global_path, {
            "engine_dir": "/global/Engine",
            "remote_execution": {"multicast_group": "239.1.1.1"},
        })
        skill_path = tmp_path / "skill.yaml"
        _write_yaml(skill_path, {
            "remote_execution": {"multicast_bind_address": "0.0.0.0"},
        })
        import ue_runner_config
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ue_runner_config, "SKILL_CONFIG_PATH", skill_path)
        monkeypatch.setattr(ue_runner_config, "_GLOBAL_CONFIG_PATH", global_path)

        config = load_config()
        assert config.uproject == "/project/Game.uproject"
        assert config.engine_dir == "/global/Engine"
        assert config.remote.multicast_group == "239.1.1.1"
        assert config.remote.multicast_bind_address == "0.0.0.0"
        assert config.remote.multicast_port == 7000

    def test_explicit_config_isolated_from_discovered_project_and_global(self, tmp_path, monkeypatch):
        project = tmp_path / PROJECT_CONFIG_NAME
        _write_yaml(project, {"uproject": "/project/Game.uproject"})
        global_path = tmp_path / "global.yaml"
        _write_yaml(global_path, {"engine_dir": "/global/Engine"})
        explicit = tmp_path / "explicit.yaml"
        _write_yaml(explicit, {"engine_dir": "/explicit/Engine"})
        import ue_runner_config
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(ue_runner_config, "_GLOBAL_CONFIG_PATH", global_path)

        config = load_config(config_path=explicit)
        assert config.engine_dir == "/explicit/Engine"
        assert config.uproject == ""

    @pytest.mark.parametrize("contents", ["[broken", "- a\n- b\n", "null\n", "remote_execution: []\n"])
    def test_invalid_explicit_layer_names_file_and_refuses(self, tmp_path, contents):
        explicit = tmp_path / "bad.yaml"
        explicit.write_text(contents, encoding="utf-8")
        with pytest.raises(ConfigError, match=str(explicit)):
            load_config(config_path=explicit)

    def test_missing_explicit_layer_is_an_error(self, tmp_path):
        explicit = tmp_path / "missing.yaml"
        with pytest.raises(ConfigError, match=str(explicit)):
            load_config(config_path=explicit)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("engine_dir", 42),
            ("uproject", 42),
            ("remote_execution", "null"),
            ("remote_execution", "[]"),
        ],
    )
    def test_invalid_types_refuse_with_path(self, tmp_path, field, value):
        explicit = tmp_path / "typed.yaml"
        if field == "remote_execution":
            explicit.write_text(f"{field}: {value}\n", encoding="utf-8")
        else:
            explicit.write_text(f'{field}: {value}\n', encoding="utf-8")
        with pytest.raises(ConfigError, match=str(explicit)):
            load_config(config_path=explicit)

    @pytest.mark.parametrize("port", [0, 65536, "quoted"])
    def test_invalid_remote_port_refuses(self, tmp_path, port):
        explicit = tmp_path / "port.yaml"
        raw = f'"{port}"' if isinstance(port, str) else str(port)
        explicit.write_text(f"remote_execution:\n  multicast_port: {raw}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="multicast_port"):
            load_config(config_path=explicit)

    def test_simple_parser_preserves_quoted_empty_and_numeric_strings(self, tmp_path):
        import ue_runner_config
        config = tmp_path / "quoted.yaml"
        config.write_text(
            'engine_dir: ""\nremote_execution:\n  multicast_group: "6766"\n',
            encoding="utf-8",
        )
        data = ue_runner_config._parse_yaml_simple(config)
        assert data["engine_dir"] == ""
        assert data["remote_execution"]["multicast_group"] == "6766"

    def test_atomic_write_preserves_nested_and_quoted_values(self, tmp_path):
        data = {
            "engine_dir": 'C:/UE/With "quotes"',
            "plugin_data_dir": "Generated/Plugin Data",
            "nested": {"keep": ["a", "b"], "quote": 'x"y'},
        }
        path = write_project_config(tmp_path, data)
        import ue_runner_config
        assert ue_runner_config._load_yaml(path) == data

    def test_atomic_write_failure_preserves_previous_bytes(self, tmp_path, monkeypatch):
        path = write_project_config(tmp_path, {"uproject": "old"})
        old = path.read_bytes()
        import ue_runner_config
        real_replace = ue_runner_config.os.replace

        def fail_replace(*args, **kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr(ue_runner_config.os, "replace", fail_replace)
        with pytest.raises(OSError, match="replace failed"):
            write_project_config(tmp_path, {"uproject": "new"})
        assert path.read_bytes() == old
        assert not list(path.parent.glob(".config.yaml.*.tmp"))
        monkeypatch.setattr(ue_runner_config.os, "replace", real_replace)
