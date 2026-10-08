"""Manifest-shape test for plugins/job-kit/bootstrap.json.

Guards the quota-resilient-dispatch migration step 1 (Decision 2 in
docs/planning/quota-resilient-dispatch/declaration-format-design.md): job-kit's
own code will call bootstrap_lib.model_declaration for declaration-shape
validation, so bootstrap_lib must be a declared shared_lib_imports edge before
any import lands.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "plugins" / "job-kit" / "bootstrap.json"


def test_job_kit_bootstrap_lib_is_a_declared_shared_lib_import() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "bootstrap_lib" in manifest.get("shared_lib_imports", []), (
        "job-kit/bootstrap.json must declare bootstrap_lib in "
        "shared_lib_imports so bootstrap_lib.model_declaration is importable"
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_requires_bootstrap_covers_every_bootstrap_lib_call() -> None:
    """job-kit calls bootstrap_lib.model_declaration.parse (bootstrap 0.129.0)
    records every ledger transition through bootstrap_lib.execution_event
    with the v2 schema selector (bootstrap 0.136.0), and checks interrupts
    with bootstrap_lib.interrupt_contract (bootstrap 0.137.0). The floor is
    the HIGHEST call shape it uses, not what happens to import."""
    import job_kit.events as events
    import job_kit.interrupts as interrupts
    import job_kit.model as model

    floors = (
        model._MODEL_DECLARATION_BOOTSTRAP,
        events._EXECUTION_EVENT_BOOTSTRAP,
        interrupts._INTERRUPT_CONTRACT_BOOTSTRAP,
    )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == max(floors, key=_version)


def test_execution_event_floor_is_0_136_0() -> None:
    """The execution-event floor is the literal 0.136.0 -- the bootstrap that
    shipped plugins-kit.execution-event/v2 and make_event(schema=) -- asserted
    independently of the manifest, so moving the constant back alone cannot
    stay green. The manifest floor is the higher interrupt-contract one,
    pinned below."""
    import job_kit.events as events

    assert events._EXECUTION_EVENT_BOOTSTRAP == "0.136.0"


def test_interrupt_contract_floor_is_pinned_literally() -> None:
    """The contract floor is the literal 0.137.0 -- the bootstrap that shipped
    bootstrap_lib.interrupt_contract with the call shapes job-kit binds --
    asserted for the constant and the manifest apart, so moving both back
    together cannot stay green (the derived-floor test above derives its floor
    from the constants)."""
    import job_kit.interrupts as interrupts

    assert interrupts._INTERRUPT_CONTRACT_BOOTSTRAP == "0.137.0"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == "0.137.0"


def test_validator_floor_is_pinned_literally() -> None:
    """The validator floor is the literal 0.56.0 -- the llm-scripting-kit that
    shipped the subset selector on check_schema and validate."""
    import job_kit.interrupts as interrupts

    assert interrupts._JSON_SCHEMA_LSK_VERSION == "0.56.0"


def test_llm_scripting_kit_min_version_is_the_subset_release() -> None:
    """The manifest asks bootstrap for the llm-scripting-kit that ships the
    subset selector, on the entry job-kit already declared; no new entry."""
    import job_kit.interrupts as interrupts

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in manifest.get("plugins", [])
        if entry.get("ref") == "plugins-kit:llm-scripting-kit"
    ]
    assert len(entries) == 1 and len(manifest["plugins"]) == 1
    assert entries[0].get("install") == "auto"
    # The floor also covers arm_call, HALT_BACKPRESSURE, HaltError.retry_after_s
    # and classify_backpressure (llm-scripting-kit 0.61.0), above the subset release.
    assert entries[0].get("min_version") == "0.61.0"
    assert tuple(map(int, entries[0]["min_version"].split("."))) >= tuple(
        map(int, interrupts._JSON_SCHEMA_LSK_VERSION.split("."))
    )
