"""Static manifest invariants for awesome-kit after pdf-kit extraction."""

import json
from pathlib import Path
from typing import Any

_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "awesome-kit"
    / "bootstrap.json"
)
_PLUGIN_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "awesome-kit"
    / ".claude-plugin"
    / "plugin.json"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_awesome_kit_keeps_yaml_only_in_check_imports() -> None:
    manifest = _read_json(_MANIFEST)

    assert manifest["venv"]["check_imports"] == ["yaml"]
    assert "script" not in manifest


def test_awesome_kit_description_matches_remaining_skills() -> None:
    manifest = _read_json(_PLUGIN_MANIFEST)
    description = manifest["description"]

    # Deliberately NOT pinned to an exact version: this test guards the
    # description against drifting away from the skills that remain after the
    # pdf-kit extraction, and an exact-version assert would fail on every
    # routine bump while proving nothing about that.
    assert manifest["version"].count(".") == 2
    assert "html-pdf" not in description
    assert all(
        skill_name in description
        for skill_name in (
            "plugin-ecosystem",
            "task",
            "orchestrate",
            "recap",
            "verbose-updates",
        )
    )
