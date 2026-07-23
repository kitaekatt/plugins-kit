"""Tests for the M1 rule-id + threshold-constant surface (audit.py, rule_catalog.py).

Pins the invariants the standards resolver (M2+) and the config docs (M4) rely
on: every emitted CheckResult carries a stable non-empty ``rule``; every rule id
that shows up in audit output is catalogued in rule_catalog.BUCKETS; the
thresholds are centralized; and every bucket value is one of the three allowed
strings.
"""

import yaml

from skills_kit_lib import rule_catalog
from skills_kit_lib.audit import THRESHOLDS, audit


# The audit report shape: four surfaces carrying CheckResult dicts. universal,
# yaml_contract and type_specific are lists; mixed_type is a single dict.
def _all_rows(report: dict) -> list[dict]:
    rows: list[dict] = []
    for key in ("universal", "yaml_contract", "type_specific"):
        rows.extend(report.get(key, []))
    if report.get("mixed_type"):
        rows.append(report["mixed_type"])
    return rows


def _write_skill(tmp_path, fixture: dict, name: str = "example-skill") -> "object":
    """Materialize a SKILL.md from a minimal_* fixture dict (its single root key
    names the skill type). Reuses the conftest floors as authored contract."""
    root = next(iter(fixture))
    skill_type = root.replace("_", "-")
    skill_dir = tmp_path / name
    skill_dir.mkdir()
    body = (
        f"---\n"
        f"name: {name}\n"
        f"description: Use when doing X. Do NOT use for Y.\n"
        f"skill-type: {skill_type}\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"Orientation paragraph.\n\n"
        f"```yaml\n{yaml.safe_dump(fixture, sort_keys=False)}```\n"
    )
    path = skill_dir / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_every_row_carries_a_rule(tmp_path, minimal_reference_skill):
    report = audit(_write_skill(tmp_path, minimal_reference_skill))
    rows = _all_rows(report)
    assert rows, "audit produced no rows"
    for r in rows:
        assert r.get("rule"), f"row missing rule id: {r}"


def test_every_emitted_rule_is_catalogued(tmp_path, minimal_reference_skill, minimal_technique_skill):
    seen: set[str] = set()
    for fixture in (minimal_reference_skill, minimal_technique_skill):
        report = audit(_write_skill(tmp_path, fixture, name=next(iter(fixture)).replace("_", "-")))
        for r in _all_rows(report):
            seen.add(r["rule"])
    assert seen, "no rule ids observed"
    uncatalogued = seen - set(rule_catalog.BUCKETS)
    assert not uncatalogued, f"rule ids missing from rule_catalog.BUCKETS: {sorted(uncatalogued)}"


def test_thresholds_have_expected_keys_and_values():
    assert THRESHOLDS == {
        "name_max_chars": 64,
        "desc_max_chars": 160,
        "body_max_lines": 500,
        "body_max_tokens": 3000,
        "mixed_min_score": 2,
    }


def test_bucket_values_are_only_the_three_allowed_strings():
    allowed = {"architectural", "optional", "inoffensive"}
    assert set(rule_catalog.BUCKETS.values()) <= allowed
    assert set(rule_catalog.BUCKET_NAMES) == allowed


def test_helpers_agree_with_catalog():
    assert rule_catalog.is_architectural("yaml-contract") is True
    assert rule_catalog.is_architectural("desc-160-char") is False
    assert rule_catalog.is_architectural("nonexistent-rule") is False
    optional = rule_catalog.optional_rule_ids()
    assert "desc-160-char" in optional
    assert all(rule_catalog.BUCKETS[rid] == "optional" for rid in optional)
