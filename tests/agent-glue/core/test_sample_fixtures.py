"""Sample fixtures: round-trip identity + pre-commit acceptance + broken-fixture rejection.

These fixtures exercise every shipped subsystem's entity types end-to-end.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from agent_glue_lib.core import (
    dump_instance,
    load_catalog,
    load_instances,
    validate_all,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "agent-glue"
FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE = FIXTURES_ROOT / "sample"
BROKEN = FIXTURES_ROOT / "broken"
HOOK = PLUGIN_ROOT / "scripts" / "precommit_consistency.py"

SUBSYSTEMS = ("core", "claude-work-queue", "work-system", "graph-system")


def _kit():
    component_dirs = [PLUGIN_ROOT / s / "components" for s in SUBSYSTEMS]
    entity_dirs = [PLUGIN_ROOT / s / "entities" for s in SUBSYSTEMS]
    return load_catalog(component_dirs, entity_dirs)


def test_sample_fixtures_load_cleanly():
    instances = load_instances(SAMPLE)
    # Graph + 2 Nodes + Edge + Cohort + Fixture + ExpectedOutcome = 7
    assert len(instances) == 7
    types = sorted(i.type for i in instances)
    assert types == ["Cohort", "Edge", "ExpectedOutcome", "Fixture", "Graph", "Node", "Node"]


def test_sample_fixtures_validate_against_shipped_kit():
    catalog = _kit()
    instances = load_instances(SAMPLE)
    assert validate_all(catalog, instances) == []


def test_sample_fixtures_round_trip_identity():
    instances = load_instances(SAMPLE)
    for inst in instances:
        original = yaml.safe_load(Path(inst.source_path).read_text(encoding="utf-8"))
        dumped = dump_instance(inst)
        reparsed = yaml.safe_load(dumped)
        assert reparsed == original, f"round-trip mismatch for {inst.source_path}"


def test_hook_accepts_sample_fixtures():
    result = subprocess.run(
        [sys.executable, str(HOOK), "--instances", str(SAMPLE)],
        capture_output=True,
        text=True,
        cwd=PLUGIN_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "7 instances" in result.stdout


def test_hook_rejects_broken_fixture():
    result = subprocess.run(
        [sys.executable, str(HOOK), "--instances", str(BROKEN)],
        capture_output=True,
        text=True,
        cwd=PLUGIN_ROOT,
    )
    assert result.returncode == 1
    assert "missing required components" in result.stderr
    assert "topology" in result.stderr
