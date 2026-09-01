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


def test_awesome_kit_version_and_description_match_remaining_skills() -> None:
    manifest = _read_json(_PLUGIN_MANIFEST)
    description = manifest["description"]

    assert manifest["version"] == "0.42.0"
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
