"""Integrity guard for audit-framework.yaml -- the audit-kind registry.

The registry is agent-read prose-adjacent data with no runtime loader, so
nothing used to catch a dead scaffolding path, a rule id bound to a criteria
entry that does not exist, or a future_rules entry that silently became (or
claimed to be) implemented. This suite makes those invariants mechanical:

1. Every repo-relative path named in a registry entry resolves on disk.
2. Every id in rules_per_composition exists in the owning SKILL.md's
   audit_skill.criteria block (bound = implemented).
3. No future_rules id appears in any rules_per_composition or in any member
   SKILL.md criteria block (future = not implemented; shipping one means
   moving it out of future_rules).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from skills_kit_lib.document_walker import collect_yaml_units

REPO_ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_YAML = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
    / "references" / "audit-framework.yaml"
)

REGISTRY = yaml.safe_load(FRAMEWORK_YAML.read_text(encoding="utf-8"))

# Registry fields whose value opens with a repo-relative path (annotations may
# follow in parentheses or after whitespace).
_PATH_FIELDS = (
    "discovery",
    "corpus_discovery",
    "evaluator_scaffolding",
    "taxonomy_doc",
    "rules_owned_in",
    "taxonomy_in_skill",
    "generator_scaffolding",
)


def _leading_path(value: str) -> str | None:
    token = value.split()[0] if value.split() else ""
    if token.startswith("plugins/"):
        return token
    return None


def _registry_paths() -> list[tuple[str, str, str]]:
    out = []
    for section in ("audit_kinds", "viewer_kinds"):
        for entry in REGISTRY.get(section) or []:
            for field in _PATH_FIELDS:
                value = entry.get(field)
                if isinstance(value, str):
                    path = _leading_path(value)
                    if path:
                        out.append((entry["id"], field, path))
    return out


def _criteria_ids(owner_doc: Path) -> set[str]:
    """Extract the implemented rule ids from the doc rules_owned_in names.

    Pre-fold this was the member SKILL.md's audit_skill.criteria block; the
    md-domain restructure moved the criteria into each artifact's standards
    doc, where they appear either as table rows whose first column is the
    backticked id, or (references-standards.md) as prose "Rule id `x`"
    statements. A SKILL.md owner still parses via its audit_skill unit.
    """
    text = owner_doc.read_text(encoding="utf-8")
    if owner_doc.name == "SKILL.md":
        units, _, parse_error = collect_yaml_units(text)
        assert parse_error is None, f"{owner_doc}: yaml parse error {parse_error}"
        for root, data in units:
            if root == "audit_skill":
                block = data.get(root, data)
                return {
                    c["id"] for c in block.get("criteria") or [] if isinstance(c, dict)
                }
        raise AssertionError(f"{owner_doc}: no audit_skill unit found")
    ids = set(re.findall(r"^\| `([a-z][a-z0-9_]+)`", text, flags=re.MULTILINE))
    ids |= set(re.findall(r"[Rr]ule id\s+`([a-z][a-z0-9_]+)`", text))
    assert ids, f"{owner_doc}: no criteria ids found (table rows or 'Rule id' prose)"
    return ids


def _owning_skill_md(entry: dict) -> Path:
    path = _leading_path(entry["rules_owned_in"])
    assert path, f"{entry['id']}: rules_owned_in has no leading path"
    return REPO_ROOT / path


@pytest.mark.parametrize(
    "entry_id,field,path",
    _registry_paths(),
    ids=[f"{e}:{f}" for e, f, _ in _registry_paths()],
)
def test_registry_path_resolves(entry_id, field, path):
    assert (REPO_ROOT / path).exists(), (
        f"{entry_id}.{field} names {path}, which does not exist -- "
        "a dead registry path (fix the entry or restore the file)"
    )


@pytest.mark.parametrize(
    "entry", REGISTRY["audit_kinds"], ids=[e["id"] for e in REGISTRY["audit_kinds"]]
)
def test_bound_rules_are_implemented(entry):
    owned = _criteria_ids(_owning_skill_md(entry))
    bound = {
        rid
        for ids in (entry.get("rules_per_composition") or {}).values()
        for rid in ids
    }
    missing = sorted(bound - owned)
    assert not missing, (
        f"{entry['id']}: rules_per_composition binds ids with no criteria entry "
        f"in {entry['rules_owned_in']}: {missing} -- bound means implemented; "
        "planned rules belong in future_rules"
    )


def test_future_rules_are_not_bound_anywhere():
    future_ids = {r["id"] for r in REGISTRY.get("future_rules") or []}
    for entry in REGISTRY["audit_kinds"]:
        bound = {
            rid
            for ids in (entry.get("rules_per_composition") or {}).values()
            for rid in ids
        }
        overlap = sorted(future_ids & bound)
        assert not overlap, (
            f"{entry['id']}: binds future_rules ids {overlap} -- a shipped rule "
            "must be removed from future_rules; a planned one must not be bound"
        )


def test_future_rules_are_not_implemented():
    future = REGISTRY.get("future_rules") or []
    kinds_by_id = {e["id"]: e for e in REGISTRY["audit_kinds"]}
    for rule in future:
        kind = kinds_by_id.get(rule["audit_kind"])
        assert kind is not None, (
            f"future_rules.{rule['id']}: audit_kind {rule['audit_kind']!r} "
            "is not a registered audit-kind"
        )
        owned = _criteria_ids(_owning_skill_md(kind))
        assert rule["id"] not in owned, (
            f"future_rules.{rule['id']}: a criteria entry EXISTS in "
            f"{kind['rules_owned_in']} -- the rule appears implemented; move it "
            "out of future_rules and bind it in rules_per_composition"
        )


def test_registry_parses_and_has_expected_sections():
    for section in ("primitives", "compositions", "audit_kinds"):
        assert REGISTRY.get(section), f"audit-framework.yaml missing {section}"
