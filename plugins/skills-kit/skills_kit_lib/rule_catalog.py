"""rule_catalog -- SSOT for every audit rule id: its bucket, sub-group, and
user-facing description.

This is the registry of every CONFIGURABLE id, which is a superset of the ids
audit.py emits: besides every rule id audit.py puts in a CheckResult's ``rule``
field, it carries the judgment criteria an agent applies inside a workflow lane
(the coverage-criteria group). Each id is mapped to one of three buckets:

- ``architectural`` -- structural-contract checks (the YAML type contract,
  mixed-type / cross-block drift). These are the spine of the framework and are
  never disableable; the standards resolver rejects any attempt to switch one
  off by id.
- ``optional`` -- opinion checks. They carry stable ids so a user can disable
  or tune them via config. The catalog holds only the coverage lane's admission
  criteria; every other optional opinion (description hygiene, size signals,
  record floors, the legacy per-type heuristic rows) is a criterion of a
  shipped standards file.
- ``inoffensive`` -- mechanical integrity checks (frontmatter/name presence and
  charset, reference reachability and citation resolution, asset-path
  resolution). Disabling one could never make a correct document, so they get
  no knob.

Criteria declared by the shipped standards layer (defaults/standards/) are not
catalogued here: the shipped file is their source, and the resolver rejects a
criterion id that repeats a catalog id. Thirty-nine ids live there:

- SKILL-standards.md: desc-160-char, desc-directive-form,
  desc-exclusion-clause, skill-type-tag, skill-type-valid, body-line-count,
  body-token-count, body-size-signal, refs-one-hop-deep, facts-floor,
  facts-gotcha, facts-example and caution-floor.
- SKILL-legacy-standards.md: ref-example-block, ref-gotcha-block,
  ref-prohibited-discipline, ref-prohibited-checklist,
  pattern-recognition-block, pattern-counter-example,
  pattern-prohibited-bundle, pattern-prohibited-checklist,
  pattern-prohibited-rule-counter, technique-ordered-steps, step-tracking,
  technique-prohibited-pressure-test, wrapper-ordered-step,
  wrapper-procedure-cited, wrapper-max-lines, discipline-rule-counter,
  discipline-red-flags, discipline-pressure-test, domain-identity-sentence,
  domain-companion-declaration, domain-orientation, domain-reference-index and
  domain-prohibited-index-only.
- CLAUDE-md-standards.md: claude-md-import-size, claude-md-size-signal and
  claude-md-record-floor.

The eight coverage ids below stay registered here (settled decision D2): their
criteria are shipped data in skills/md-domain/references/standards/
coverage-standards.md, applied by an agent inside the coverage lane rather than
evaluated by audit.MECHANICAL_EVALUATORS, and the lane's disable check reads
this registry.

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
    "refs-cited-exist": ("inoffensive", "integrity", "Every reference cited in the body resolves to a file."),
    "asset-paths-resolve": ("inoffensive", "integrity", "Every declared asset-dependency and `tools[].tests` path resolves."),
    "refs-reachable": ("inoffensive", "integrity", "Every file under `references/` is reachable from SKILL.md."),
    "claude-md-links-resolve": ("inoffensive", "integrity", "Every relative markdown link target and `@import` path in a CLAUDE.md / AGENTS.md (outside code fences) resolves on disk; URLs, `#fragment`-only and absolute targets are skipped."),
    "claude-md-fences-closed": ("inoffensive", "integrity", "Every code fence opened in a CLAUDE.md / AGENTS.md is closed."),
    # -- architectural: structural contract -------------------------------
    "yaml-contract": ("architectural", "contract", "The YAML type-contract block is recognized and validates against its schema (root key found, required keys present, rules satisfied)."),
    "mixed-type": ("architectural", "contract", "A SKILL.md declares exactly one skill-type root -- no drift across two type contracts (consumes `mixed_min_score`)."),
    "cross-block-drift": ("architectural", "contract", "Multiple YAML blocks in one document do not disagree about the document's type."),
    # -- optional: shipped standards criteria ----------------------------
    # None of them is here. The thirty-six criteria listed in the module
    # docstring (defaults/standards/SKILL-standards.md,
    # SKILL-legacy-standards.md and CLAUDE-md-standards.md) are criteria of the
    # shipped standards layer, evaluated by audit.MECHANICAL_EVALUATORS. A
    # criterion id may not repeat a catalog id.
    # -- optional: coverage (analyze) admission criteria --------------------
    # The eight criteria declared in skills/md-domain/references/standards/
    # coverage-standards.md, registered here so each is disableable by id. They
    # are not emitted by audit.py: their `enforcement` is judgment, so they are
    # applied by an agent inside the coverage lane
    # (skills/md-domain/workflow/coverage-detect.js), which suppresses any
    # candidate whose criterion id the resolver reports as disabled. The
    # catalog is the registry of configurable ids, and audit.py's emitted ids
    # are a subset of it rather than the whole of it.
    "absent-fact-earns-ambient-cost": ("optional", "coverage-criteria", "CV-1: a coverage candidate is durable and consequential rather than cheaply recoverable from the local files."),
    "already-ambient-suppressed": ("optional", "coverage-criteria", "CV-2: a fact already carried by any CLAUDE.md in the directory's ancestor chain is not a candidate, a trigger site closer to the code included."),
    "fact-scoped-to-this-directory": ("optional", "coverage-criteria", "CV-3: a candidate is a fact about the assessed directory's own direct code, and its destination is that directory."),
    "candidate-tier-classified": ("optional", "coverage-criteria", "CV-4: every surviving candidate is classified finding-convertible or context-only, and the classification is reported."),
    "hazard-durability": ("optional", "coverage-criteria", "CV-5: an observed hazard earns ambient prose only when it is durable, or severe and not being fixed."),
    "loud-failure-excluded": ("optional", "coverage-criteria", "CV-6: a constraint that is documented, fails loudly at runtime, and is test-enforced is not a candidate."),
    "evidence-floor": ("optional", "coverage-criteria", "CV-7: every candidate cites a file and line observed in source; a convention needs two or more instances or one authoritative source."),
    "present-content-not-re-audited": ("optional", "coverage-criteria", "CV-8: coverage judges absent facts only and never evaluates content already present."),
}

# rule id -> bucket, derived. Public API preserved for existing consumers
# (standards_resolve, tests); RULES is the richer surface behind it.
BUCKETS: dict[str, str] = {rid: rec[0] for rid, rec in RULES.items()}

BUCKET_NAMES = ("architectural", "optional", "inoffensive")

# threshold name -> the rule id(s) that consume it. Values (defaults) live in
# audit.py THRESHOLDS; this mapping is the doc-facing "Consumed by" column. A
# threshold declared by a standards criterion is not listed: the criterion that
# declares it is its consumer. A shipped criterion's evaluator may consume a
# built-in threshold without declaring it (MechanicalEvaluator.
# reads_builtin_thresholds); it is listed here as a consumer.
THRESHOLD_CONSUMERS: dict[str, str] = {
    "name_max_chars": "`name-length`",
    "body_max_lines": "`body-size-signal`, `claude-md-size-signal`",
    "body_max_tokens": "`body-size-signal`, `claude-md-size-signal`",
    "mixed_min_score": "`mixed-type`",
    "wrapper_max_lines": "`wrapper-max-lines`",
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
