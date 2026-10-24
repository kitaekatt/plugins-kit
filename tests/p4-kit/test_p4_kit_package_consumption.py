"""p4-kit consumes llm_kit as the katsoft-llm-kit package, not as a linked library.

The manifest keeps the bootstrap_lib link (the review lane runs in package mode
through llm_kit's fleet gate, which needs bootstrap_lib to import) and drops the
llm_kit link; pyproject.toml carries the released-package requirements in the
one form bootstrap resolves a venv's target from.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from bootstrap_lib.venv_check import parse_restricted_requirement, validate_package_target_document

_REPO = Path(__file__).resolve().parents[2]
_PLUGIN = _REPO / "plugins" / "p4-kit"
_PYPROJECT = tomllib.loads((_PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
_MANIFEST = json.loads((_PLUGIN / "bootstrap.json").read_text(encoding="utf-8"))
_RELEASED = validate_package_target_document(json.loads(
    (_REPO / "data" / "package-targets.json").read_text(encoding="utf-8")))


def _requirements() -> list[str]:
    project = _PYPROJECT["project"]
    out = list(project["dependencies"])
    for group in project.get("optional-dependencies", {}).values():
        out.extend(group)
    return out


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_llm_kit_is_installed_not_linked() -> None:
    assert _MANIFEST["shared_lib_imports"] == ["bootstrap_lib"]


def test_the_manifest_requires_the_bootstrap_that_revokes_the_old_link() -> None:
    assert _MANIFEST["requires_bootstrap"] == "0.160.0"


def test_the_package_requirements_are_declared() -> None:
    dependencies = _PYPROJECT["project"]["dependencies"]
    assert "katsoft-llm-kit[config]>=1.1,<2" in dependencies
    assert "katsoft-contracts>=1.0,<2" in dependencies
    assert _PYPROJECT["project"]["optional-dependencies"]["endpoint-dispatch"] == [
        "katsoft-llm-kit[sdk]>=1.1,<2"
    ]


def test_every_released_package_requirement_is_in_the_restricted_form() -> None:
    declared = [text for text in _requirements() if "katsoft" in text]
    assert len(declared) == 3, declared
    for text in declared:
        parsed = parse_restricted_requirement(text)
        assert parsed is not None, text
        name, minimum, exclusive_major = parsed
        assert name in _RELEASED, f"{name} is not in data/package-targets.json"
        assert any(
            minimum <= _version(release) < (exclusive_major,)
            for release in _RELEASED[name]
        ), f"no released {name} satisfies {text}"


def test_the_plugin_version_matches_its_manifest() -> None:
    plugin_json = json.loads(
        (_PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert _PYPROJECT["project"]["version"] == plugin_json["version"]
