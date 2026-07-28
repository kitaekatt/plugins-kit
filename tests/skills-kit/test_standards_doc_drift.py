"""Drift guard: the generated rule-catalog section of configuring-standards.md
must match what gen_standards_doc.py renders from rule_catalog.RULES and
audit.THRESHOLDS. On failure, re-run the generator and review the diff.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from skills_kit_lib import rule_catalog
from skills_kit_lib.audit import THRESHOLDS

REPO_ROOT = Path(__file__).resolve().parents[2]
_GEN_PATH = REPO_ROOT / "plugins" / "skills-kit" / "scripts" / "gen_standards_doc.py"
_spec = importlib.util.spec_from_file_location("gen_standards_doc", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("gen_standards_doc", gen)
_spec.loader.exec_module(gen)


def test_generated_section_is_current():
    current = gen.DOC.read_text(encoding="utf-8")
    assert gen.BEGIN_MARK in current and gen.END_MARK in current, (
        "generation markers missing from configuring-standards.md"
    )
    assert gen.splice(current) == current, (
        "configuring-standards.md generated section is stale -- run "
        "plugins/skills-kit/scripts/gen_standards_doc.py and review the diff"
    )


def test_every_rule_has_description_and_group():
    for rid, (bucket, group, desc) in rule_catalog.RULES.items():
        assert bucket in rule_catalog.BUCKET_NAMES, f"{rid}: bad bucket {bucket!r}"
        assert group, f"{rid}: empty group"
        assert desc.strip(), f"{rid}: empty description"


def test_threshold_consumers_cover_thresholds_exactly():
    assert set(rule_catalog.THRESHOLD_CONSUMERS) == set(THRESHOLDS), (
        "THRESHOLD_CONSUMERS and audit.THRESHOLDS name different thresholds"
    )


def test_threshold_consumers_name_catalogued_rules():
    for name, consumers in rule_catalog.THRESHOLD_CONSUMERS.items():
        for rid in consumers.replace("`", "").split(","):
            assert rid.strip() in rule_catalog.RULES, (
                f"threshold {name}: consumer {rid.strip()!r} is not a catalogued rule id"
            )
