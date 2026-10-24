"""Plugin dependency integration with the installed distribution metadata."""
from importlib.metadata import distribution
from pathlib import Path
import json
import tomllib

from packaging.requirements import Requirement
from packaging.version import Version

PLUGIN = Path(__file__).resolve().parents[2] / "plugins/llm-kit"

def test_plugin_requirements_are_satisfied_by_installed_distributions():
    project = tomllib.loads((PLUGIN / "pyproject.toml").read_text())["project"]
    for text in project["dependencies"]:
        requirement = Requirement(text)
        installed = distribution(requirement.name)
        assert Version(installed.version) in requirement.specifier
        assert requirement.extras <= set(installed.metadata.get_all("Provides-Extra") or [])

def test_installed_library_supplies_the_console_entry_point():
    scripts = {entry.name: entry.value for entry in distribution("katsoft-llm-kit").entry_points if entry.group == "console_scripts"}
    assert scripts == {"llm-kit": "llm_kit.cli:main"}

def test_plugin_remains_a_thin_non_package_consumer():
    project = tomllib.loads((PLUGIN / "pyproject.toml").read_text())
    assert project["tool"]["uv"]["package"] is False
    assert "scripts" not in project["project"]
    assert not (PLUGIN / "lib").exists()
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["version"] == project["project"]["version"]
