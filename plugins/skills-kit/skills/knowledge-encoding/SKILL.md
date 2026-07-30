---
_schema_version: 1
name: knowledge-encoding
author: christina
skill-type: technique-skill
description: Use when encoding a discovered insight into a persistent location (CLAUDE.md, skill, reference doc). Do NOT use for end-of-session doc review.
---

# Knowledge Encoding

The technique of translating a discovered insight into a persistent project
artifact, so the next agent inherits it instead of re-discovering it. Encoding is
the inverse of search: search finds knowledge that exists; encoding makes the
project *have* knowledge you found this session.

This skill owns the encoding **process** -- the ordered procedure for turning an
insight into a durable record. It does NOT own the two decisions the procedure
defers to:

- **Where the fact lives** (which CLAUDE.md / SKILL.md / reference) is decided by
  the placement framework in `skills/md-domain/references/cohesion-principles.md` (CCP /
  CRP / ADP over the load graph). Do not re-derive placement from memory.
- **What shape the fact takes** (YAML record vs prose vs frontmatter) is decided by
  the md-domain content-shape reference (`skills/md-domain/references/authoring-patterns/content-authoring.md`)
  (the three content-form surfaces).

A SKILL.md is a gateway: a trigger plus an index into reference docs. Encoding an
insight into a skill therefore means extending that index (a new reference, or a
new record in an existing one) or, more often, adding an insight record to the
co-located CLAUDE.md -- whichever cohesion-principles.md selects.

The YAML block below is the load-bearing contract; the prose above is orientation.

## Contract

```yaml
technique_skill:
  _schema_version: "1"
  identity: The ordered procedure for encoding a discovered insight into a durable project artifact, deferring placement to md-domain's cohesion-principles reference and content-shape to content-authoring.
  scope:
    covers:
      - turning a session-discovered insight into a persistent record (CLAUDE.md insight, skill reference, anti-pattern, convention)
      - deciding whether an insight is worth encoding at all (encoding-as-proposition)
      - routing the placement decision to cohesion-principles.md and the shape decision to content-authoring.md
      - verifying the encoded insight is discoverable and that schema/framework changes re-audit clean
    excludes:
      - where a fact should live across CLAUDE.md / SKILL.md / references (use md-domain's cohesion-principles.md)
      - what content form a fact should take -- YAML vs prose vs frontmatter (use content-authoring.md)
      - authoring or auditing a skill's type contract (use md-domain's author skill lane)
      - end-of-session documentation review / sweep (use update-documentation)
  techniques:
    - id: encode_insight
      name: Encode a discovered insight into a persistent artifact
      keywords: [encode insight, persist learning, where should this live, future agents, capture finding, prevent re-discovery]
      goal: Translate one discovered insight into a durable, discoverable project record, or consciously decide not to.
      preconditions:
        - "An insight, correction, pattern, or gotcha surfaced this session that a future agent would benefit from."
      steps:
        - n: 1
          action: "Articulate the insight in one sentence -- what was learned, what mistake was corrected, what pattern was found, what would have saved time if known earlier."
          expected: "A single crisp statement of the insight."
        - n: 2
          action: "Decide whether it should be encoded at all (encoding is a proposition, not an obligation). Ask: does the project get better if a future agent carries this? Sometimes the honest answer is no -- the insight is one-off, or it points at a structural fix rather than a doc record. Asking the question has value even when the answer is no."
          expected: "A go / no-go on encoding. On no-go, stop here and say why."
        - n: 3
          action: "Identify the trigger -- the future work context that would need this insight, and the keywords an agent in that context would have in mind. The trigger and keywords are what make the record findable later."
          expected: "A named trigger context plus a keyword cluster (>=3 terms)."
        - n: 4
          action: "Decide WHERE it lives by applying cohesion-principles (CCP change-cadence -> CRP reader-set -> ADP load-order -> frequency tiebreak). Read skills/md-domain/references/cohesion-principles.md rather than re-deriving placement. The output is one target surface (a specific CLAUDE.md, SKILL.md, or reference doc)."
          tool: "Read (${CLAUDE_PLUGIN_ROOT}/skills/md-domain/references/cohesion-principles.md)"
          expected: "Exactly one target surface, justified by the placement algorithm."
        - n: 5
          action: "Decide the CONTENT SHAPE for the target via content-authoring -- a structured YAML record (insight / anti-pattern / convention), a prose section, or a frontmatter field. Default to structured for LLM-facing content; structure asserts completeness a bullet list cannot."
          tool: "Read (${CLAUDE_PLUGIN_ROOT}/skills/md-domain/references/authoring-patterns/content-authoring.md)"
          expected: "A chosen surface (yaml record / prose / frontmatter) and the record shape if structured."
        - n: 6
          action: "Search the chosen target for existing coverage. If a record or section already owns this area, EXTEND it (SSOT); do not create a parallel doc that will drift. Creating a new home is the last resort, only when nothing existing fits."
          tool: "Grep"
          expected: "Either an existing record/section to extend, or a confirmed gap justifying a new one."
        - n: 7
          action: "Write the encoding in the target's existing format. For a CLAUDE.md insight record, carry id / keywords / summary / detail / origin / added. For an anti-pattern, carry the why-it-seems-right / why-it-is-wrong / alternative shape. Always include the WHY and the provenance, not just the what."
          expected: "The insight is written into the target in its native shape."
        - n: 8
          action: "Verify discoverability and integrity. Confirm the record is reachable via its trigger keywords. If the encoding touched a schema, framework.md, or glossary, run the merge-gate re-audit (all SKILL.md + CLAUDE.md to 0 FAILs) before considering it done."
          tool: "Bash (skills_kit_lib.audit, when schemas/framework/glossary changed)"
          expected: "Record is keyword-reachable; any contract change re-audits clean."
      gotchas:
        - "Re-deriving placement from memory instead of reading cohesion-principles.md -- the canonical placement framework already answers 'where does this live', and ad-hoc reasoning drifts from it."
        - "Creating a new doc when an existing record/section already owns the area -- duplication breaks SSOT and the two copies drift independently (CCP violation)."
        - "Encoding a COMMON agent error behind a skill's trigger -- a frequently-hit error must live ambient in a CLAUDE.md, not gated behind a skill invocation that may not fire (ADP)."
        - "Importing vocabulary or references from another project -- only name docs, skills, and concepts that actually exist in THIS repo. (This skill itself was an imported wholesale copy whose alien refs went unnoticed for months; do not recreate that.)"
      checklist:
        - "Insight stated in one sentence"
        - "Go / no-go decided (encoding is a proposition)"
        - "Trigger + keyword cluster identified"
        - "Placement chosen via cohesion-principles.md"
        - "Shape chosen via content-authoring.md"
        - "Existing coverage searched; extend-over-create honored"
        - "Encoding written with why + provenance"
        - "Discoverability verified; merge-gate re-audit run if a contract changed"
  anti_patterns:
    - id: asking_instead_of_placing
      name: Asking the user where it should live
      keywords: [ask where, defer to user, placement question, cohesion-principles bypass]
      why_it_seems_right: "Placement feels like a judgment call the user should make, so asking 'where should this go?' seems collaborative."
      why_it_is_wrong: "Placement is a solved, framework-driven decision (cohesion-principles.md). Punting it to the user adds a round-trip and signals the agent has not consulted the framework it should be using."
      alternative: "Run the cohesion-principles placement algorithm (step 4). Surface the chosen target and the reasoning; ask the user only to confirm a genuinely ambiguous CRP tie, not to make the call from scratch."
    - id: jam_it_in_anywhere
      name: Transcribing instead of translating
      keywords: [jam it in, transcribe, dump text, wrong shape, no translation]
      why_it_seems_right: "The insight is true and writing it down somewhere feels like progress."
      why_it_is_wrong: "Encoding is translation, not transcription. Dropped into the wrong surface or the wrong shape, the insight is either unreachable (no trigger) or unasserted (prose where a record was needed)."
      alternative: "Choose the surface via cohesion-principles.md and the shape via content-authoring.md before writing (steps 4-5). Match the target's existing format (step 7)."
    - id: duplicate_instead_of_extend
      name: New doc when one already owns the area
      keywords: [duplicate doc, parallel reference, ssot violation, should have extended, new file reflex]
      why_it_seems_right: "A fresh doc is easy to write and feels clean; the existing doc looks crowded."
      why_it_is_wrong: "Two docs covering one area drift apart; a future agent reads one and misses the other. SSOT is broken and the CCP boundary is wrong."
      alternative: "Search the target first (step 6). Extend the record or section that already owns the area; create a new home only when nothing existing fits."
```

