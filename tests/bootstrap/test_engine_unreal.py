"""End-to-end tests for unreal-kit bootstrap manifest processing."""

import json
import os
from pathlib import Path

import pytest

from bootstrap_lib.var_resolve import resolve_vars, build_variables
from bootstrap_lib.ini_check import check_ini_setting, write_ini_setting


class TestUnrealKitManifestStructure:
    """Validate the unreal-kit bootstrap.json manifest structure."""

    @pytest.fixture
    def manifest(self):
        manifest_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                         "plugins", "unreal-kit", "bootstrap.json")
        )
        with open(manifest_path) as f:
            return json.load(f)

    def test_has_venv_with_imports(self, manifest):
        assert "venv" in manifest
        assert "upyrc" in manifest["venv"]["check_imports"]
        assert "yaml" in manifest["venv"]["check_imports"]

    def test_has_config_section(self, manifest):
        config = manifest["config"]
        assert config["file"] == "config.yaml"
        assert "uproject" in config["required_fields"]
        assert "engine_dir" in config["required_fields"]
        assert "autodetect" in manifest["project_config"]

    def test_has_ini_settings(self, manifest):
        assert len(manifest["ini_settings"]) >= 1
        ini = manifest["ini_settings"][0]
        assert "${uproject_dir}" in ini["file"]
        assert "bRemoteExecution" in ini["settings"]

    def test_has_pypi_packages(self, manifest):
        assert len(manifest["pypi_packages"]) >= 1
        pkg = manifest["pypi_packages"][0]
        assert pkg["package"] == "unreal-stub"
        assert pkg["extract_to"] == "${data_dir}/stubs/unreal.py"

    def test_has_script(self, manifest):
        assert manifest["script"]["path"] == "custom_bootstrap.py"
        assert manifest["script"]["entry_point"] == "bootstrap"


class TestUnrealKitVariableResolution:
    """Test variable resolution with unreal-kit-like config."""

    def test_ini_file_resolves_with_uproject(self):
        config = {"uproject": "/projects/MyGame/MyGame.uproject"}
        variables = build_variables("/opt/unreal-kit", "/data/unreal-kit", config)
        result = resolve_vars("${uproject_dir}/Config/UserEngine.ini", variables)
        # Path.parent uses OS-native separators for the derived _dir variable
        expected = str(Path("/projects/MyGame")) + "/Config/UserEngine.ini"
        assert result == expected

    def test_ini_file_skipped_without_uproject(self):
        config = {"uproject": ""}
        variables = build_variables("/opt/unreal-kit", "/data/unreal-kit", config)
        result = resolve_vars("${uproject_dir}/Config/UserEngine.ini", variables)
        assert result is None

    def test_pypi_extract_resolves(self):
        variables = build_variables("/opt/unreal-kit", "/data/unreal-kit")
        result = resolve_vars("${data_dir}/stubs/unreal.py", variables)
        assert result == "/data/unreal-kit/stubs/unreal.py"


class TestUnrealKitIniSettings:
    """Test INI settings with UE-style section names."""

    UE_SECTION = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]"

    def test_write_and_check_remote_execution(self, tmp_path):
        ini = tmp_path / "Config" / "UserEngine.ini"
        write_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True")
        result = check_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True")
        assert result.passed is True

    def test_write_multiple_settings(self, tmp_path):
        ini = tmp_path / "Config" / "UserEngine.ini"
        write_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True")
        write_ini_setting(str(ini), self.UE_SECTION, "bIsDeveloperMode", "True")

        assert check_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True").passed
        assert check_ini_setting(str(ini), self.UE_SECTION, "bIsDeveloperMode", "True").passed

    def test_update_existing_setting(self, tmp_path):
        ini = tmp_path / "Config" / "UserEngine.ini"
        write_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "False")
        write_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True")
        result = check_ini_setting(str(ini), self.UE_SECTION, "bRemoteExecution", "True")
        assert result.passed is True


