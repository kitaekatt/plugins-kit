"""The plugin's manifests: pins, versions, dependency edge, published status."""

from __future__ import annotations

import json
import re
import tomllib

from cck_support import PLUGIN_ROOT

from clean_code_kit import environment


def _pyproject():
    return tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_pins_are_40_char_shas():
    pins = environment.read_pins(PLUGIN_ROOT / "pyproject.toml")
    assert set(pins) == {"dryer", "crapper"}
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in pins.values())
    git_requirements = [r for r in _pyproject()["project"]["dependencies"] if "git+" in r]
    assert len(git_requirements) == len(pins)  # every git requirement is commit-pinned


def test_parser_pack_is_pinned_exactly():
    assert "tree-sitter-language-pack==1.25.0" in _pyproject()["project"]["dependencies"]


def test_versions_and_manifest_fields():
    plugin = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "clean-code-kit"
    assert plugin["version"] == _pyproject()["project"]["version"]
    assert plugin["dependencies"] == ["bootstrap"]
    assert "published" not in plugin  # published plugins omit the flag (missing = true)
    assert _pyproject()["tool"]["uv"]["package"] is False


def test_bootstrap_manifest():
    manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text())
    assert manifest["requires_bootstrap"] == "0.120.0"
    assert [t["name"] for t in manifest["tools"]] == ["git", "uv"]
    assert manifest["venv"]["check_imports"] == ["dryer", "crapper", "yaml", "tree_sitter_language_pack"]


# Distribution names whose import name is not the normalized distribution name.
_IMPORT_NAMES = {"pyyaml": "yaml"}


def _import_name(requirement: str) -> str:
    """Import name for one PEP 508 requirement string (extras, markers, versions, URL refs)."""
    dist = re.split(r"[\s;@\[<>=!~(]", requirement.strip(), maxsplit=1)[0]
    normalized = re.sub(r"[-_.]+", "-", dist).lower()
    return _IMPORT_NAMES.get(normalized, normalized.replace("-", "_"))


def test_check_imports_covers_every_declared_dependency():
    manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text())
    check_imports = manifest["venv"]["check_imports"]
    dependencies = _pyproject()["project"]["dependencies"]
    assert dependencies  # an empty list would make the loop below vacuous
    for requirement in dependencies:
        assert _import_name(requirement) in check_imports, requirement
