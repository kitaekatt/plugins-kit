"""Pin workflow-kit's bootstrap.json shared-lib declarations.

The openrouter node path resolves models via llm_scripting_kit.models, whose
load_model_config() needs bootstrap_lib.config_resolve to read the layered
user/project config.yaml. Without "bootstrap_lib" in shared_lib_imports the
provisioned venv cannot import it and the function silently falls back to the
shipped baseline -- user/project model config is ignored for every openrouter
node. This test pins the declaration so it cannot regress.
"""

import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "workflow-kit"


class TestSharedLibImports:
    def test_declares_llm_scripting_kit_and_bootstrap_lib(self):
        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        shared = manifest.get("shared_lib_imports", [])
        assert "llm_scripting_kit" in shared
        assert "bootstrap_lib" in shared


class TestTools:
    def test_no_dead_claude_tool_entry(self):
        # W10: bootstrap cannot install `claude` (every platform said "manual"),
        # so the entry was dead weight that could only ever produce a useless
        # fix-all. Pin its removal.
        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        names = [t["name"] for t in manifest.get("tools", [])]
        assert "claude" not in names


def _version(text):
    return tuple(int(part) for part in text.split("."))


class TestRequiresBootstrap:
    def test_floor_covers_every_required_bootstrap_lib_call(self):
        # The loader calls bootstrap_lib.model_declaration.parse / CORE_IDS
        # (MODEL_DECLARATION_BOOTSTRAP), every compiled openrouter node calls
        # bootstrap_lib.execution_event.Emitter / JsonlSink
        # (EXECUTION_EVENT_BOOTSTRAP), and every compiled provider node calls
        # Emitter(..., schema=<v3>) for its contract event
        # (CONTRACT_EVENT_BOOTSTRAP). The floor is the newest of the three.
        from workflow_kit_lib.declarations import (
            CONTRACT_EVENT_BOOTSTRAP,
            EXECUTION_EVENT_BOOTSTRAP,
            MODEL_DECLARATION_BOOTSTRAP,
        )

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        floor = max(
            MODEL_DECLARATION_BOOTSTRAP,
            EXECUTION_EVENT_BOOTSTRAP,
            CONTRACT_EVENT_BOOTSTRAP,
            key=_version,
        )
        assert manifest.get("requires_bootstrap") == floor
        assert "execution_event" in manifest["$comment"]

    def test_contract_event_floor_is_0_137_0(self):
        # A literal, independent of the constants: bootstrap 0.137.0 shipped
        # execution-event schema v3 with the contract event. Moving the
        # constant and the manifest back together keeps the test above green;
        # it turns this one red.
        from workflow_kit_lib.declarations import CONTRACT_EVENT_BOOTSTRAP

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        assert CONTRACT_EVENT_BOOTSTRAP == "0.137.0"
        assert manifest["requires_bootstrap"] == "0.137.0"

    def test_bootstrap_comment_names_contract_event_call(self):
        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        comment = manifest["$comment"]
        assert 'Emitter(..., schema="plugins-kit.execution-event/v3")' in comment
        assert "contract event" in comment
        assert "0.137.0" in comment