## The Meta-Insight

This skill is itself an instance of knowledge encoding: the recurring "where
should this live, and in what shape?" conversation was encoded once -- as a
procedure that routes the two sub-decisions to the references that own them
(md-domain's cohesion-principles.md, content-authoring.md) -- so the conversation
does not repeat.

## Cross-references

- **Where a fact lives** -- `skills/md-domain/references/cohesion-principles.md` (placement: CCP / CRP / ADP over the load graph).
- **What shape a fact takes** -- the md-domain content-shape reference (`skills/md-domain/references/authoring-patterns/content-authoring.md`) (the three content-form surfaces).
- **Authoring/auditing a skill's type contract** -- `Skill(skills-kit:md-domain)`, author skill lane.
- **End-of-session documentation sweep** -- `/skills-kit:update-documentation` (feeds individual insights to this skill).

## CLAUDE.md instance example

CLAUDE.md is the canonical persistence target for the insights this skill encodes. The `claude_md:` schema validates the shape of that file's load-bearing content -- scope, plus insights and/or conventions (and an optional glossary). The non-empty floor is the insights/conventions UNION: a conventions-only CLAUDE.md validates (rules + why with no insight records is a legitimate shape); an empty block -- neither insights nor conventions -- fails the audit. A minimal-valid instance block:

```yaml
claude_md:
  scope:
    directory: plugins/skills-kit
    covers:
      - skills-kit plugin conventions and persistent insights
    excludes:
      - per-skill SKILL.md content
  insights:
    - id: owner-doc-bidirectional-drift
      keywords: [owner doc, schema drift, instance validation]
      summary: Every registered schema declares an owner_doc that must carry a valid instance block; the corpus audit enforces bidirectional anti-drift.
      detail: Schema changes break the owner doc's example block; owner-doc edits that drop or corrupt the block fail the audit on the next run.
      origin: skills_kit_lib.checks.check_schema_owner_docs_validate, 2026-05-19.
      added: "2026-05-19"
```
