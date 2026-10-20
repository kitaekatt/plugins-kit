# Shipped legacy SKILL.md standards

The legacy per-skill-type heuristic criteria skills-kit ships for a SKILL.md
that declares a `skill-type` and has not moved to the YAML contract. This file
is layer 0 of the standards configuration, beside `SKILL-standards.md`:
`standards_resolve.resolve()` reads it before the user and project layers, and
every SKILL.md audit applies the criteria below. It must exist; when it is
missing, the audit stops with an error rather than skipping these rows.

The block below is the single source of truth for each criterion's id,
statement and severity. Each criterion is `mechanical`: the check itself is a
registered evaluator in `audit.py` (`MECHANICAL_EVALUATORS`), keyed by the
criterion `id`. None declares a threshold; `wrapper-max-lines` reads the built-in
`wrapper_max_lines` limit (default 40), which a user tunes with
`thresholds: {wrapper_max_lines: <positive int>}`.

Every criterion here is scoped by the evaluator's `applies` gate, not by this
file, and the scope has three parts:

- The SKILL.md has no YAML contract block. A contract block, parsed or merely
  detected on a bare interpreter, supersedes these heuristics, so a skill on
  the contract gets none of these rows.
- The SKILL.md's `skill-type` frontmatter names the skill type the criterion
  covers (its statement says which). An untagged skill, or one tagged with a
  type that has no criteria here, gets none.
- The evaluator reads the body with fenced code blocks removed, so a marker
  that appears only inside a fenced example does not count.

These rows report in the `type_specific` list, after the YAML-contract rows and
before the mixed-type row. They are the only SKILL.md rows that keep their real
verdict on a bare interpreter without pyyaml: a deferred row would replace a
deterministic heading or marker test with a request to install a parser.
Because the severity below cannot be read there, each evaluator carries a copy
(`MechanicalEvaluator.bare_severity`) that a test pins equal to this file.

A user or project layer tunes these criteria the same way as any other optional
opinion: `rules: {<id>: off}` in a layer's `config.yaml` removes that
criterion's row from the audit. An authored standards file may not declare a
criterion with one of these ids: a criterion id repeated anywhere in the
resolved layers is an error.

