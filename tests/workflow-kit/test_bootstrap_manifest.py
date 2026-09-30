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
        # (MODEL_DECLARATION_BOOTSTRAP), and every compiled openrouter node
        # calls bootstrap_lib.execution_event.Emitter / JsonlSink
        # (EXECUTION_EVENT_BOOTSTRAP). The floor is the newer of the two.
        from workflow_kit_lib.declarations import (
            EXECUTION_EVENT_BOOTSTRAP,
            MODEL_DECLARATION_BOOTSTRAP,
        )

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        floor = max(MODEL_DECLARATION_BOOTSTRAP, EXECUTION_EVENT_BOOTSTRAP, key=_version)
        assert manifest.get("requires_bootstrap") == floor
        assert "execution_event" in manifest["$comment"]
