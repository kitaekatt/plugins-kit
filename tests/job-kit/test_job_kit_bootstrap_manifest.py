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
    and records every ledger transition through bootstrap_lib.execution_event
    with the v2 schema selector (bootstrap 0.136.0). The floor is the HIGHEST
    call shape it uses, not what happens to import."""
    import job_kit.events as events
    import job_kit.model as model

    floors = (
        model._MODEL_DECLARATION_BOOTSTRAP,
        events._EXECUTION_EVENT_BOOTSTRAP,
    )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == max(floors, key=_version)


def test_execution_event_floor_is_0_136_0() -> None:
    """The execution-event floor is the literal 0.136.0 -- the bootstrap that
    shipped plugins-kit.execution-event/v2 and make_event(schema=) -- asserted
    independently of the manifest, so moving the constant and the manifest back
    together cannot stay green (the test above derives its floor from the
    constants)."""
    import job_kit.events as events

    assert events._EXECUTION_EVENT_BOOTSTRAP == "0.136.0"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == events._EXECUTION_EVENT_BOOTSTRAP
