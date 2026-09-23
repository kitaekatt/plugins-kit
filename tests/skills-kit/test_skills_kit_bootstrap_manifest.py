"""Manifest-shape test for plugins/skills-kit/bootstrap.json.

Guards the quota-resilient-dispatch migration step 1 (Decision 2 in
docs/planning/quota-resilient-dispatch/declaration-format-design.md):
skills-kit's own code will call bootstrap_lib.model_declaration for
declaration-shape validation, so bootstrap_lib must be a declared
shared_lib_imports edge before any import lands. skills-kit declared no
shared_lib_imports at all before this change.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "plugins" / "skills-kit" / "bootstrap.json"


def test_skills_kit_bootstrap_lib_is_a_declared_shared_lib_import() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "bootstrap_lib" in manifest.get("shared_lib_imports", []), (
        "skills-kit/bootstrap.json must declare bootstrap_lib in "
        "shared_lib_imports so bootstrap_lib.model_declaration is importable"
    )
