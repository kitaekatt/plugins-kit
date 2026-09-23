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


def test_requires_bootstrap_covers_the_model_declaration_call() -> None:
    """job-kit calls bootstrap_lib.model_declaration.parse (directly, and through
    llm-scripting-kit's describe), which first shipped in bootstrap 0.129.0; the
    floor is set from that call, not from what happens to import."""
    import job_kit.model as model

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == model._MODEL_DECLARATION_BOOTSTRAP