```yaml
standards_set:
  _schema_version: "1"
  identity: Shipped legacy per-skill-type heuristic standards for a SKILL.md that declares a skill-type and has no YAML contract.
  applies_to: skill_md
  criteria:
    - id: ref-example-block
      statement: A reference-skill body has an "Example" heading.
      severity: judgment
      keywords: [reference-skill, example heading, example block, legacy heuristics]
      enforcement: mechanical
    - id: ref-gotcha-block
      statement: A reference-skill body has a "Gotcha" heading.
      severity: judgment
      keywords: [reference-skill, gotcha heading, gotcha block, legacy heuristics]
      enforcement: mechanical
    - id: ref-prohibited-discipline
      statement: A reference-skill body omits discipline content (rule+counter, RED/GREEN/REFACTOR).
      severity: fail
      keywords: [reference-skill, prohibited discipline content, rule and counter, legacy heuristics]
      enforcement: mechanical
    - id: ref-prohibited-checklist
      statement: A reference-skill body omits a workflow tickbox checklist.
      severity: fail
      keywords: [reference-skill, prohibited checklist, tickbox list, legacy heuristics]
      enforcement: mechanical
    - id: pattern-recognition-block
      statement: A pattern-skill body carries a recognition-criteria marker.
      severity: judgment
      keywords: [pattern-skill, recognition criteria, recognize, legacy heuristics]
      enforcement: mechanical
    - id: pattern-counter-example
      statement: A pattern-skill body carries a counter-example or "do NOT apply" marker.
      severity: judgment
      keywords: [pattern-skill, counter-example, do not apply, legacy heuristics]
      enforcement: mechanical
    - id: pattern-prohibited-bundle
      statement: A pattern-skill ships no `scripts/` or `bin/` utility bundle.
      severity: fail
      keywords: [pattern-skill, prohibited utility bundle, scripts directory, legacy heuristics]
      enforcement: mechanical
    - id: pattern-prohibited-checklist
      statement: A pattern-skill body omits a workflow tickbox checklist.
      severity: fail
      keywords: [pattern-skill, prohibited checklist, tickbox list, legacy heuristics]
      enforcement: mechanical
    - id: pattern-prohibited-rule-counter
      statement: A pattern-skill body omits rule+counter (excuse-to-reality) pairs.
      severity: fail
      keywords: [pattern-skill, prohibited rule and counter, excuse and reality, legacy heuristics]
      enforcement: mechanical
    - id: technique-ordered-steps
      statement: A technique-skill body has an ordered-step sequence.
      severity: fail
      keywords: [technique-skill, ordered steps, numbered list, legacy heuristics]
      enforcement: mechanical
    - id: step-tracking
      statement: A technique-skill with more than three steps carries a tickbox checklist or a step-tracker invocation.
      severity: fail
      keywords: [technique-skill, step tracking, tickbox checklist, step tracker, legacy heuristics]
      enforcement: mechanical
    - id: technique-prohibited-pressure-test
      statement: A technique-skill body omits adversarial RED/GREEN/REFACTOR pressure testing.
      severity: fail
      keywords: [technique-skill, prohibited pressure testing, red green refactor, legacy heuristics]
      enforcement: mechanical
    - id: wrapper-ordered-step
      statement: A technique-wrapper body has an ordered-step sequence (its one numbered step).
      severity: fail
      keywords: [technique-wrapper, ordered step, numbered list, legacy heuristics]
      enforcement: mechanical
    - id: wrapper-procedure-cited
      statement: A technique-wrapper body cites at least one references/ document, and every document it cites exists.
      severity: fail
      keywords: [technique-wrapper, procedure document, references citation, load-graph edge, legacy heuristics]
      enforcement: mechanical
    - id: wrapper-max-lines
      statement: A technique-wrapper body stays within `wrapper_max_lines` lines.
      severity: fail
      keywords: [technique-wrapper, stub length, wrapper_max_lines, repeat invocation, legacy heuristics]
      enforcement: mechanical
    - id: discipline-rule-counter
      statement: A discipline-skill body carries at least one rule+counter pair.
      severity: fail
      keywords: [discipline-skill, rule and counter, excuse and reality, legacy heuristics]
      enforcement: mechanical
    - id: discipline-red-flags
      statement: A discipline-skill body carries a "Red flags" list.
      severity: fail
      keywords: [discipline-skill, red flags, rationalization list, legacy heuristics]
      enforcement: mechanical
    - id: discipline-pressure-test
      statement: A discipline-skill applies adversarial pressure testing to its own rules.
      severity: fail
      keywords: [discipline-skill, pressure testing, red green refactor, legacy heuristics]
      enforcement: mechanical
    - id: domain-identity-sentence
      statement: A domain-skill body carries a single-sentence identity after the H1.
      severity: judgment
      keywords: [domain-skill, identity sentence, first paragraph, legacy heuristics]
      enforcement: mechanical
    - id: domain-companion-declaration
      statement: A domain-skill declares its companions (siblings, or an explicit "no sibling").
      severity: fail
      keywords: [domain-skill, companion declaration, sibling domains, legacy heuristics]
      enforcement: mechanical
    - id: domain-orientation
      statement: A domain-skill body carries orientation content (at least one H2 beyond the index).
      severity: fail
      keywords: [domain-skill, orientation content, h2 sections, legacy heuristics]
      enforcement: mechanical
    - id: domain-reference-index
      statement: A domain-skill body carries a Conditional-Loading reference index.
      severity: fail
      keywords: [domain-skill, reference index, conditional loading, legacy heuristics]
      enforcement: mechanical
    - id: domain-prohibited-index-only
      statement: A domain-skill is not an index-only stub (an index with no orientation content).
      severity: fail
      keywords: [domain-skill, prohibited index only, index stub, legacy heuristics]
      enforcement: mechanical
```
