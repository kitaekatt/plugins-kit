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


def test_requires_bootstrap_covers_the_skill_material_call() -> None:
    """The floor is the HIGHEST bootstrap call shape skills-kit makes, not what
    happens to import: skills_kit_lib.material binds
    bootstrap_lib.skill_material's parse_frontmatter_strict,
    SkillSelection.from_json, materialize and SkillMaterialReport.to_json.
    SKILL_MATERIAL_BOOTSTRAP is skills-kit's only bootstrap version constant,
    so the manifest equals it."""
    from skills_kit_lib import material

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == material.SKILL_MATERIAL_BOOTSTRAP


def test_skill_material_floor_is_pinned_literally() -> None:
    """The skill-material floor is the literal 0.138.0, asserted for the
    constant and the manifest apart, so moving both back together cannot stay
    green (the test above derives its floor from the constant)."""
    from skills_kit_lib import material

    assert material.SKILL_MATERIAL_BOOTSTRAP == "0.138.0"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == "0.138.0"


def _imports_llm_kit(path: Path) -> bool:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "llm_kit" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "llm_kit":
            return True
    return False


def test_llm_kit_is_not_linked_because_no_skills_kit_python_imports_it() -> None:
    """The link was vestigial: skills-kit reaches llm-kit only through the `llm-kit` launcher on PATH and the
    job files it emits. Red: add `import llm_kit` to any skills-kit .py (the link is then needed, and it must
    arrive as a declared package), or put `llm_kit` back in `shared_lib_imports`."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "llm_kit" not in manifest.get("shared_lib_imports", [])
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "plugins" / "skills-kit").rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts and _imports_llm_kit(path)
    ]
    assert offenders == []


def test_katsoft_contracts_is_declared_because_bootstrap_lib_is_linked() -> None:
    """Red: drop the requirement from pyproject.toml, or widen its range."""
    import tomllib

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "bootstrap_lib" in manifest["shared_lib_imports"]
    pyproject = tomllib.loads((MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert "katsoft-contracts>=1.0,<2" in pyproject["project"]["dependencies"]
