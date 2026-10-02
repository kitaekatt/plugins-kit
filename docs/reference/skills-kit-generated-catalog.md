# skills-kit generated rule-id catalog

The rule-id catalog and threshold table in
`plugins/skills-kit/skills/md-domain/references/configuring-standards.md` are
generated. The generated region is the block between the `BEGIN GENERATED:
rule-catalog` and `END GENERATED` markers.

## Sources

- Rule ids, buckets, and descriptions: `skills_kit_lib/rule_catalog.py`
  (`RULES`).
- Threshold defaults: `skills_kit_lib/audit.py` (`THRESHOLDS`).

## Regenerating

Edit those sources, then run `scripts/gen_standards_doc.py` from
`plugins/skills-kit/`. Never hand-edit the generated region. A stale region is
caught by the drift check in `tests/skills-kit/test_standards_doc_drift.py`.

The resolver's reject-un-tunable-rule check reads `rule_catalog.py` directly,
so the doc and the enforcement cannot disagree.
