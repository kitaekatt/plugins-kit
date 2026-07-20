---
name: md-authoring
author: christina
skill-type: domain-skill
description: Use when authoring or refining an md artifact -- a SKILL.md or a CLAUDE.md -- or via /md-authoring. Do NOT use for auditing (use md-audit).
disable-model-invocation: false
user-invocable: true
argument-hint: "[skill | claude-md]"
---

# md-authoring

The **authoring** half of the verb x artifact matrix: the broader domain for authoring the `md`
artifact and its specializations (`skill` = SKILL.md, `claude-md` = CLAUDE.md). `md-audit` is its
sibling (auditing the same artifacts); `cohesion-principles` is the placement spine both judge against.

This is a **broader union domain**, not a nest (see `skill-authoring:references/domain-layering.md`).
`/md-authoring` is a thin router: it greets with a menu, or argument-dispatches into exactly one
sub-domain and loads only that one. `skill-authoring` is itself a domain-skill kept whole as a
sub-domain member; `claude-md-authoring` is the thin claude-md specialization.

The domain owns the **content-shape** references (`content-authoring` and its deeper docs) -- the
*how* a fact should be shaped, shared by both authoring sub-domains. The orthogonal *where* a fact
lives is `cohesion-principles` (standalone, cited by both md-authoring and md-audit).

## Invocation

- **Bare** -- `/md-authoring` greets with the menu below; pick an artifact.
- **Argument-dispatched** -- `/md-authoring skill` or `/md-authoring claude-md` jump straight into that sub-domain.
- **Natural language** -- "author a skill", "refine this SKILL.md", "write a claude_md block" -- routed by the artifact named.

### Bare-invocation greeting

```
How can I help you author?
 - a SKILL.md (skill design, type contracts, scripts) (/md-authoring skill)
 - a CLAUDE.md (a valid claude_md block)              (/md-authoring claude-md)

Or can I help you with something else?
```

Show the menu and stop; do not co-load both sub-domains.

## Routing

Route by the artifact being authored, then load that member and follow it. The members are reached through `/md-authoring`.

| You want to author… | Dispatch | Member (loaded by md-authoring) | What it is |
|---|---|---|---|
| a **SKILL.md** (any of the skill types) | `/md-authoring skill` | `skill-authoring` (a domain-skill, kept whole) | vocabulary, type contracts, audit/classify/tag tooling |
| a **CLAUDE.md** (a `claude_md` block) | `/md-authoring claude-md` | `claude-md-authoring` (technique-skill) | produce a valid claude_md block; defers placement + shape |

To dispatch: load the member skill's `SKILL.md` and follow it. `skill-authoring` is itself a domain (it has its own deep reference graph and scripts); loading it on dispatch is the union pattern -- you load this router plus that one sub-domain, never both.

**Project documents** (standalone reference docs outside skills and the CLAUDE.md hierarchy) have no authoring member; they are authored directly against `cohesion-principles`' `project_reference_md` role -- forward-only load-graph edges, one-hop cross-references, no back-references into CLAUDE.md sections, and pointers to skill-owned content rather than restatement (the skill's references/ stays SSOT). The audit counterpart is `/md-audit project-doc`.

## Domain contract

