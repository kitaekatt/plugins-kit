"""rule_catalog -- SSOT for the bucket each audit rule id belongs to.

Every rule id emitted by audit.py (the ``rule`` field on a CheckResult) is
mapped here to exactly one of three buckets:

- ``architectural`` -- structural-contract checks (the YAML type contract,
  mixed-type / cross-block drift). These are the spine of the framework and are
  never disableable; the standards resolver rejects any attempt to switch one
  off by id.
- ``optional`` -- opinion checks (description hygiene, size signals, record
  floors, the legacy per-type heuristic rows). These carry stable ids so a user
  can disable or tune them via config.
- ``inoffensive`` -- mechanical integrity checks (frontmatter/name presence and
  charset, reference reachability and citation resolution, asset-path
  resolution). Disabling one could never make a correct document, so they get
  no knob.

This module is the single source consumed by the standards resolver (for the
reject-architectural check) and by the M4 configuration docs. Keep it in sync
with the ``rule=`` assignments in audit.py.
"""

from __future__ import annotations

# rule id -> bucket. Every id assigned in audit.py appears exactly once.
BUCKETS: dict[str, str] = {
    # -- inoffensive: mechanical integrity --------------------------------
    "frontmatter-present": "inoffensive",
    "name-present": "inoffensive",
    "name-length": "inoffensive",
    "name-charset": "inoffensive",
    "name-reserved": "inoffensive",
    "desc-present": "inoffensive",
    "refs-one-hop-deep": "inoffensive",
    "refs-cited-exist": "inoffensive",
    "asset-paths-resolve": "inoffensive",
    "refs-reachable": "inoffensive",
    # -- architectural: structural contract -------------------------------
    "yaml-contract": "architectural",
    "mixed-type": "architectural",
    "cross-block-drift": "architectural",
    # -- optional: description hygiene ------------------------------------
    "desc-160-char": "optional",
    "desc-directive-form": "optional",
    "desc-exclusion-clause": "optional",
    "skill-type-tag": "optional",
    "skill-type-valid": "optional",
    # -- optional: thresholds / signals -----------------------------------
    "body-line-count": "optional",
    "body-token-count": "optional",
    "body-size-signal": "optional",
    # -- optional: record floors ------------------------------------------
    "step-tracking": "optional",
    "facts-floor": "optional",
    "facts-gotcha": "optional",
    "facts-example": "optional",
    "caution-floor": "optional",
    "claude-md-record-floor": "optional",
    # -- optional: legacy per-type heuristic rows -------------------------
    "ref-example-block": "optional",
    "ref-gotcha-block": "optional",
    "ref-prohibited-discipline": "optional",
    "ref-prohibited-checklist": "optional",
    "pattern-recognition-block": "optional",
    "pattern-counter-example": "optional",
    "pattern-prohibited-bundle": "optional",
    "pattern-prohibited-checklist": "optional",
    "pattern-prohibited-rule-counter": "optional",
    "technique-ordered-steps": "optional",
    "technique-prohibited-pressure-test": "optional",
    "discipline-rule-counter": "optional",
    "discipline-red-flags": "optional",
    "discipline-pressure-test": "optional",
    "domain-identity-sentence": "optional",
    "domain-companion-declaration": "optional",
    "domain-orientation": "optional",
    "domain-reference-index": "optional",
    "domain-prohibited-index-only": "optional",
}

BUCKET_NAMES = ("architectural", "optional", "inoffensive")


def is_architectural(rule_id: str) -> bool:
    """True when the rule is a structural-contract check (never disableable)."""
    return BUCKETS.get(rule_id) == "architectural"


def optional_rule_ids() -> list[str]:
    """The ids a user may disable or tune, sorted for stable output."""
    return sorted(rid for rid, bucket in BUCKETS.items() if bucket == "optional")
