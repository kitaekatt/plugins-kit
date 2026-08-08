"""Integration tests for durable project-data manifest resolution."""

from pathlib import Path
from unittest.mock import patch

from bootstrap_lib.engine import _process_manifest
from bootstrap_lib.pypi_check import PypiCheckResult


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_pypi_extract_to_resolves_project_config_override(tmp_path):
    project_root = tmp_path / "project"
    plugin_data_dir = tmp_path / "user-data" / "plugins-kit" / "demo-kit"
    project_config = (
        project_root
        / ".local-data"
        / "plugins-kit"
        / "demo-kit"
        / "config.yaml"
    )
    _write(plugin_data_dir / "config.yaml", "plugin_data_dir: UserGenerated\n")
    _write(project_config, "plugin_data_dir: Generated/PluginData\n")

    observed = []

    def check(package, extract_to):
        observed.append((package, extract_to))
        return PypiCheckResult(True, package, "exists")

    manifest = {
        "pypi_packages": [{
            "package": "generated-api",
            "extract_to": "${plugin_data_dir}/api.py",
        }],
    }
    with patch("bootstrap_lib.pypi_check.check_pypi_package", side_effect=check):
        failures = _process_manifest(
            manifest,
            "windows",
            str(plugin_data_dir),
            str(tmp_path / "plugin-root"),
            [],
            [],
            plugin_name="demo-kit",
            project_dir=str(project_root),
            marketplace="plugins-kit",
        )

    assert failures == []
    assert len(observed) == 1
    assert observed[0][0] == "generated-api"
    assert Path(observed[0][1]) == (
        project_root / "Generated" / "PluginData" / "api.py"
    ).resolve()
