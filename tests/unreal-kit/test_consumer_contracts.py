"""Consumer-facing contracts for the shipped unreal-kit package."""

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "unreal-kit"
sys.path.insert(0, str(PLUGIN / "lib"))
README = PLUGIN / "README.md"
PYTHON_SKILL = PLUGIN / "skills" / "ue-python-api" / "SKILL.md"
SCRIPT_BOOTSTRAP = PYTHON_SKILL.parent / "references" / "script-bootstrap.md"
UNREAL_PIP = PYTHON_SKILL.parent / "references" / "unreal-pip.md"
MCP_SKILL = PLUGIN / "skills" / "ue-mcp-server" / "SKILL.md"
CATALOG = MCP_SKILL.parent / "references" / "tool-catalog.md"
WORKFLOWS = MCP_SKILL.parent / "references" / "workflows.md"
FIXUP_SKILL = PLUGIN / "skills" / "fix-up-redirectors" / "SKILL.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_shipped_surface_does_not_depend_on_maintainer_only_files():
    assert not (PLUGIN / "lib" / "CLAUDE.md").exists()

    package_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in PLUGIN.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".json", ".toml"}
    )
    assert "tests/bootstrap" not in package_text
    assert "bootstrap-reset-cooldown.sh" not in package_text
    assert "DEFAULT_EXTENSIONS" not in package_text
    assert "Extend the scan logic" not in package_text


def test_host_requirements_are_owned_by_bootstrap_manifest():
    assert not (PYTHON_SKILL.parent / "host-requirements.txt").exists()

    pyproject = _text(PLUGIN / "pyproject.toml")
    manifest = json.loads(_text(PLUGIN / "bootstrap.json"))
    declared = {"upyrc", "pyyaml", "websocket-client"}
    assert all(f'"{name}"' in pyproject for name in declared)
    assert set(manifest["venv"]["check_imports"]) == {"upyrc", "yaml", "websocket"}


def test_consumer_config_examples_match_resolver_and_autodetection():
    from ue_runner_config import PROJECT_CONFIG_NAME

    bootstrap = _text(SCRIPT_BOOTSTRAP)
    readme = _text(README)
    assert PROJECT_CONFIG_NAME in bootstrap
    assert "~/.claude/.local-data/skills/" not in bootstrap
    assert "walking up from the directory Claude" not in readme
    assert "current directory and up to two child levels" in readme


def test_git_dependency_guidance_names_bootstrap_data_location():
    text = _text(UNREAL_PIP)
    assert "<data_dir>/github/unreal-pip" in text
    assert "vendored" not in text.lower()
    assert "pip install -r host-requirements.txt" not in _text(PYTHON_SKILL)


def test_mcp_catalog_does_not_claim_an_authoritative_schema():
    catalog = _text(CATALOG)
    skill = _text(MCP_SKILL)
    assert "Complete catalog" not in catalog
    assert "parameter shape" not in catalog
    assert "response shape" not in catalog
    assert "Stub document" not in catalog
    assert "summary" in catalog.lower()
    assert "Complete catalog" not in skill
    assert "summary" in skill.lower()


def test_pie_recipe_separates_runtime_testing_from_editor_capture():
    workflow = _text(WORKFLOWS)
    pie = workflow.split("## PIE drive for testing", 1)[1]
    assert pie.index("stop_pie") < pie.index("screenshot")
    assert "runtime visual capture" in pie.lower()
    assert "unverified" in pie.lower()


@pytest.mark.parametrize(
    "path",
    [
        README,
        PYTHON_SKILL,
        SCRIPT_BOOTSTRAP,
        UNREAL_PIP,
        MCP_SKILL,
        CATALOG,
        WORKFLOWS,
        FIXUP_SKILL,
    ],
)
def test_reviewed_consumer_guidance_is_ascii(path):
    text = _text(path)
    assert text.isascii(), f"non-ASCII consumer guidance in {path}"


def test_fixup_guidance_has_no_relative_cadence_claims():
    text = _text(FIXUP_SKILL)
    assert "every couple of weeks" not in text
    assert chr(0x2014) not in text
