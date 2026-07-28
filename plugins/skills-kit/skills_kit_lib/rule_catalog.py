"""rule_catalog -- SSOT for every audit rule id: its bucket, sub-group, and
user-facing description.

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
reject-architectural check) and rendered into the configuring-standards.md
rule-id catalog by scripts/gen_standards_doc.py -- edit HERE, then regenerate
(tests/skills-kit/test_standards_doc_drift.py fails on a stale doc). Keep it
in sync with the ``rule=`` assignments in audit.py (pinned by
tests/skills-kit/test_rule_ids.py).
"""

from __future__ import annotations

# rule id -> (bucket, group, description). Every id assigned in audit.py
# appears exactly once. `group` is the intra-bucket family (preserved here so
# regeneration can round-trip it); `description` is the user-facing one-liner
# rendered into the configuring-standards.md catalog tables.
RULES: dict[str, tuple[str, str, str]] = {
    # -- inoffensive: mechanical integrity --------------------------------
    "frontmatter-present": ("inoffensive", "integrity", "A leading frontmatter block exists."),
    "name-present": ("inoffensive", "integrity", "`frontmatter.name` is present."),
    "name-length": ("inoffensive", "integrity", "`frontmatter.name` is at most `name_max_chars` characters."),
    "name-charset": ("inoffensive", "integrity", "`frontmatter.name` uses the allowed charset."),
    "name-reserved": ("inoffensive", "integrity", "`frontmatter.name` is not a reserved name."),
    "desc-present": ("inoffensive", "integrity", "`frontmatter.description` is present."),
    "refs-one-hop-deep": ("inoffensive", "integrity", "`references/` is one hop deep (no nested references directories)."),
    "refs-cited-exist": ("inoffensive", "integrity", "Every reference cited in the body resolves to a file."),
    "asset-paths-resolve": ("inoffensive", "integrity", "Every declared asset-dependency and `tools[].tests` path resolves."),
    "refs-reachable": ("inoffensive", "integrity", "Every file under `references/` is reachable from SKILL.md."),
    # -- architectural: structural contract -------------------------------
    "yaml-contract": ("architectural", "contract", "The YAML type-contract block is recognized and validates against its schema (root key found, required keys present, rules satisfied)."),
    "mixed-type": ("architectural", "contract", "A SKILL.md declares exactly one skill-type root -- no drift across two type contracts (consumes `mixed_min_score`)."),
    "cross-block-drift": ("architectural", "contract", "Multiple YAML blocks in one document do not disagree about the document's type."),
    # -- optional: description hygiene ------------------------------------
    "desc-160-char": ("optional", "description-hygiene", "The description frontmatter field is at most `desc_max_chars` characters."),
    "desc-directive-form": ("optional", "description-hygiene", 'The description opens with "Use when..." or "Invoke when...".'),
    "desc-exclusion-clause": ("optional", "description-hygiene", 'The description carries a "Do NOT use for..." exclusion clause.'),
    "skill-type-tag": ("optional", "description-hygiene", "A `skill-type` advisory tag is present in frontmatter (else the agent infers the type)."),
    "skill-type-valid": ("optional", "description-hygiene", "The `skill-type` value is one of the canonical skill types."),
    # -- optional: thresholds / signals -----------------------------------
    "body-line-count": ("optional", "thresholds-signals", "Reports the SKILL.md body line count (informational count row)."),
    "body-token-count": ("optional", "thresholds-signals", "Reports the approximate SKILL.md body token count (informational count row)."),
    "body-size-signal": ("optional", "thresholds-signals", "An over-threshold body with no `references/` directory raises a progressive-disclosure signal (consumes `body_max_lines`, `body_max_tokens`)."),
    # -- optional: record floors ------------------------------------------
    "step-tracking": ("optional", "record-floors", "A technique-skill with more than three steps carries a tickbox checklist or a step-tracker invocation."),
    "facts-floor": ("optional", "record-floors", "A reference-skill declares at least one fact (nested in `reference_skill:` or as a top-level `facts:` unit)."),
    "facts-gotcha": ("optional", "record-floors", "At least one fact carries a `gotchas` list."),
    "facts-example": ("optional", "record-floors", "At least one fact carries an `example` block."),
    "caution-floor": ("optional", "record-floors", "A technique-skill carries at least one per-technique gotcha or at least one `anti_patterns` record."),
    "claude-md-record-floor": ("optional", "record-floors", "A CLAUDE.md declares at least one record across the `insights` / `conventions` union."),
    # -- optional: legacy per-type heuristic rows -------------------------
    "ref-example-block": ("optional", "legacy-heuristics", 'A reference-skill body has an "Example" heading.'),
    "ref-gotcha-block": ("optional", "legacy-heuristics", 'A reference-skill body has a "Gotcha" heading.'),
    "ref-prohibited-discipline": ("optional", "legacy-heuristics", "A reference-skill body omits discipline content (rule+counter, RED/GREEN/REFACTOR)."),
    "ref-prohibited-checklist": ("optional", "legacy-heuristics", "A reference-skill body omits a workflow tickbox checklist."),
    "pattern-recognition-block": ("optional", "legacy-heuristics", "A pattern-skill body carries a recognition-criteria marker."),
    "pattern-counter-example": ("optional", "legacy-heuristics", 'A pattern-skill body carries a counter-example or "do NOT apply" marker.'),
    "pattern-prohibited-bundle": ("optional", "legacy-heuristics", "A pattern-skill ships no `scripts/` or `bin/` utility bundle."),
    "pattern-prohibited-checklist": ("optional", "legacy-heuristics", "A pattern-skill body omits a workflow tickbox checklist."),
    "pattern-prohibited-rule-counter": ("optional", "legacy-heuristics", "A pattern-skill body omits rule+counter (excuse-to-reality) pairs."),
    "technique-ordered-steps": ("optional", "legacy-heuristics", "A technique-skill body has an ordered-step sequence."),
    "technique-prohibited-pressure-test": ("optional", "legacy-heuristics", "A technique-skill body omits adversarial RED/GREEN/REFACTOR pressure testing."),
    "discipline-rule-counter": ("optional", "legacy-heuristics", "A discipline-skill body carries at least one rule+counter pair."),
    "discipline-red-flags": ("optional", "legacy-heuristics", 'A discipline-skill body carries a "Red flags" list.'),
    "discipline-pressure-test": ("optional", "legacy-heuristics", "A discipline-skill applies adversarial pressure testing to its own rules."),
    "domain-identity-sentence": ("optional", "legacy-heuristics", "A domain-skill body carries a single-sentence identity after the H1."),
    "domain-companion-declaration": ("optional", "legacy-heuristics", 'A domain-skill declares its companions (siblings, or an explicit "no sibling").'),
    "domain-orientation": ("optional", "legacy-heuristics", "A domain-skill body carries orientation content (at least one H2 beyond the index)."),
    "domain-reference-index": ("optional", "legacy-heuristics", "A domain-skill body carries a Conditional-Loading reference index."),
    "domain-prohibited-index-only": ("optional", "legacy-heuristics", "A domain-skill is not an index-only stub (an index with no orientation content)."),
}

# rule id -> bucket, derived. Public API preserved for existing consumers
# (standards_resolve, tests); RULES is the richer surface behind it.
BUCKETS: dict[str, str] = {rid: rec[0] for rid, rec in RULES.items()}

BUCKET_NAMES = ("architectural", "optional", "inoffensive")

# threshold name -> the rule id(s) that consume it. Values (defaults) live in
# audit.py THRESHOLDS; this mapping is the doc-facing "Consumed by" column.
THRESHOLD_CONSUMERS: dict[str, str] = {
    "name_max_chars": "`name-length`",
    "desc_max_chars": "`desc-160-char`",
    "body_max_lines": "`body-size-signal`",
    "body_max_tokens": "`body-size-signal`",
    "mixed_min_score": "`mixed-type`",
}


def is_architectural(rule_id: str) -> bool:
    """True when the rule is a structural-contract check (never disableable)."""
    return BUCKETS.get(rule_id) == "architectural"


def optional_rule_ids() -> list[str]:
    """The ids a user may disable or tune, sorted for stable output."""
    return sorted(rid for rid, bucket in BUCKETS.items() if bucket == "optional")


def description(rule_id: str) -> str:
    """The user-facing one-line description of a rule."""
    return RULES[rule_id][2]
