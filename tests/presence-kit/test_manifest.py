"""presence-kit 0.3.0 manifest shape: plugin.json, bootstrap.json, hooks.json."""
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"
REPO = PLUGIN.parents[1]


def _load(rel: str) -> dict:
    return json.loads((PLUGIN / rel).read_text(encoding="utf-8"))


def test_plugin_json_shape():
    manifest = _load(".claude-plugin/plugin.json")
    assert manifest["name"] == "presence-kit"
    assert manifest["version"] == "0.3.3"
    assert manifest["dependencies"] == ["bootstrap"]
    assert manifest["description"].strip()


def test_marketplace_entry_matches_the_plugin_manifest():
    """The derived entry (scripts/regen_marketplace.py) carries the same version."""
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    (entry,) = [p for p in market["plugins"] if p["name"] == "presence-kit"]
    manifest = _load(".claude-plugin/plugin.json")
    assert entry["version"] == manifest["version"]
    assert entry["description"] == manifest["description"]


def test_bootstrap_json_shape():
    manifest = _load("bootstrap.json")
    # 0.141.0: the first bootstrap that re-emits the recorded <PLUGIN>_ROOT names into a
    # gate-skipped session, so the prompt hook's undo command (PRESENCE_KIT_ROOT) resolves.
    assert manifest["requires_bootstrap"] == "0.141.0"
    assert isinstance(manifest["$comment"], str) and manifest["$comment"]


def test_hooks_json_wires_the_prompt_and_stop_hooks():
    # Commands and wrapper detail: test_hook_wrappers.py.
    assert list(_load("hooks/hooks.json")["hooks"]) == ["UserPromptSubmit", "Stop"]
