# Schema fixtures -- minimal-valid instance blocks

Minimal-valid instance blocks for the skill-type schemas and the portable
`references:` / `facts:` units, validated against the `skills_kit_lib` schemas.
This file is the md-domain tree's `owner_doc` target for those schemas: the
corpus check `check_schema_owner_docs_validate` parses every fenced block in
the `owner_doc` registered in `skills_kit_lib/schemas/` and validates it against
its schema on each run, so the fixtures cannot silently drift.

These are fixtures, not standards. The standards themselves -- the per-type
contracts, the schemas-as-floors stance, and the allocation rules -- live in
[`../standards/skill-standards.md`](../standards/skill-standards.md).

Each block is the smallest legal instance of its root key: required fields
only, list-length minimums met, no forbidden keys.

Portable unit -- `references:`:

```yaml
references:
  - id: skill-standards-doc
    path: skills/md-domain/references/standards/skill-standards.md
    keywords: [skill standards, type contracts, schema floor]
    summary: Artifact-keyed standards for SKILL.md, read by both md-domain lanes.
```

Portable unit -- `facts:`:

```yaml
facts:
  - id: schemas-are-floors
    summary: Per-type schemas declare the required minimum, not a ceiling.
    keywords: [schema floor, required minimum, extras allowed]
    detail: Authors may add load-bearing structured keys beyond what the schema enumerates; the schema enforces the floor.
```

Portable unit -- `asset_dependencies:`:

```yaml
asset_dependencies:
  - path: .claude/skills/project-design-domain/references/candidate-screening-funnel.md
    consumer: workflow.js
    purpose: runtime input -- the screening-funnel instrument the workflow lanes read
    invariant: workflow.js output records mirror the funnel's "Output schema (per game)" section
```

`reference_skill:`:

```yaml
reference_skill:
  identity: Reference skill that catalogs framework contracts for chat-time retrieval.
  scope:
    covers: [framework facts retrieval]
    excludes: [authoring procedure, audit enforcement]
  facts:
    - id: scope-block-shape
      summary: Every skill carries a covers/excludes scope at the root of its type unit.
      keywords: [scope block, covers list, excludes list]
      detail: Both covers and excludes are non-empty lists; the exclusion clause is materialized in YAML, not implied in prose.
```

`pattern_skill:`:

```yaml
pattern_skill:
  identity: Pattern skill that names the structured-extras-over-prose recognition.
  scope:
    covers: [recognition of where to extract structure from prose]
    excludes: [step-by-step authoring procedure]
  patterns:
    - id: structure-asserts
      name: Containment-asserts-membership
      keywords: [structured records, list of typed records, implicit assertion]
      problem: A bullet list of items asserts nothing about their kind; a list of typed records asserts every entry is that kind.
      mechanic: Promote a recurring structured extension to a first-class optional field with a known record shape.
      why: The record shape itself carries the cross-item invariant the prose otherwise has to state and the audit otherwise has to infer.
      apply_when:
        - signal: A recurring extension uses the same key set across many skills.
          example: Three skills carry an ad-hoc anti_patterns bullet list with the same fields.
      do_not_apply_when:
        - signal: The extension is one-off and the field set is unstable.
          counter_example: A single skill carries a one-time troubleshooting note with bespoke fields.
      examples:
        - title: Anti-pattern promotion
          before: Bullet list of mixed-shape items inside a technique skill.
          after: First-class optional anti_patterns list with a fixed record shape on technique-skill and discipline-skill.
```

`technique_skill:`:

```yaml
technique_skill:
  identity: Technique skill that procedurally authors a minimal-valid instance block.
  scope:
    covers: [authoring a passing instance block for a schema]
    excludes: [designing new schemas, audit corpus operations]
  techniques:
    - id: author-instance
      name: Author a minimal-valid instance block
      keywords: [instance block, minimal valid, schema fixture]
      goal: Produce a fenced YAML block that validates against its target schema and passes the corpus audit.
      steps:
        - n: 1
          action: Read the target schema and list required keys and list-length minimums.
        - n: 2
          action: Draft the smallest instance that satisfies every required key.
        - n: 3
          action: Run the corpus audit and confirm a pass status.
      gotchas:
        - The keywords list must hold at least three entries on every load-bearing record.
```

`discipline_skill:`:

