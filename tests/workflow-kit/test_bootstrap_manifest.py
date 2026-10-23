"""Pin workflow-kit's bootstrap.json shared-lib declarations and package pins.

The openrouter node path resolves models via llm_kit.registry.models, whose
load_model_config() needs bootstrap_lib.config_resolve to read the layered
user/project config.yaml. Without "bootstrap_lib" in shared_lib_imports the
provisioned venv cannot import it and the function silently falls back to the
shipped baseline -- user/project model config is ignored for every openrouter
node. llm_kit itself is the katsoft-llm-kit package in the plugin's own venv,
never a shared-lib link. These tests pin both declarations so they cannot
regress.
"""

import json
import tomllib
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "workflow-kit"


class TestSharedLibImports:
    def test_declares_bootstrap_lib_and_not_llm_kit(self):
        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        shared = manifest.get("shared_lib_imports", [])
        assert "bootstrap_lib" in shared
        # llm_kit arrives as the katsoft-llm-kit package (TestPackagePins); a
        # link would shadow the pinned release with whatever the owner ships.
        assert "llm_kit" not in shared


class TestPackagePins:
    def _dependencies(self):
        data = tomllib.loads((PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return data["project"]["dependencies"]

    def test_pins_the_released_llm_kit_with_the_extras_its_paths_need(self):
        # config: the registry loader's pyyaml; sdk: the openai SDK the
        # OpenRouter backend imports.
        assert "katsoft-llm-kit[config,sdk]>=1.1,<2" in self._dependencies()

    def test_pins_the_released_contracts_distribution(self):
        assert "katsoft-contracts>=1.0,<2" in self._dependencies()

    def test_keeps_the_pyyaml_the_loader_imports_directly(self):
        assert any(dep.startswith("pyyaml") for dep in self._dependencies())


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
        # (CONTRACT_EVENT_BOOTSTRAP), and an agent step's model: calls
        # model_declaration.parse_entries (MODEL_ENTRIES_BOOTSTRAP). The floor
        # is the highest version of the four, raised to the package-mode floor
        # (PACKAGE_MODE_BOOTSTRAP) a katsoft-llm-kit venv relies on.
        from workflow_kit_lib.declarations import (
            CONTRACT_EVENT_BOOTSTRAP,
            EXECUTION_EVENT_BOOTSTRAP,
            MODEL_DECLARATION_BOOTSTRAP,
            MODEL_ENTRIES_BOOTSTRAP,
            PACKAGE_MODE_BOOTSTRAP,
        )

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        floor = max(
            MODEL_DECLARATION_BOOTSTRAP,
            EXECUTION_EVENT_BOOTSTRAP,
            CONTRACT_EVENT_BOOTSTRAP,
            MODEL_ENTRIES_BOOTSTRAP,
            PACKAGE_MODE_BOOTSTRAP,
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
        assert _version(manifest["requires_bootstrap"]) >= _version("0.137.0")

    def test_model_entries_floor_is_0_150_0(self):
        # A literal, independent of the constants: bootstrap 0.150.0 shipped
        # model_declaration.parse_entries, which an agent step's model: calls.
        from workflow_kit_lib.declarations import MODEL_ENTRIES_BOOTSTRAP

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        assert MODEL_ENTRIES_BOOTSTRAP == "0.150.0"
        assert _version(manifest["requires_bootstrap"]) >= _version("0.150.0")
        assert "parse_entries" in manifest["$comment"]

    def test_package_mode_floor_is_0_160_0(self):
        # A literal, independent of the constant: bootstrap 0.160.0 revokes
        # undeclared bootstrap-written .pth links and stamps released-package
        # targets, which a venv holding the katsoft-llm-kit package relies on.
        # An older engine keeps a stale llm_kit link beside the package.
        from workflow_kit_lib.declarations import PACKAGE_MODE_BOOTSTRAP

        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        assert PACKAGE_MODE_BOOTSTRAP == "0.160.0"
        assert manifest["requires_bootstrap"] == "0.160.0"

    def test_bootstrap_comment_names_contract_event_call(self):
        manifest = json.loads((PLUGIN_ROOT / "bootstrap.json").read_text(encoding="utf-8"))
        comment = manifest["$comment"]
        assert 'Emitter(..., schema="plugins-kit.execution-event/v3")' in comment
        assert "contract event" in comment
        assert "0.137.0" in comment