```yaml
domain_skill:
  _schema_version: "1"
  identity: The broader union domain for authoring the md artifact and its specializations (SKILL.md via skill-authoring, CLAUDE.md via claude-md-authoring); a thin router that argument-dispatches into one sub-domain at a time and owns the shared content-shape references.
  companions:
    siblings:
      - md-audit
    note: |
      md-audit is the sibling domain (auditing the same md artifacts). Adjacent non-sibling skills:
      cohesion-principles (the placement spine -- where a fact lives) and knowledge-encoding (encoding
      a discovered insight into a persistent artifact). This domain owns content-shape (how), not
      placement (where).
  scope:
    covers:
      - routing authoring intent to the right sub-domain (skill vs claude-md) via greeting + argument-dispatch
      - owning the content-shape references (content-authoring + the three-surfaces / area / actions / query-tool docs) shared by both sub-domains
      - orienting a fresh agent on the authoring half of the verb x artifact matrix
    excludes:
      - auditing md artifacts (use md-audit)
      - where a fact lives -- the placement question (use cohesion-principles)
      - the deep per-artifact authoring contracts and procedures (they live in the member skills)
  orientation:
    summary: |
      Two sub-domains author the md artifact, specialized by which md: skill-authoring for SKILL.md
      (a domain-skill kept whole, with its own type contracts and audit/classify/tag scripts) and
      claude-md-authoring for CLAUDE.md (a thin technique producing a valid claude_md block). This is
      a broader union domain: /md-authoring greets, then argument-dispatches into exactly one
      sub-domain and loads only that one. The domain owns the content-shape references both share;
      placement (where a fact lives) is deferred to cohesion-principles.
    behavioral_guardrails:
      - Route by artifact -- skill vs claude-md. Do not apply SKILL.md type contracts to a CLAUDE.md or vice-versa.
      - Dispatch loads ONE sub-domain at a time (union, not nest). On a bare invocation show the menu and wait; do not co-load skill-authoring and claude-md-authoring together.
      - Defer placement to cohesion-principles and content-shape to content-authoring; this domain routes and owns the shared references, it does not re-derive those frameworks.
      - Summarize-and-reference, do not restate: keep a fact in its SSOT and reference it elsewhere; the compact form is a reminder plus a reference, and only when the fact fits ~a dozen tokens -- beyond that, reference only. See cohesion-principles `summarize_and_reference` for the rule and its loss-free-deletion guard (this is the authoring side of the dedup the md-audit skills apply as a FIX).
      - Do not author a recommended pattern -- only required, conditionally required, prohibited (inherited from skill-authoring's discipline).
  index:
    references:
      - id: content_authoring
        path: references/content-authoring.md
        keywords: [content shape, three surfaces, yaml header markdown embedded yaml, structure asserts, analysis framework, how to shape a fact, typed unit composition]
        summary: The content-shape framework -- the three content-form surfaces (yaml header / markdown text / embedded yaml) and the analysis framework for choosing between them. The how, orthogonal to cohesion-principles' where. Folded in from the former content-authoring skill; shared by both authoring sub-domains.
      - id: three_surfaces
        path: references/three-surfaces.md
        keywords: [parser model, surface comparison, structure asserts deep, wrapper rule, common authoring mistakes, per-surface analysis]
        summary: Deep treatment of each content-form surface -- parser, audience, fits-when -- plus the structure-asserts deep dive and common authoring mistakes.
      - id: area_ownership
        path: references/area-ownership.md
        keywords: [area definition, ownership expression, identity, scope, covers excludes, sub-area registry, single-area, multi-area, audit hooks]
        summary: What an area is and how a document expresses ownership -- identity + scope (single-area) and the optional sub-area registry (multi-area). Owner doc for the sub_areas portable unit.
      - id: area_config
        path: references/area-config.md
        keywords: [area config, runtime contract, state_terms, operations, scope_axes, canonical_phrasing, llm_dependent_content, dependency_order]
        summary: The six-field runtime contract for an area. Owner doc for the area_config portable unit.
      - id: actions_pattern
        path: references/actions-pattern.md
        keywords: [actions pattern, ordered operations, steps schema, capture, tell_user, facade script, narration discipline]
        summary: The ordered-list-of-operations shape -- a YAML actions schema with steps, the facade-script convention, narration discipline. Owner doc for the actions portable unit.
      - id: query_tool_pattern
        path: references/query-tool-pattern.md
        keywords: [query tool facade, catalog lookup, yaml output discipline, did-you-mean, spelling discovery, gazetteer, cli mode pattern]
        summary: The facade pattern for wrapping a catalog with a single CLI offering multiple lookup modes; the YAML-output discipline and did-you-mean records.
    members:
      - name: skill-authoring
        type: domain-skill
        ref: skill-authoring
        keywords: [skill authoring, skill design, type contracts, audit classify tag, glossary framework, skill.md]
      - name: claude-md-authoring
        type: technique-skill
        ref: claude-md-authoring
        keywords: [claude.md authoring, claude_md block, scope insights conventions, produce valid claude_md]
```

## Cross-references

- **Placement spine (where a fact lives)** — `cohesion-principles` (in skills-kit).
- **Auditing the same artifacts (sibling domain)** — `/md-audit` (in skills-kit).
- **Encoding a discovered insight into an artifact** — `knowledge-encoding` (in skills-kit).
- **Members (reached via `/md-authoring <artifact>`)** — `skill-authoring`, `claude-md-authoring`.
