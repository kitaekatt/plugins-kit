"""End-to-end check: the shipped agent-glue kit loads cleanly and validates."""

from __future__ import annotations

from pathlib import Path

from agent_glue_lib.core import load_catalog, validate_kit

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "agent-glue"
SUBSYSTEMS = ("core", "claude-work-queue", "work-system", "graph-system")


def _kit_dirs() -> tuple[list[Path], list[Path]]:
    component_dirs = [PLUGIN_ROOT / s / "components" for s in SUBSYSTEMS]
    entity_dirs = [PLUGIN_ROOT / s / "entities" for s in SUBSYSTEMS]
    return component_dirs, entity_dirs


def test_shipped_kit_loads():
    component_dirs, entity_dirs = _kit_dirs()
    catalog = load_catalog(component_dirs, entity_dirs)
    # 6 core + 23 graph + 21 work + 0 claude-work-queue = 50 components
    assert len(catalog.component_schemas) == 50
    # 6 graph + 4 work + 0 others = 10 entity types
    assert len(catalog.entity_types) == 10


def test_shipped_kit_is_consistent():
    component_dirs, entity_dirs = _kit_dirs()
    catalog = load_catalog(component_dirs, entity_dirs)
    assert validate_kit(catalog) == []


def test_shipped_kit_cross_cutting_components_present():
    component_dirs, entity_dirs = _kit_dirs()
    catalog = load_catalog(component_dirs, entity_dirs)
    for kind in ("Name", "Description", "Timestamps", "Errored", "Status", "SourceRunId"):
        assert kind in catalog.component_schemas, f"missing cross-cutting component {kind!r}"
