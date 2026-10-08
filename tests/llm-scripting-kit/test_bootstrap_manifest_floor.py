"""Manifest floor for plugins/llm-scripting-kit/bootstrap.json.

``requires_bootstrap`` is set from the CALLS this plugin makes into
``bootstrap_lib`` (plugins/CLAUDE.md, "Set the requires_bootstrap floor from
the CALLS a plugin makes"): ``model_declaration`` (0.129.0),
``execution_event`` with the v1 schema and ``usage_payload`` (0.135.0), and
``skill_material`` (0.138.0), and ``build_codex_exec_argv(ignore_user_config=)``
(0.146.0). The floor is the highest of them.
"""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "llm-scripting-kit"
MANIFEST = PLUGIN / "bootstrap.json"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"


def _version(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_requires_bootstrap_covers_every_bootstrap_lib_call() -> None:
    from llm_scripting_kit import declaration
    from llm_scripting_kit.completion import skill_context

    floors = (
        declaration._MODEL_DECLARATION_BOOTSTRAP,
        declaration.EXECUTION_EVENT_BOOTSTRAP,
        skill_context.SKILL_MATERIAL_BOOTSTRAP,
    )
    # The codex text-only mode passes ignore_user_config (bootstrap 0.146.0),
    # a keyword no constant tracks, so the manifest may only sit at or above.
    assert _version(_manifest()["requires_bootstrap"]) >= _version(max(floors, key=_version))


def test_skill_material_floor_is_pinned_literally() -> None:
    """The constant and the manifest are each pinned to the literal, so moving
    both back together cannot stay green (the test above derives its floor
    from the constants)."""
    from llm_scripting_kit.completion import skill_context

    assert skill_context.SKILL_MATERIAL_BOOTSTRAP == "0.138.0"
    assert _manifest()["requires_bootstrap"] == "0.146.0"


def test_manifest_adds_no_edge() -> None:
    """The floor is the only line this item changes: no plugins[] entry, no
    further shared-lib import, no plugin dependency beyond bootstrap."""
    manifest = _manifest()
    assert manifest["shared_lib_imports"] == ["bootstrap_lib"]
    assert "plugins" not in manifest
    plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert plugin["dependencies"] == ["bootstrap"]
