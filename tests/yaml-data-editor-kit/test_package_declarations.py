"""yaml-data-editor-kit gets llm_kit from the katsoft-llm-kit package, not from a shared-lib link."""

import json
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "yaml-data-editor-kit"
BOOTSTRAP = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
PLUGIN_JSON = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def test_llm_kit_is_not_a_shared_lib_link() -> None:
    """Red: put `llm_kit` back in `shared_lib_imports` (the link would outrank the package in the venv)."""
    assert "llm_kit" not in BOOTSTRAP["shared_lib_imports"]
    assert "bootstrap_lib" in BOOTSTRAP["shared_lib_imports"]


def test_the_package_requirements_are_declared_in_the_restricted_form() -> None:
    """Red: drop either requirement or widen its range. The extras are the ones the dispatch imports reach."""
    dependencies = PYPROJECT["project"]["dependencies"]
    assert "katsoft-llm-kit[config,sdk,pipeline]>=1.46.0,<2" in dependencies
    assert "katsoft-contracts>=1.0,<2" in dependencies


def test_the_bootstrap_floor_revokes_the_old_link_and_stamps_the_package_venv() -> None:
    """Red: lower the floor below 0.160.0."""
    assert BOOTSTRAP["requires_bootstrap"] == "0.160.0"


def test_plugin_json_and_pyproject_state_one_version() -> None:
    assert PLUGIN_JSON["version"] == PYPROJECT["project"]["version"]
