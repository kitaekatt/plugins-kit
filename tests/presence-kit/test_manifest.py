"""presence-kit 0.2.0 manifest shape: plugin.json, bootstrap.json, hooks.json."""
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "presence-kit"


def _load(rel: str) -> dict:
    return json.loads((PLUGIN / rel).read_text(encoding="utf-8"))


def test_plugin_json_shape():
    manifest = _load(".claude-plugin/plugin.json")
    assert manifest["name"] == "presence-kit"
    assert manifest["version"] == "0.2.0"
    assert manifest["dependencies"] == ["bootstrap"]
    assert manifest["description"].strip()


def test_bootstrap_json_shape():
    manifest = _load("bootstrap.json")
    assert manifest["requires_bootstrap"] == "0.120.0"
    assert isinstance(manifest["$comment"], str) and manifest["$comment"]


def test_hooks_json_wires_only_stop():
    # Command and wrapper detail: test_hook_wrappers.py.
    assert list(_load("hooks/hooks.json")["hooks"]) == ["Stop"]
