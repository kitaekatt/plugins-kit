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
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "plugins" / "skills-kit" / "bootstrap.json"


def test_skills_kit_bootstrap_lib_is_a_declared_shared_lib_import() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "bootstrap_lib" in manifest.get("shared_lib_imports", []), (
        "skills-kit/bootstrap.json must declare bootstrap_lib in "
        "shared_lib_imports so bootstrap_lib.model_declaration is importable"
    )


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_requires_bootstrap_covers_every_floor_skills_kit_needs() -> None:
    """The floor is the HIGHEST of what skills-kit needs: the bootstrap_lib CALL
    shapes it makes (skills_kit_lib.material binds bootstrap_lib.skill_material,
    SKILL_MATERIAL_BOOTSTRAP) and the katsoft-llm-kit package handling every
    consumer of that package requires (lane_models.LLM_KIT_PACKAGE_BOOTSTRAP)."""
    from skills_kit_lib import lane_models, material

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    floors = [material.SKILL_MATERIAL_BOOTSTRAP, lane_models.LLM_KIT_PACKAGE_BOOTSTRAP]
    assert manifest.get("requires_bootstrap") == max(floors, key=_version)


def test_bootstrap_floors_are_pinned_literally() -> None:
    """Each floor is asserted as a literal, apart from the derived check above,
    so moving a constant and the manifest back together cannot stay green.
    Red: set requires_bootstrap back to 0.138.0 (the skill-material floor alone)."""
    from skills_kit_lib import lane_models, material

    assert material.SKILL_MATERIAL_BOOTSTRAP == "0.138.0"
    assert lane_models.LLM_KIT_PACKAGE_BOOTSTRAP == "0.160.0"
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest.get("requires_bootstrap") == "0.160.0"


def _imports_llm_kit(path: Path) -> bool:
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "llm_kit" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "llm_kit":
            return True
    return False


def test_llm_kit_arrives_as_the_declared_package_never_as_a_shared_lib_link() -> None:
    """The md-domain lane route (lane_models.lane_route, llm-kit describe) and the
    md-lane bridge (lane_bridge.run_lane) import llm_kit, so the plugin venv needs
    it, as the katsoft-llm-kit package whose describe() applies the user's model
    overrides. Red: put `llm_kit` back in `shared_lib_imports`, import llm_kit
    from another module, drop it from check_imports, or declare a floor below
    lane_models.LLM_KIT_OVERRIDES_VERSION."""
    import tomllib

    from skills_kit_lib.lane_models import LLM_KIT_OVERRIDES_VERSION

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "llm_kit" not in manifest.get("shared_lib_imports", [])
    assert "llm_kit" in manifest["venv"]["check_imports"]
    importers = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "plugins" / "skills-kit").rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts and _imports_llm_kit(path)
    )
    assert importers == [
        "plugins/skills-kit/skills_kit_lib/lane_bridge.py",
        "plugins/skills-kit/skills_kit_lib/lane_models.py",
    ]
    pyproject = tomllib.loads((MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8"))
    floors = [
        re.search(r">=\s*([0-9.]+)", dep).group(1)
        for dep in pyproject["project"]["dependencies"]
        if re.match(r"katsoft-llm-kit\b", dep) and re.search(r">=\s*([0-9.]+)", dep)
    ]
    assert floors, "pyproject.toml declares no katsoft-llm-kit requirement with a >= floor"
    as_tuple = lambda v: tuple(int(x) for x in v.split("."))  # noqa: E731
    assert as_tuple(floors[0]) >= as_tuple(LLM_KIT_OVERRIDES_VERSION), floors


def test_katsoft_contracts_is_declared_because_bootstrap_lib_is_linked() -> None:
    """Red: drop the requirement from pyproject.toml, or widen its range."""
    import tomllib

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert "bootstrap_lib" in manifest["shared_lib_imports"]
    pyproject = tomllib.loads((MANIFEST.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert "katsoft-contracts>=1.0,<2" in pyproject["project"]["dependencies"]