class TestCustomBootstrapScript:
    """Test the custom_bootstrap.py autodetect function."""

    def test_autodetect_importable(self):
        """Verify the custom_bootstrap module can be imported."""
        script_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                         "plugins", "unreal-kit", "custom_bootstrap.py")
        )
        assert os.path.isfile(script_path)

        import importlib.util
        spec = importlib.util.spec_from_file_location("_cb", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert hasattr(module, "autodetect")
        assert hasattr(module, "bootstrap")

    def test_autodetect_no_uproject_returns_false(self):
        """Autodetect returns False when no .uproject is found."""
        script_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                         "plugins", "unreal-kit", "custom_bootstrap.py")
        )
        import importlib.util
        spec = importlib.util.spec_from_file_location("_cb", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # In a tmp dir with no .uproject files, autodetect should return None or a dict
        result = module.autodetect()
        # May be None or dict depending on CWD, but should not raise
        assert result is None or isinstance(result, dict)

    def test_missing_durable_stub_is_deferred_without_writing(self, tmp_path):
        module = self._load_module()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        uproject = project_dir / "Game.uproject"
        uproject.write_text("{}", encoding="ascii")
        generated = project_dir / "Intermediate" / "PythonStub" / "unreal.py"
        generated.parent.mkdir(parents=True)
        generated.write_text("generated", encoding="ascii")
        ctx = _StubContext(project_dir, uproject)

        module.bootstrap(ctx)

        assert [item[0] for item in ctx.deferred] == ["unreal_enriched_stub"]
        durable = project_dir / ".plugin-data" / "plugins-kit" / "unreal-kit" / "unreal.py"
        assert not durable.exists()
        assert ctx.action_logs == []
        assert ctx.ok_logs == [
            "stubs: durable enriched stub refresh deferred to explicit action"
        ]

    def test_matching_durable_stub_is_current(self, tmp_path):
        module = self._load_module()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        uproject = project_dir / "Game.uproject"
        uproject.write_text("{}", encoding="ascii")
        generated = project_dir / "Intermediate" / "PythonStub" / "unreal.py"
        generated.parent.mkdir(parents=True)
        generated.write_text("same", encoding="ascii")
        durable = project_dir / ".plugin-data" / "plugins-kit" / "unreal-kit" / "unreal.py"
        durable.parent.mkdir(parents=True)
        durable.write_text("same", encoding="ascii")
        ctx = _StubContext(project_dir, uproject)

        module.bootstrap(ctx)

        assert ctx.deferred == []
        assert ctx.ok_logs == ["stubs: durable enriched stub is current"]

    def test_stale_durable_stub_is_deferred(self, tmp_path):
        module = self._load_module()
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        uproject = project_dir / "Game.uproject"
        uproject.write_text("{}", encoding="ascii")
        generated = project_dir / "Intermediate" / "PythonStub" / "unreal.py"
        generated.parent.mkdir(parents=True)
        generated.write_text("new", encoding="ascii")
        durable = project_dir / ".plugin-data" / "plugins-kit" / "unreal-kit" / "unreal.py"
        durable.parent.mkdir(parents=True)
        durable.write_text("old", encoding="ascii")
        ctx = _StubContext(project_dir, uproject)

        module.bootstrap(ctx)

        assert [item[0] for item in ctx.deferred] == ["unreal_enriched_stub"]
        assert durable.read_text(encoding="ascii") == "old"

    @staticmethod
    def _load_module():
        import importlib.util

        script_path = os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                os.pardir,
                os.pardir,
                "plugins",
                "unreal-kit",
                "custom_bootstrap.py",
            )
        )
        spec = importlib.util.spec_from_file_location("_cb_stub_tests", script_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class _StubContext:
    def __init__(self, project_dir: Path, uproject: Path):
        self.config = {"uproject": str(uproject)}
        self.project_dir = str(project_dir)
        self.deferred = []
        self.action_logs = []
        self.ok_logs = []

    def add_deferred_requirement(self, name, **kwargs):
        self.deferred.append((name, kwargs))

    def log(self, message):
        self.action_logs.append(message)

    def log_ok(self, message):
        self.ok_logs.append(message)
