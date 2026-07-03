---
_schema_version: 1
name: claude-md-authoring
author: christina
skill-type: technique-skill
description: Use when md-authoring dispatches authoring a CLAUDE.md -- a valid claude_md block (scope, insights). Do NOT use for SKILL.md (use skill-authoring).
disable-model-invocation: false
user-invocable: false
---

# claude-md-authoring

The `claude-md` specialization of the `md-authoring` domain: the procedure for authoring or
refining a **CLAUDE.md** so its load-bearing `claude_md:` block is well-formed and well-placed.
A sibling of `skill-authoring` (the `skill` specialization) under `md-authoring`; reached via
`/md-authoring claude-md`.

This member is thin on purpose. It owns the *claude-md-specific* craft (the `claude_md:` schema
shape -- scope / insights / conventions / glossary) and **defers** the two general questions:

- **Where a fact lives** (which CLAUDE.md / SKILL.md / reference) -> `cohesion-principles`.
- **What shape a fact takes** (YAML record vs prose vs frontmatter) -> `content-authoring`
  (a reference under `md-authoring`).

The `claude_md:` schema is validated by `python -m skills_kit_lib.audit <CLAUDE.md path>` (via the
plugin venv). Its canonical instance currently lives in `knowledge-encoding` (the schema's owner_doc).

## Contract

```yaml
technique_skill:
  _schema_version: "1"
  identity: The procedure for authoring or refining a CLAUDE.md's claude_md block (scope, insights, conventions, glossary), deferring placement to cohesion-principles and content-shape to content-authoring.
  scope:
    covers:
      - producing a valid claude_md block (scope.covers/excludes, insights, optional conventions/glossary)
      - refining an existing CLAUDE.md against the claude_md schema and the cohesion framework
      - choosing which CLAUDE.md in the load graph a fact belongs to (by deferring to cohesion-principles)
      - authoring a code-directory CLAUDE.md (per-directory review notes for code/yaml/csv) per references/code-directory-claude-md.md -- shapes, high-value observation kinds, anchoring + path discipline (no claude_md block)
    excludes:
      - authoring a SKILL.md or its type contract (use skill-authoring)
      - where a fact lives across the load graph -- the placement question (use cohesion-principles)
      - what content form a fact takes -- YAML vs prose vs frontmatter (use content-authoring)
      - auditing an existing CLAUDE.md (use md-audit -> claude-md-audit)
  techniques:
    - id: author_claude_md_block
      name: Author or refine a CLAUDE.md claude_md block
      keywords: [claude_md block, author claude.md, scope covers excludes, insight record, conventions, refine claude.md]
      goal: Produce a CLAUDE.md whose claude_md block is schema-valid, correctly scoped, and placed in the right CLAUDE.md of the load graph.
      preconditions:
        - "A fact, convention, or insight needs to live in a CLAUDE.md (not a SKILL.md or reference)."
      steps:
        - n: 1
          action: "Confirm the artifact is a CLAUDE.md and the content belongs there. If it is really skill-contract content, hand off to skill-authoring; if it is a reference body, it belongs in a skill's references/, not a CLAUDE.md. BRANCH: if the target sits INSIDE a directory of code / YAML / CSV (a per-directory review-notes file, not a project-root or docs CLAUDE.md), this is a code-directory CLAUDE.md -- it carries review intelligence, NOT a `claude_md:` schema block. Load and follow references/code-directory-claude-md.md (shapes A/B/C/D, the high-value observation kinds, anchoring + path discipline, the value gate); steps 3-5 below (the claude_md block + schema validation) do NOT apply to it."
          tool: "Read (references/code-directory-claude-md.md) when code-directory"
          expected: "Confirmation that a claude_md block is the right home -- OR a determination that this is a code-directory review-notes file, authored per references/code-directory-claude-md.md."
        - n: 2
          action: "Choose WHICH CLAUDE.md in the load graph (root / subsystem / directory) via cohesion-principles (CCP change-cadence -> CRP reader-set -> ADP load-order). Do not re-derive placement. PRE-RESOLVED PLACEMENT: when a placement decision arrives already resolved (an audit remediation naming the destination, an orchestrator directive), follow it -- do not re-invoke cohesion-principles for it; invoke cohesion-principles only for facts without a resolved placement."
          tool: "Skill (skills-kit:cohesion-principles)"
          expected: "One target CLAUDE.md, justified by the placement algorithm (or accepted as pre-resolved)."
        - n: 3
          action: "Write or extend the claude_md block: scope.covers + scope.excludes (the exclusion clause is load-bearing), then insights as records carrying id / keywords (>=3) / summary / detail / origin / added. Add conventions or glossary only if the shape calls for it."
          tool: "Edit"
          expected: "A claude_md block with valid scope + at least the intended insight/convention records."
        - n: 4
          action: "Shape each record per content-authoring (structured YAML records over prose; structure asserts completeness). Match the surrounding CLAUDE.md's existing format and SSOT -- extend an existing record rather than duplicating."
          tool: "Read (md-authoring/references/content-authoring.md)"
          expected: "Records in the file's native shape, no duplication."
        - n: 5
          action: "Validate: run `python -m skills_kit_lib.audit <CLAUDE.md path>` via the plugin venv. Resolve any FAILs (missing scope.excludes, keywords < 3, etc.) before considering it done."
          tool: "Bash (skills_kit_lib.audit)"
          expected: "0 FAILs on the target CLAUDE.md."
      gotchas:
        - "Re-deriving placement from memory instead of invoking cohesion-principles -- which CLAUDE.md a fact belongs in is a framework decision, not a guess. (Exception: a placement already resolved upstream -- audit remediation, orchestrator directive -- is followed, not re-derived; see step 2.)"
        - "A root CLAUDE.md without a `claude_md:` block -- root files SHOULD carry the block; add one when authoring touches a root file that lacks it. (The audit side treats absence on a pre-existing root file as INFO, not FAIL -- adding the block is the authoring path's job.)"
        - "Omitting scope.excludes -- the exclusion clause is what stops adjacent areas from drifting into this file's ownership; the schema requires it."
        - "Putting skill-contract or decision-provenance content into a CLAUDE.md that should be SKILL.md (skill-authoring) or a reference -- match the artifact to the content."
        - "Keywords cluster under 3 entries on an insight record -- the schema floor is >=3 for chat-term routing."
        - "Treating a code-directory review-notes file like a schema-block CLAUDE.md (or vice-versa). Review-notes files in code/yaml/csv dirs carry gotchas/Review-Checks/boundary claims, not a `claude_md:` block; author them per references/code-directory-claude-md.md and do not run the schema validator on them."
        - "Line-only anchors in a code-directory file. Line numbers rot fast; prefer a symbol anchor and drop the number unless the gotcha is sub-function (per references/code-directory-claude-md.md)."
  anti_patterns:
    - id: duplicate_across_claude_mds
      name: Same fact in two CLAUDE.mds
      keywords: [duplicate claude.md, ssot violation, copy fact up and down, drift]
      why_it_seems_right: "Putting the fact in both the root and the subsystem CLAUDE.md feels like it guarantees the reader sees it."
      why_it_is_wrong: "Two copies drift independently; CCP/SSOT is broken. The placement algorithm yields exactly one home."
      alternative: "Place the fact in the single scope cohesion-principles selects (step 2). If sibling scopes also need it, bubble up to the common parent -- still one copy."
```

## Cross-references

- **Domain (parent)** — `/md-authoring` (reached via `/md-authoring claude-md`).
- **Where a fact lives** — `cohesion-principles` (placement: CCP / CRP / ADP).
- **What shape a fact takes** — `content-authoring` (md-authoring reference).
- **Authoring a code-directory CLAUDE.md** — `references/code-directory-claude-md.md` (shapes A/B/C/D, observation kinds, anchoring + path discipline). Its audit counterpart is `claude-md-audit:references/code-dir-insight-filter.md`.
- **Sibling specialization (the skill artifact)** — `skill-authoring`.
- **The claude_md schema instance** — `knowledge-encoding` (current owner_doc).