```yaml
discipline_skill:
  identity: Discipline skill that enforces the keywords-cluster floor on every load-bearing record.
  scope:
    covers: [keywords-cluster authoring discipline]
    excludes: [other schema-floor rules, audit reporting]
  target:
    type: skill
    ref: skills/md-domain/SKILL.md
  rules:
    - id: keywords-min-three
      keywords: [keywords cluster, minimum three, routing floor]
      statement: Every load-bearing record carries a keywords cluster of at least three entries.
      why: The chat-term router cannot disambiguate records whose keyword surface is too thin; below three entries the routing precision collapses.
      counters:
        - excuse: This record is small so two keywords are enough.
          reality: Routing precision is set by surface area not record size; small records still need three entries.
          observed_in: baseline-2026-04-28
      red_flags:
        - A keywords list shorter than three entries.
  pressure_test:
    baseline: A reference-skill draft with several fact records carrying two-entry keywords lists.
    green: Every record now carries at least three keywords and the audit reports a clean pass.
    refactor:
      - loophole: Authors split one record into two to dodge the floor on each.
        closed_by: The floor applies per record after split; the audit re-runs on the new records.
```

`domain_skill:`:

```yaml
domain_skill:
  identity: Domain skill that owns the framework's authoring vocabulary and its reference index.
  companions:
    siblings: []
    note: No sibling domains at this layer.
  scope:
    covers: [framework vocabulary, type contracts index]
    excludes: [content-authoring patterns, runtime audit operations]
  orientation:
    summary: This domain orients a fresh agent in the skill-types framework's vocabulary and routes them to the right per-type contract.
    behavioral_guardrails:
      - Read the glossary before authoring; type contracts reference glossary terms without redefining them.
  index:
    references:
      - id: skill-standards-doc
        path: skills/md-domain/references/standards/skill-standards.md
        keywords: [skill standards, type contracts, schemas as floors]
        summary: Canonical per-type contracts and the schemas-as-floors stance.
```

`capability_skill:`:

```yaml
capability_skill:
  identity: Capability skill that wraps the corpus audit CLI for skill-types validation.
  scope:
    covers: [running the corpus audit, reading its rendered report]
    excludes: [authoring new schemas, modifying owner docs]
  external_capability:
    kind: tool
    name: skills-kit corpus audit
    description: The skills_kit_lib check that validates schema owner docs against their declared schemas.
  layering:
    claude_md: []
    skill_md:
      - The capability surface for invoking the audit and reading its output.
    references: []
  capabilities:
    - id: run-owner-doc-audit
      keywords: [owner doc audit, schema validation, corpus check]
      user_objective: Confirm every registered schema has a valid instance block in its owner doc.
      operation: Invoke check_schema_owner_docs_validate and render its results.
  gotchas:
    - The audit must run from the skills-kit plugin root so the relative owner_doc paths resolve.
```

`audit_skill:`:

```yaml
audit_skill:
  identity: Audit skill that validates owner-doc instance blocks against their schemas across the corpus.
  scope:
    covers: [owner-doc instance validation across registered schemas]
    excludes: [single-skill SKILL.md audits, content-authoring audits]
  subject:
    what: All schemas registered in skills_kit_lib.schema_registry that declare an owner_doc.
    subject_type: corpus
  criteria:
    - id: instance-present
      name: Owner doc contains a root-key instance block
      keywords: [instance present, missing instance, root key block]
      summary: Every registered schema's owner doc carries at least one fenced YAML block whose root key matches the schema.
      severity: FAIL
      detail: The walker collects every recognized typed unit in the document; absence of the schema's root key triggers missing-instance.
  taxonomy:
    - id: missing-instance
      name: Owner doc lacks the root-key block
      keywords: [missing instance, no root block, schema unanchored]
      detection_signal: The walker returns no units with root equal to the schema's root key.
      default_remediation: Author a minimal-valid instance block in the owner doc and re-run the audit.
      bucket: AUTO
  procedures:
    - id: run-audit
      name: Run the owner-doc validation pass
      keywords: [run audit, validation pass, owner doc check]
      goal: Produce a per-schema pass/fail report across the corpus.
      steps:
        - n: 1
          action: Invoke check_schema_owner_docs_validate from skills_kit_lib.checks.
        - n: 2
          action: Render the result list via render_owner_doc_results and inspect each line.
      gotchas:
        - Run from the plugin root so plugin-root-relative owner_doc paths resolve.
  remediations:
    auto:
      - category: missing-instance
        procedure: run-audit
    discuss: []
    special:
      procedure: run-audit
  gotchas:
    - A schema with an owner_doc value that does not point at an existing file produces missing-file, not missing-instance.
```
