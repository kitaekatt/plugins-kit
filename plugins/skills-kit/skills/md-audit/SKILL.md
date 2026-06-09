---
name: md-audit
author: christina
skill-type: domain-skill
description: Use when auditing an md artifact -- a CLAUDE.md, a SKILL.md, or skill cross-references -- or via /md-audit. Do NOT use to author skills (use md-authoring).
disable-model-invocation: false
user-invocable: true
argument-hint: "[skill <path> | claude-md <path> | references [--scope skills|references|md|all]]"
---

# md-audit

The **audit** half of the verb x artifact matrix: the broader domain for auditing the `md` artifact and its specializations (`skill` = SKILL.md, `claude-md` = CLAUDE.md) against the cohesion framework (CCP / CRP / ADP). `md-authoring` is its sibling (authoring the same artifacts); `cohesion-principles` is the placement spine both judge against.

This is a **broader union domain**, not a nest (see `skill-authoring:references/domain-layering.md`). `/md-audit` is a thin router: it greets with a menu, or argument-dispatches into exactly one sub-domain audit and loads only that one. The four member audits are reached **through** `/md-audit` -- their standalone slash commands were collapsed into this single front door.

## Invocation

- **Bare** -- `/md-audit` greets with the menu below; pick an artifact.
- **Argument-dispatched** -- `/md-audit skill <path>`, `/md-audit claude-md <path>`, `/md-audit references [flags]` jump straight into that audit.
- **Natural language** -- "audit this CLAUDE.md", "check my SKILL.md", "find broken skill references", "inventory the skills" -- routed by the artifact named.

### Bare-invocation greeting

```
How can I help you audit?
 - a SKILL.md (contract + cohesion)            (/md-audit skill <path>)
 - a CLAUDE.md (cohesion + hygiene + schema)   (/md-audit claude-md <path>)
 - broken skill cross-references               (/md-audit references)
 - the whole skill corpus (roster / hierarchy) (/md-audit skill roster)

Or can I help you with something else?
```

Show the menu and stop; do not dump audit detail until the user picks.

## Routing

Route by the artifact under audit, then load that member's SKILL.md and run its procedure. The members are non-user-invocable now; `/md-audit` is the only front door.

| You want to audit… | Dispatch | Member (loaded by md-audit) | What it does |
|---|---|---|---|
| a **SKILL.md** (contract + cohesion) | `/md-audit skill <path>` | `skill-audit` | per-skill contract + CCP/CRP/ADP; fans multi-file runs via the Workflow tool; also `roster` / `hierarchy` corpus inventory |
| a **CLAUDE.md** (multi-file capable) | `/md-audit claude-md <path>` | `claude-md-audit` | CCP/CRP/ADP + hygiene + optional `claude_md:` schema + opt-in `density` lens (verbosity / extract-to-reference); fans multi-file runs via the Workflow tool |
| **broken skill cross-references** | `/md-audit references [flags]` | `references-audit` | scans markdown for dangling `/skill` refs and unresolved `skill:` hard deps; fans multi-file classify/remediate via the Workflow tool |

To dispatch: read the member skill's `SKILL.md` (e.g. `skills/skill-audit/SKILL.md`) and follow its procedure, including running its scaffolding script via the plugin venv. The member keeps its full contract, taxonomy, and scripts; md-audit only chooses which one and feeds it the target.

## Domain contract

```yaml
domain_skill:
  _schema_version: "1"
  identity: The broader union domain for auditing the md artifact and its specializations (SKILL.md, CLAUDE.md, skill cross-references) against the cohesion framework (CCP / CRP / ADP); a thin router that argument-dispatches into one member audit at a time.
  companions:
    siblings:
      - md-authoring
    note: |
      md-authoring is the sibling domain (authoring the same md artifacts). Adjacent non-sibling
      skills: cohesion-principles (the placement spine the audits judge against) and materialized-output
      (the insight-view pattern). This domain consumes the spine; it does not duplicate it.
  scope:
    covers:
      - interpreting natural-language or argument-dispatched audit intent and routing to the right member audit
      - greeting + argument-dispatch over the four md-artifact audits (the union-domain router surface)
      - owning the shared audit-framework (glossary + data model) the members operate under
    excludes:
      - authoring or refining the md artifacts (use md-authoring)
      - deciding where content should live -- the placement question (use cohesion-principles)
      - the deep per-audit procedures, taxonomies, and remediation flows (they live in the member skills)
  orientation:
    summary: |
      Four member audits share one subject -- auditing md artifacts against the cohesion framework --
      and one shared framework (the audit-framework glossary + data model, owned here in references/).
      They specialize by artifact: skill-audit for SKILL.md, claude-md-audit
      for CLAUDE.md, references-audit for cross-references. This is a broader union domain: /md-audit is
      a thin router that greets, then argument-dispatches into exactly one member and loads only that one.
      The members' standalone slash commands were collapsed into /md-audit; route, then load the member.
    behavioral_guardrails:
      - Route by artifact -- do not run a SKILL.md audit on a CLAUDE.md (or vice-versa); each member's contract is artifact-specific.
      - Dispatch loads ONE member at a time (union, not nest). Do not co-load all four members' content on a bare invocation; show the menu and wait for the pick.
      - Detection and remediation are separate phases. The audit pass produces a verdict; it does not silently mutate the subject. Remediation is dispatched after, as its own work.
      - Size is a SIGNAL, not a verdict. An over-threshold file prompts a CRP evaluation (do sections serve different reading tasks?), never an automatic split. Defer to cohesion-principles and the CRP test.
  index:
    references:
      - id: audit_framework
        path: references/audit-framework.md
        keywords: [audit framework, glossary, subject, primitive, composition, audit-kind, rule, finding, severity, taxonomy, bucket, corpus]
        summary: Canonical glossary for the audit family -- the vocabulary (subject / primitive / composition / discovery / audit-kind / rule / finding / severity / taxonomy / bucket / corpus / scaffolding) every member audit declares its subject and rules in terms of. Shared substrate owned by this domain.
      - id: audit_framework_data
        path: references/audit-framework.yaml
        keywords: [audit framework data, primitives, compositions, audit-kind registry, rules per composition, machine-readable]
        summary: The machine-readable data side of the framework -- primitives, compositions, and the audit-kind registry (which rule ids bind to which compositions per audit-kind). Authoritative on divergence with the markdown tables.
    members:
      - name: skill-audit
        type: audit-skill
        ref: skill-audit
        keywords: [skill.md audit, contract, roster, hierarchy, inventory]
      - name: claude-md-audit
        type: audit-skill
        ref: claude-md-audit
        keywords: [claude.md audit, cohesion, ccp crp adp, workflow fan-out, multi-file]
      - name: references-audit
        type: audit-skill
        ref: references-audit
        keywords: [broken references, cross-reference scan, soft ref, hard dep, workflow fan-out]
```

## Cross-references

- **Placement spine (what the audits judge against)** — `cohesion-principles` (in skills-kit).
- **Authoring the same artifacts (sibling domain)** — `/md-authoring` (in skills-kit).
- **Members (reached via `/md-audit <artifact>`)** — `skill-audit`, `claude-md-audit`, `references-audit`. All three fan multi-file runs out via the Workflow tool.
