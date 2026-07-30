# Skill Types Framework

This document defines the **contracts** that skills in this project must
satisfy. Per SSOT, vocabulary lives in `glossary.md` -- types,
building blocks, patterns, principles, and attributes. Read the glossary
first; this document references entries by name without redefining them.

## Canonical contract surface (schema v1, locked 2026-04-28)

The **canonical** contract per skill type is the YAML schema declared in
`plugins/skills-kit/skills_kit_lib/schema_registry.py` (with the schema
literals in `skills_kit_lib/schemas/`). Each
skill carries a `<type>:` YAML block in its SKILL.md body; the audit
script validates that block against the schema. A skill is well-formed
when its YAML contract block parses successfully against its type's
schema.

Schema v1 was locked after all six existing plugins-kit skills converted
cleanly. Future schema changes ship as v2 alongside v1; the validator dispatches by `_schema_version:` in each
skill's YAML root.

The structurally-repetitive framework content below is captured as YAML records. The "Type contracts" section retains markdown tables for human review; skills_kit_lib/schema_registry.py is authoritative on divergence.

```yaml
framework:
  _schema_version: "1"
  scope:
    covers:
      - what every skill must satisfy regardless of type (description requirements, schemas-as-floors, content-form choice)
      - the framework's two design goals (auditability, robustness)
      - the conditional-requirement grammar
      - the audit procedure for an existing skill
      - open questions awaiting real-world friction signal
    excludes:
      - vocabulary (lives in glossary.md)
      - the 5 per-type contracts (kept as markdown tables below; canonical machine form in skills_kit_lib/schema_registry.py)

  schemas_are_floors:
    keywords: [floors not ceilings, extras allowed, forbidden_keys, mixed-type drift, structure-friendly]
    summary: |
      The per-type YAML schemas validate the required minimum: which keys must be present, what their structure must be, and which prohibited keys signal cross-type drift. Authors may add load-bearing structured keys beyond what the schema enumerates -- e.g. an `exceptions:` list inside an anti-pattern entry, a `narration:` sub-block describing agent narration patterns inside a technique, a `false_positive_guardrails:` record inside a multi-agent review technique. The schema does not enumerate every key an author may use; it enumerates the floor that must be there.
      
      This matters because the framework biases toward structured data (see Content-form choice). Forbidding extras would push authors toward unstructured prose when they want to add legitimate structure the schema didn't anticipate. The schema is a contract on the floor, not a straitjacket on the ceiling.
      
      Mixed-type drift is detected via the explicit `forbidden_keys:` list on each schema (e.g. a `rules:` key inside `reference_skill:` is forbidden because rules belong to discipline-skills). Forbidden keys are deliberate cross-type signals; unknown keys not in the forbidden list are permitted.
    promoted_extensions:
      - field: anti_patterns
        applies_to: [technique_skill, discipline_skill]
        required: false
        record_shape: [id, name, keywords, why_it_seems_right, why_it_is_wrong, alternative]
        rationale: |
          Anti-patterns are the canonical example of "structure asserts." Containment in
          a list of records carrying these specific keys implicitly asserts every item
          is a real anti-pattern; a markdown bullet list carries no such assertion. A
          common-enough load-bearing extension was promoted to a first-class optional
          field on the two skill types whose surface (procedure or rule) frequently
          benefits from naming the lookalike-but-wrong moves an agent would reach for.
          On technique_skill, anti_patterns also participates in the caution-surface
          floor: ">=1 gotcha OR >=1 anti_pattern record" satisfies the known-gotchas
          requirement, because the two are alternate containers for the same caution
          surface. Duplicating one caution across both (or across steps[].detail and
          either) is itself an anti-pattern -- the reader pays twice.
        keywords: [anti-pattern record, named anti-patterns, lookalike-but-wrong, structured assertion, technique-skill optional, discipline-skill optional]

  framework_goals:
    - id: auditability
      keywords: [auditable, contract, mechanical evaluation, deficiency, retroactive order]
      summary: "A skill is auditable when we can evaluate it against a defined contract and mechanically identify deficiencies."
      detail: |
        Without a contract, a skill is just markdown -- no objective standard exists. With one, we can flag missing required patterns, surface anti-patterns, and bring retroactive order to skills that grew organically.
    - id: robustness
      keywords: [robust, consistent across calls, organic decay, raise the floor]
      summary: "A skill is robust when it behaves consistently across calls and resists organic decay."
      detail: |
        Meeting a user's immediate goal in a skill is often fast and easy; producing a robust skill is a separate discipline. Robustness raises the floor -- if a skill deserves to be in this project, it deserves to be robust within the framework.

  conditional_requirements:
    keywords: [conditional requirement, testable criterion, IF-THEN, no recommended category]
    grammar: |
      A *conditionally required* row in a contract specifies a pattern or block that becomes mandatory when a stated condition holds. Every conditional requirement carries an explicit, testable criterion -- without one, the requirement is not auditable and doesn't belong in this framework.
      
      The structure:
      
      **IF** *condition* (testable by *criterion*) **THEN** *pattern or block is required*
    examples:
      - rule: progressive disclosure CONSIDERED when SKILL.md body exceeds 500 lines or 3000 tokens; the split is REQUIRED only if it passes CRP (sections serve different reading tasks). The size threshold is a signal that the split deserves evaluation, not a verdict that splitting is correct.
        criterion: line/token count of the SKILL.md body triggers the evaluation; per-section CRP is the test that decides whether to split. If no CRP-passing decomposition exists, the larger SKILL.md is the right answer -- a stub-with-always-co-loaded-reference is a tool-call doubling, not a context-efficiency win.
        anti_pattern: SKILL.md trimmed to a thin pointer that links to a single reference always loaded next. The reader pays two file loads for one reading task; CRP fails because the "sections" do not serve different reading tasks. Revert by inlining the reference back into SKILL.md and accepting the over-threshold size, or by finding a genuine sub-trigger decomposition.
      - rule: explicit step-tracking required when a technique has more than three steps -- satisfied by EITHER a paste-able `- [ ]` checklist OR an explicit step-tracker invocation (TaskCreate, scratch file, or equivalent) at the start of the procedure
        criterion: count `step` blocks in the technique definition; presence of either a tickbox checklist OR a step-tracker-invocation marker satisfies the row
        note: the goal is the discipline of explicit step-tracking; the markdown syntax is one path, not the only one. If the procedure already invokes a step tracker (e.g. TaskCreate), a parallel `- [ ]` checklist adds no information and is not required.
        canonical_form_when_on_yaml_contract: when a skill is on the YAML contract, the `steps:` list IS the canonical step-tracking surface -- it is structured, schema-validated, keyword-able, and authoritative. A parallel markdown `- [ ]` checklist alongside the YAML steps creates a two-source-of-truth drift hazard and is not required. The shape hierarchy is YAML `steps:` for contract skills; markdown `- [ ]` for legacy / non-YAML-contract skills; mixed shapes promote to YAML and drop the markdown rather than maintaining both.
      - rule: sub-agent binding rule required when a paired sub-agent exists
        criterion: check for a matching <skill-name>-a agent definition
      - rule: vocabulary block required when reference files use canonical terms not defined in SKILL.md
        criterion: scan reference files for repeated terms; check whether each is defined inline
    no_recommended_category: |
      A conditional requirement without a testable criterion is a *recommendation*, not a requirement. The framework deliberately omits a "recommended" category -- patterns are either required (always or under a stated condition) or prohibited. If a real-world audit shows the strictness is wrong, the contract gets revised; the framework doesn't accumulate fuzzy middle ground.

  description_requirements:
    keywords: [description requirements, frontmatter, universal, cost-justified, clear condition]
    intro: |
      The frontmatter `description` is the only signal Claude uses to decide whether to load a skill. Every skill, regardless of type, must satisfy these:
    rules:
      - id: length
        keywords: [length budget, 160 chars, summarizing capability vs trigger]
        rule: "Length: <=160 characters."
        why: "A description that doesn't fit in 160 characters is summarizing capability rather than naming a trigger."
      - id: form
        keywords: [directive, use when, invoke when, capability summary anti-pattern]
        rule: "Form: directive."
        why: "Open with 'Use when...' or 'Invoke when...'. Capability summaries ('Enables...', 'Provides...', 'Manages...') cause Claude to follow the description as a workflow instead of reading the body."
      - id: condition
        keywords: [clear condition, unambiguous trigger, vague trigger anti-pattern]
        rule: "Condition: clear and unambiguous."
        why: "The trigger must name a specific situation when invocation is the right move. Vague conditions ('when you need help with X', 'for any X work') are evidence the skill is doing too much or doesn't have a real role."
      - id: cost_justified
        keywords: [cost-justified, tool-call boundary, topical adjacency, over-aggressive trigger]
        rule: "Cost-justified."
        why: "Every skill load is tokens and a tool-call boundary. A trigger that fires on topical adjacency ('...for any Python work...', '...whenever you read code...') violates the user's trust by burning tool calls without bringing value. Be specific about when the skill earns its load cost."
      - id: exclusion_clause
        keywords: [exclusion clause, do-not-use-for, bound activation surface]
        rule: "Exclusion clause: present."
        why: "Append a 'Do NOT use for...' clause that bounds the activation surface. Positive triggers alone do not bound activation; the exclusion is what keeps adjacent skills from triggering each other's loads."
    universal: |
      These requirements are universal -- they apply to every skill regardless of type contract. The auditing process checks them as the first step (after the mixed-type check).

  content_form_choice:
    keywords: [yaml default, prose exception, embedded not pure-yaml, structure-carries-assertions]
    intro: |
      The default for LLM-facing content is structured YAML. Skills are runtime context for Claude (Audience-Claude); structure aids Claude's comprehension and enables routing, keyword matching, and validation that prose cannot. **When in doubt, bias toward structured data.**
    rules:
      - id: use_yaml_by_default
        keywords: [yaml default, structured data, records, lookup tables, indexes]
        rule: "Use YAML by default for LLM-facing content"
        detail: |
          Records with the same shape (facts, rules, capabilities, steps, references, anti-patterns, gotchas), lookup tables, indexes, contract data, anything where keywords route per record. Structure carries assertions prose cannot -- an `anti_patterns:` list with each entry as a record asserts implicitly that every item is genuinely an anti-pattern; a markdown bullet list carries no such assertion.
      - id: use_prose_only_when
        keywords: [prose exception, naturally narrative, bar for prose, articulate why]
        rule: "Use prose only when the content is naturally narrative or hierarchy carries no meaning"
        detail: |
          (a) the content is naturally narrative -- an identity sentence, an orientation paragraph, a single-paragraph explanation that does not decompose into discrete records; or (b) the hierarchy carries no meaning over prose. The bar for prose is "I can articulate why this would be worse as YAML." If you cannot articulate that, default to YAML.
      - id: embedded_not_pure_yaml
        keywords: [embedded yaml, markdown wrapper, orientation surface, pure yaml anti-pattern]
        rule: "Embedded YAML in markdown, not pure-YAML SKILL.md"
        detail: |
          SKILL.md keeps a markdown wrapper -- title, identity sentence, brief orientation -- around fenced YAML blocks. Pure-YAML SKILL.md files are harder to skim during review and lose the orientation surface. The YAML carries the load-bearing contract; the markdown carries the priming.
    note_on_old_default: |
      The previous default ("if unclear, prose") was wrong-direction for an Audience-Claude framework. Structure is the default; prose is the documented exception.

  compositional_order:
    keywords: [dependency graph, bottom-up composition, atomic primitives, type order]
    intro: |
      Authoring follows the type dependency graph:
    steps:
      - n: 1
        types: [reference-skill, pattern-skill]
        guidance: "Atomic, no dependencies. Write these first."
      - n: 2
        types: [technique-skill]
        guidance: "Composes references and patterns into procedure."
      - n: 3
        types: [discipline-skill]
        guidance: "Wraps a target technique or pattern with rules + counters."
      - n: 4
        types: [domain-skill]
        guidance: "Assemble only after the leaf members exist."
    anti_pattern: |
      Top-down domain-skill authoring tends to produce a long monolithic SKILL.md that should have been five small files plus an index. The contract for domain-skill explicitly prohibits this.

  auditing_procedure:
    keywords: [audit procedure, mixed-type check, contract checklist, audit criterion]
    mixed_type_check:
      keywords: [mixed-type check, common audit finding, split along boundaries]
      detail: |
        Before classifying or running a contract, check whether the skill spans multiple types. A skill containing rule-and-counter material *plus* lookup tables, or how-to-procedure *plus* recognition criteria *plus* an aggregation index, is mixed-type. Mixed-type skills are the most common audit finding because skills tend to grow organically across type boundaries. The right remedy is splitting along those boundaries, not forcing the skill into one type's contract. Audit each type's content separately; the skill as currently written cannot pass any single type's contract while it remains mixed.
    steps:
      - n: 1
        action: identify the declared (or implicit) type
        detail: "If the skill claims none, infer from content shape."
      - n: 2
        action: run the contract checklist
        detail: "Required blocks present? Required patterns applied? Conditional requirements: do any conditions hold, and if so are the required patterns present? Any prohibited patterns?"
      - n: 3
        action: run the audit criterion
        detail: "This is the behavioral test, not a static check."
    worked_example_writing_skills: |
      Applied to the current `writing-skills`: it teaches discipline-skill authoring (RED/GREEN/REFACTOR with adversarial subagents) and presents that approach as universal. Under this framework that fits the discipline contract correctly but is the wrong shape for the other four types. A redraft should:
      
      1. Assume the glossary.
      2. Lead with this framework's contracts.
      3. Scope the existing red/green/refactor pressure-testing content to the discipline-skill contract.
      4. Provide per-type contract checklists and audit criteria.

  open_questions:
    - id: q1_five_types_granularity
      keywords: [five types, granularity, technique-skill user-only attribute, script-skill collapse]
      question: "Are the five types the right granularity?"
      context: |
        Specifically, does collapsing the previous `script-skill` into `technique-skill` with a `trigger: user-only` attribute lose anything important about user-invoked workflows?
    - id: q2_harness_targeted_type_status
      keywords: [harness-targeted, type vs attribute, audit criterion change]
      question: "Does `harness-targeted` deserve type status?"
      context: |
        It changes the audit criterion (verify the harness reflects the change) more than other attributes do. Currently treated as an attribute.
    - id: q3_prohibited_patterns_strictness
      keywords: [prohibited patterns, discipline hedge prohibition, rule-with-exception]
      question: "Are the prohibited patterns per type too strict?"
      context: |
        Notably: discipline-skills currently prohibit hedging. Real rules often have legitimate exceptions; should the contract distinguish "rule" from "rule-with-exception"?
    - id: q4_index_block_machine_readable
      keywords: [index machine-readable, conditional loading, automated audit of index]
      question: "Should the `index` block in domain-skills be machine-readable?"
      context: |
        A YAML or table of `{name, trigger, file}` would enable automated audits.
```

## Content allocation across CLAUDE.md / SKILL.md / references/

A separate-from-type principle that governs how content is allocated across the three load levels. Most acutely required for capability-skills (which have ambient L1 territory because the wrapped external thing is used widely); applies to any skill that has SKILL.md plus references/ plus relevant project CLAUDE.md content.

The three layers and their tests:

### L1 -- CLAUDE.md (ambient, always loaded)

Test: would the agent fail a tool call or violate a project convention without this knowledge in MOST sessions?

Lives here: common operations the agent runs constantly; project-specific syntax substitutions; tool-call gotchas that fail safety checks; hard prohibitions; one-line skill-discovery breadcrumbs ("for advanced X, see /Y"). Every load-bearing fact in CLAUDE.md justifies its ambient cost.

Does NOT live here: advanced procedures, deep mechanics, edge cases, syntax references for rare operations.

### L2 -- SKILL.md (triggered, loaded on activation)

Test: would a fresh agent need this to navigate to the right reference doc once the domain trigger fires?

Lives here: identity sentence; brief orientation on the domain shape; capability surface (what operations exist); Conditional Loading index pointing at L3; behavioral guardrails that span the domain.

Does NOT live here: deep step-by-step procedures (those are L3); tool-specific syntax tables (L3); content already in CLAUDE.md (L1 territory).

### L3 -- references/*.md (on-demand, loaded by name)

Test: is this content only relevant when one specific advanced situation fires?

Lives here: step-by-step workflows for ONE operation; edge cases + error recovery; tool-specific syntax tables; worked examples; shared prerequisites extracted from multiple member skills (CCP cleanup).

Does NOT live here: orientation (L2); ambient setup (L1).

### The contested boundary: L1 vs L2

The framework's progressive-disclosure pattern names L1/L2/L3 load levels but is silent on which content goes where. The rule above resolves it: **frequency of need + cost of absence**. Frequent + tool-call-failing -> L1. Situational + orientation-only -> L2. Specific + deep -> L3.

For capability-skill, this allocation is required (the schema's `layering:` field declares the manifest). For other skill types, it is the default authoring guide; the schema does not enforce it.

### CRP is the test for L2 -> L3 splits

L2 (SKILL.md) and L3 (references/*.md) are different load events: loading a reference is a second tool call after the skill loads. A split that creates a stub-with-always-co-loaded-reference is a tool-call doubling, not a context-efficiency win. The decision rule:

**Three principles in tension:**
1. Loading context the agent does not need is bad (context efficiency).
2. Two tool calls where one would suffice is bad (tool-call efficiency).
3. There is a size threshold beyond which a SKILL.md is too large to keep monolithic (the 500-line / 3000-token signal).

**The test that resolves the tension is CRP (Common Reuse Principle):** if a reader loads one section, they should plausibly need the rest. Sections serve different reading tasks -> split is legitimate. Sections always read together -> split is illegitimate (it manufactures a second tool call for content that always co-loads).

**Operational rule:**
- The size threshold (>500 lines / >3000 tokens) signals that the skill DESERVES evaluation for a split.
- CRP is the test: enumerate the proposed sections, identify whether each fires on the same trigger or on independent sub-triggers.
- Split only when at least one section can be omitted on a typical invocation. The remaining SKILL.md must be a viable standalone for the case when the omitted reference does not load.
- If no decomposition passes CRP, keep the larger SKILL.md. An over-threshold SKILL.md that costs one tool call is better than a stub-plus-always-co-loaded reference that costs two.

**Anti-pattern (CRP-fail split):** SKILL.md trimmed to a thin pointer that always points at one (or N) references that always load next. Symptoms: SKILL.md is short (~30-100 lines) and contains primarily Conditional Loading entries; every reference is "loaded every time the skill fires" with no sub-trigger that selects between them. Revert by inlining the reference back into SKILL.md and accepting the over-threshold size.

**CRP-pass split (worked example):** a domain-skill whose body declares N member sub-domains, with a Conditional Loading entry per sub-domain. Each sub-domain reference fires on a different sub-task within the domain (e.g. "task-category-A" vs "task-category-B" vs "task-category-C"). A typical invocation loads SKILL.md plus one sub-domain reference, not all of them. Average load shrinks; the second tool call is paid only when actually navigating into the sub-domain.

### When to consolidate skills into a domain (the merge direction)

`compositional_order` covers building a *new* domain bottom-up; `auditing_procedure.mixed_type_check` covers splitting *one* skill that outgrew its type. This section covers the third, retroactive case: looking across the corpus at N existing standalone skills and deciding whether they should merge into one domain-skill.

A domain-skill is a container that routes among operations on one shared subject. Consolidation is justified only when **both** hold:

1. **2+ skills share a subject** -- not co-location, not topical adjacency, not a shared pattern. The same *subject*. One skill is just a skill; never wrap a singleton in a domain.
2. **The skills are "doer" types** -- a domain's members are operations. Which type a skill is determines whether it merges as a member or folds in as supporting content:

| Type | Role in consolidation |
|---|---|
| technique / capability / audit | **Merge as members** -- they are operations over the subject; multiple operations on one subject *is* the domain. |
| reference / pattern / discipline | **Fold in, don't merge** -- knowledge, not operations. A reference becomes an L3 doc; a pattern stays standalone (it applies across many subjects); a discipline becomes the domain's guardrails. None needs its own member sub-trigger. |
| domain | **May be a sub-domain member of a *broader union domain*** -- a thin parent that greets + argument-dispatches into one sub-domain at a time (the domain-layering pattern) loads the router plus one sub-domain, so the top-level CRP test passes. What fails CRP is a *nest*: a parent that force-co-loads its member domains' full content. Union = selective dispatch (allowed); nest = co-load (prohibited). See "Broader union domains over sub-domains" below. |

Consequences:
- A cluster that is *one doer + N references* is one skill with references, not a domain.
- Skills that share a **pattern** (e.g. several "produce an HTML insight view" skills) but operate on **different subjects** are not a domain -- they reference one pattern-skill and stay independent.
- Skills co-located in one **plugin** are not thereby a domain. A plugin is a packaging unit; a domain is a subject unit. They can diverge (a junk-drawer plugin of unrelated skills) or a single subject can split across two plugins.

The merge passes CRP for the same reason an L2 -> L3 split does: each member fires on a distinct sub-trigger, so a typical invocation loads the container plus one member, not all of them. If every candidate member would load on every invocation, it is a CRP-fail merge -- keep them as separate skills (or as one skill), exactly as you would revert a CRP-fail split.

**Audit hook:** in a corpus inventory (`/md-domain audit skill hierarchy`), cluster skills by subject and flag any subject owning 2+ doer-type skills as a domain-consolidation candidate; flag any domain-skill whose members all co-load as a CRP-fail to revert.

### Shared references across sibling domains stay standalone

The merge table above sends a *reference* skill to "fold in -> becomes an L3 doc" of the domain it supports. That disposition assumes a single consuming domain. When the same reference is cited by **2+ sibling domains**, it cannot fold into any one of them without the others losing it -- folding it into domain A severs domain B's edge to it.

Such a reference stays **standalone**, exactly as a pattern-skill does, and every consuming domain cites it. This is the cross-verb base case: a reference that is the shared substrate of two domains (for example, a placement framework cited by both an authoring domain and an audit domain) is structurally a pattern in the merge table's sense -- it "applies across many subjects" -- even though it is typed reference-skill.

**Test:** if folding a reference into one domain would force a sibling domain to reach across a domain boundary to read it, keep it standalone.

**Worked example (historical):** `cohesion-principles` (placement: CCP/CRP/ADP) was cited by both `md-authoring` (placement while authoring) and `md-audit` (placement criteria while auditing) -- two sibling domains -- so it stayed a standalone reference-skill rather than folding into either. That justification expired when the two domains merged into `md-domain` (the phase-3 restructure): with a single consuming domain, cohesion-principles folded in as an ordinary reference doc, per decision 5's retirement of the standalone-reference-skill type wherever a domain exists to host the content.

### Specialization by artifact (a second merge axis)

The merge direction above groups doers that **share a subject** (N operations on one subject = a domain). There is a second, orthogonal grouping axis: one subject **specialized along an artifact dimension**. Where merge-by-subject asks "do these skills operate on the same thing?", specialization-by-artifact asks "is this skill the general case, and that one a narrower case of the same artifact?"

The shape: a general artifact (e.g. `md` -- any LLM-facing markdown document) has specializations that are still that artifact *plus a contract* -- `skill` is-a `md` + the SKILL.md contract; `claude_md` is-a `md` + the CLAUDE.md contract. A domain organized along this axis routes by artifact specialization rather than by operation. (Historical example: pre-restructure, `md-authoring` indexed `skill-authoring` and `claude-md-authoring` as member sub-domains, one per artifact. `md-domain` now expresses the same specialization flatter -- as artifact columns in its verb x artifact dispatch table rather than as nested member domains -- with the general-`md` content living as the domain's own shared references, loaded by every lane.)

This composes with merge-by-subject rather than competing with it. A domain may group members along **either** axis -- by operation (technique/audit doers over one subject) or by artifact specialization (the same verb applied to narrower artifacts). The CRP gate is identical: each member must fire on a distinct sub-trigger (here, "which artifact") so a typical invocation loads the domain plus one member, not all of them.

### Broader union domains over sub-domains (union vs nest)

The earlier rule "a domain never nests" is a CRP rule, not a topology ban. A domain *member* may itself be a domain when the parent is a **broader union domain** -- a thin router that greets and argument-dispatches into one sub-domain at a time (the domain-layering pattern). The discriminator is what the parent does on invocation:

- **Union (allowed).** The parent is a thin greeting + argument-dispatch surface. Invoking it loads the router plus the *one* sub-domain the argument selects. CRP holds for the same reason every member merge does: each sub-domain fires on a distinct sub-trigger. The sub-domain may itself be a full domain-skill -- you still only load one at a time.
- **Nest (prohibited).** The parent force-co-loads its member domains' full content on every invocation. You wanted one sub-area and got all of them, two domain-indexes deep. This is the CRP-fail the "never nest" rule targets.

So the test is selective-dispatch vs co-load, not "is the member a domain". A broader union domain that routes to sub-domain members via greeting/argument-dispatch is the legitimate way to put a roof over several established domains without dissolving them.

**Two ways to back a sub-domain** (see `domain-layering.md`):
- **Reference sub-area** -- the sub-domain is a `references/*.md` doc inside the parent, declared as an `index.references[]` entry of the domain_skill contract (one declaration serves both the schema and the routing surface). Use when the sub-area is not a standalone skill.
- **Member skill** -- the sub-domain is a flat skill pointed at by `index.members[]` (member `type` may be `domain-skill`). Use when the sub-domain is a substantial standalone skill that already exists, or is itself a domain. The parent's routing behavior (greeting, argument-dispatch) is identical either way.

**Worked example:** `md-domain` applies the identical discipline one level down, at a dispatch table rather than a member-domain roster. Invoking `/md-domain` greets with a verb x artifact menu; `/md-domain audit skill <path>` dispatches into exactly the `audit_skill` lane and loads only that lane's procedure plus the `skill-standards.md` doc. Union, not nest: no dispatch ever co-loads two lanes or two standards docs. (The union-of-member-domains form this example once illustrated -- `md-authoring` routing to `skill-authoring` and `claude-md-authoring` as `index.members[]` -- was folded away in the md-domain restructure; the selective-dispatch discipline it taught survives as the dispatch table's own rule.)

### Visibility criterion for examples and anti-patterns

The L1/L2/L3 split above governs major content allocation. The same visibility decision recurs at the example/anti-pattern grain -- a single gotcha, a single anti-pattern record, a single escaping example. The criterion at that grain:

- **L1 (CLAUDE.md): if they are COMMON.** Example/anti-pattern fires in most sessions touching the area; the agent will need it ambient. Frequency dominates.
- **L2 (SKILL.md): if they are DIRECTLY RELATED to why the agent invokes the skill.** Even if also common, content that is literally the reason the skill exists belongs in SKILL.md. The skill's trigger surface is the right home for trigger-relevant content. Trigger-relevance dominates frequency when both fire.
- **L3 (references/): if they are ESOTERIC.** One-in-a-hundred edge cases, third-party-tool-specific quirks, environment-specific footguns most invocations never hit. Specificity dominates -- ambient cost is not justified, but the content must be reachable when the rare situation fires.

Worked example: a tool-wrapper skill for a shell language with quoting gotchas. An escape rule on the most common invocation form is BOTH common (most invocations of that shell) AND trigger-relevant (the wrapper skill exists precisely to handle escaping). Trigger-relevance wins -- it stays in the wrapper SKILL.md (L2). A rare quirk specific to one obscure cmdlet of that shell is L3. A cross-shell quoting collision the agent hits constantly across many tasks (regardless of which shell is the day's target) is L1.

Counter-worked-example: a tool-wrapper skill layering audit (April 2026) found 5 of 7 SKILL.md gotchas were also common across most sessions outside that tool's domain and should have been in CLAUDE.md (L1) -- frequency criterion fired and trigger-relevance did not. The audit surfaced that the framework named the levels but did not articulate the visibility-decision criterion at the example/anti-pattern grain; this section closes that gap.

## Portable typed units

A *typed unit* is a top-level YAML key with a registered schema. Skill-type units (`reference_skill`, `pattern_skill`, `technique_skill`, `discipline_skill`, `domain_skill`, `capability_skill`, `audit_skill`) are the per-skill contracts. *Portable typed units* are non-skill-type units that may appear in any of three layouts:

- as a sub-field of a skill-type unit (the original convention)
- as their own top-level unit in a separate fenced yaml block
- as one of multiple top-level keys sharing a fenced yaml block

All three are semantically equivalent; the schema validates each unit independently against its registered shape. Layout is the author's choice.

Today's portable unit registry:

| Unit root | Schema | Notes |
|---|---|---|
| `references` | `REFERENCES_SCHEMA` (list of `{id, path, keywords, summary}`) | May appear as `reference_skill.references` (nested) OR as a top-level `references:` block. Both validate. Used by reference-skill, domain-skill, capability-skill, and any document that wants a structured pointer list. |
| `facts` | `FACTS_SCHEMA` (list of fact records sharing `FACT_ITEM_RULE`) | May appear as `reference_skill.facts` (nested) OR as a top-level `facts:` block (or both -- the audit unions all sources). Cross-rules (>=1 fact carries gotchas, >=1 fact carries example, >=1 fact exists somewhere) are enforced at audit time across the union, not per-source. Used by reference-skill; permitted in other documents that want fact-shaped content. |
| `asset_dependencies` | `ASSET_DEPENDENCIES_SCHEMA` (list of `{path, consumer?, purpose, invariant?}`) | Declares repo files the skill consumes at RUNTIME -- see "Runtime asset dependencies and script contracts" below. May appear top-level or nested inside any skill-type unit. The per-skill audit resolves every declared `path` (and every `tools[].tests` path) against the skill dir, then the project root; an unresolved path is a FAIL. |

**Mixed-type drift** detection fires only on multiple skill-type roots across a document. Portable units coexist freely with any skill-type unit.

**Backward compatibility.** A skill that keeps `references:` nested inside its skill-type unit continues to validate without change. Migration to a separate block is optional, never required.

**Cross-block validation.** The audit walker collects every recognized typed unit across every fenced yaml block in a document and validates each. The walker does not partition validation by block; every unit in every block is in scope.

See the `content-authoring` reference's `typed_unit_composition` fact for the design rationale and worked encoding examples.

## Runtime asset dependencies and script contracts

A skill whose operative core is a bundled script (a workflow `.js`, a `scripts/*.py` helper) or whose tool-argument examples embed repo paths has load-graph edges the md-citation checks cannot see. Two declarations make them auditable:

**1. `asset_dependencies:` (portable unit) -- the runtime consumption edge.** Any repo file the skill consumes at runtime that is not resolved implicitly (its own `references/` are already covered by citation checks) gets one record: `path` (skill-dir-relative or project-root-relative), optional `consumer` (which bundled script/doc reads it), `purpose`, and optional `invariant`. Consuming a file owned by *another* skill is often the right design -- the asset stays with its CCP siblings and the consumer Reads it out-of-band -- but only the declaration makes the edge survive a reorg: the audit resolves every declared path, so moving or renaming the asset FAILs the consumer's audit instead of breaking silently at runtime.

**2. The `invariant` field -- the mirrors-style script contract.** When a bundled script must stay in sync with a doc section (e.g. "the workflow's output records mirror the 'Output schema' section of the funnel reference"), that contract belongs in the declaration, not in a code comment only the script's editor sees. The `invariant` string names the SSOT the script must track; the `path` it sits on is mechanically resolved, so the pointer cannot dangle. (The sync itself remains a judgment/test concern -- the declaration makes the obligation visible and its target resolvable.)

**Skill-shipped script tests.** Convention: a test for a skill-bundled script lives next to the script under the skill's `scripts/` (stdlib-only preferred, so it runs without the project's dev environment), and is declared on the tool record via the optional `tools[].tests` path (domain-skill `tools:` block). The audit resolves `tests` paths like asset paths. The project's test inventory (root CLAUDE.md commands block) names skill test roots alongside the main suite so "run the tests" enumerates the full set -- a skill-shipped test that only the skill's author knows about is not part of the inventory.

Instance example (validated by the corpus audit):

```yaml
asset_dependencies:
  - path: .claude/skills/project-design-domain/references/candidate-screening-funnel.md
    consumer: workflow.js
    purpose: runtime input -- the screening-funnel instrument the workflow lanes read
    invariant: workflow.js output records mirror the funnel's "Output schema (per game)" section
```

## Type contracts

A skill claiming a type must satisfy the **required** rows and the
**conditionally required** rows whose conditions hold. Glossary terms are
referenced by name; consult `glossary.md` for definitions.

Tables are kept for human review; the canonical machine-readable contract is in `plugins/skills-kit/skills_kit_lib/schema_registry.py` (schema literals in `skills_kit_lib/schemas/`). When the two diverge, the registry wins; the table gets updated to match.

### reference-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 example; >=1 gotcha |
| **Required patterns** | activation metadata, exclusion clause, in-skill examples, known gotchas, context efficiency |
| **Optional fact fields** | `category:` -- an optional cluster label per fact (string). When facts are ordered by category, a flat `facts:` list reads as conceptually grouped without requiring a separate `groupings:` block. The `groupings:` top-level block remains available for skills that prefer the macro-cluster shape with per-cluster keywords. |
| **Conditionally required patterns** | progressive disclosure -- CONSIDERED if SKILL.md body exceeds 500 lines or 3000 tokens (criterion: line/token count); REQUIRED only if a CRP-passing decomposition exists (sections serve different reading tasks). If no decomposition passes CRP, keep the larger SKILL.md rather than create a stub-plus-always-co-loaded reference; domain-specific organization -- IF reference content covers more than one mutually-exclusive sub-domain (criterion: are sub-domains independently loadable without cross-references) |
| **Prohibited patterns** | adversarial pressure testing, rule + counter pairs, workflow checklists |
| **Audit** | Drop a fresh agent into a topic the skill covers. Does it retrieve and apply the right fact? Are gotchas current? |

Examples: `/bootstrap` (plugins-kit) -- engine behavior reference, config schemas, remediation lookup tables. Generally: any skill that primarily collects facts, conventions, or syntax for retrieval.

### pattern-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; recognition criteria; counter-example(s); >=1 example |
| **Required patterns** | activation metadata, exclusion clause, explain-the-why, in-skill examples |
| **Prohibited patterns** | utility bundle, workflow checklist, rule + counter pairs |
| **Audit** | Does the agent recognize when to apply *and* when not? Counter-examples must be exercised. |

Examples: `flatten-with-flags`, `test-invariants`, `reducing-complexity` (from the obra/superpowers public skill set). The plugins-kit ecosystem currently has no pattern-skill -- see the audit-gap note in this directory's audit reports.

### technique-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 technique with ordered-step body (`steps:` list, min 1 step); >=1 gotcha OR >=1 anti_pattern record. Gotchas and `anti_patterns:` records are alternate containers for the same caution surface; state each caution once -- duplicating it across both (or across `steps[].detail` and either) is an anti-pattern: the reader pays twice. `output_template:` is an optional companion to `steps:` carrying the output-shape contract for the agent's reply -- not a substitute for steps. Even user-only slash-command skills reduce to a 1-step procedure ("invoke command; render output") and write that step explicitly. |
| **Required patterns** | activation metadata, exclusion clause, technique, known gotchas (satisfiable by gotchas OR anti_patterns records) |
| **Conditionally required patterns** | explicit step-tracking -- IF the technique has more than 3 steps (criterion: count `step` blocks; satisfied by EITHER a paste-able `- [ ]` checklist OR an explicit step-tracker invocation like `TaskCreate` or a scratch file at the start of the procedure -- the goal is the discipline of explicit step-tracking, not the markdown syntax; when on the YAML contract, the `steps:` list IS the canonical step-tracking surface and a parallel markdown checklist is not required); utility bundle -- IF the procedure has deterministic steps that would otherwise be regenerated each call (criterion: any step where output depends only on input); self-correcting loop -- IF the procedure produces output that can be programmatically validated (criterion: a validator script or rubric exists); plan-validate-execute -- IF the procedure has batch operations or irreversible side effects (criterion: any step that modifies external state at scale or is hard to undo) |
| **Prohibited patterns** | adversarial pressure testing |
| **Audit** | Can the agent apply the method to a novel scenario? Try variation and missing-information tests. |

Examples: `condition-based-waiting`, `root-cause-tracing` (from obra/superpowers); `/cache-report`, `/p4-code-review`, `/git-code-review` (plugins-kit, all `trigger: user-only`).

### capability-skill

Conceptually IS-A technique-skill: capabilities are techniques+ per the glossary. The schema requires capabilities: at root in place of technique-skill's techniques:, plus three capability-skill-specific blocks: external_capability, layering, and capability records carrying structural metadata (user_objective, operation, optional reference_section).

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; identity sentence; scope; external_capability declaration (kind: tool / mcp_server / api / service / ide / framework / harness + name + description); layering manifest (claude_md + skill_md + references lists declaring L1/L2/L3 content allocation); >=1 capability record (id + keywords + user_objective + operation + optional sub_cases / scope_axes / reference_section / inline steps / gotchas); >=1 capability-skill-level gotcha. **Optional `subdomain_config:`** at root for capability-skills with 2+ sub-areas -- one record per sub-area carrying optional `state_terms` / `operations` / `scope_axes` / `canonical_phrasing` / `llm_dependent_content` / `dependency_order` fields. See `subdomain-schema.md`. |
| **Required patterns** | activation metadata, exclusion clause, capability (each capability is a structured operation), known gotchas |
| **Conditionally required patterns** | members + Conditional Loading reference index -- IF capabilities grow into separate member skills (criterion: presence of `members:` block); aggregated capability surface listing each member's contribution -- IF members exist; companion declaration -- IF a wrapper sibling skill exists; progressive disclosure -- CONSIDERED if SKILL.md body exceeds 500 lines or 3000 tokens (criterion: line/token count); REQUIRED only if a CRP-passing decomposition exists (sections serve different reading tasks). If no decomposition passes CRP, keep the larger SKILL.md rather than create a stub-plus-always-co-loaded reference |
| **Prohibited patterns** | adversarial pressure testing (inherited from technique-skill); rule + counter pairs (capability-skills do not enforce rules under pressure); `techniques:` at root (capabilities: subsumes it); `index:` at root (members + Conditional Loading is the canonical shape) |
| **Audit** | Does the capability surface accurately enumerate the operations a user might invoke? Does the layering manifest match the actual content allocation across CLAUDE.md / SKILL.md / references/? Are capability records structured (user_objective + operation + optional metadata), not freeform prose? |

Examples: a skill wrapping a CLI tool (e.g. version-control mirror operations); a skill wrapping an MCP server's tool surface; a skill wrapping a third-party API for a specific project workflow; a skill wrapping the Claude Code harness's exposed surfaces (hook system, settings, MCP config, status line, slash-command runtime) with project-specific conventions -- declared with `kind: harness`. Harness-targeted skills are eligible when their content shape is capabilities-wrapping-an-external-thing; harness-targeted skills whose content is rules / lookup tables / techniques stay in their respective non-capability types and use the `harness-targeted: true` frontmatter flag for cross-cutting categorization. The plugins-kit ecosystem currently has skills that match this shape and will be re-classified as capability-skills in a follow-up audit.

### discipline-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 rule + counter pair; rationalization counter table |
| **Required patterns** | activation metadata, exclusion clause, adversarial pressure testing (applied to this skill's own rules -- the rationalization counter table must reflect observed agent failures, not hypothetical ones), rationalization counter table, red flags list, control tuning (low freedom), explain-the-why on rules |
| **Conditionally required patterns** | autonomy calibration -- IF the skill invokes specific tools whose autonomy scope matters (criterion: any `Bash` or external-tool invocation that should be pre-approved or restricted) |
| **Prohibited patterns** | high-freedom phrasing in rule statements; softening hedges that weaken a rule's core. Note: an exception clause that names a specific known legitimate case (e.g. "delete the user copy unless it intentionally diverges from the project version") is permitted -- the rule's core stays sharp and the exception is bounded. |
| **Audit** | Does the rule hold under combined pressures (time + sunk cost + fatigue)? Run an adversarial subagent. |

Examples: red/green/refactor discipline (write test first; rationalization counters for "too simple to test", "tests after achieve the same purpose", etc.) is the canonical reference shape for this type. The plugins-kit ecosystem currently has no discipline-skill.

### audit-skill (container)

A container type for quality-evaluation operations over a corpus, namespace, or stream. Audit-skill composes primitives borrowed from other types: criteria (reference-skill flavor), taxonomy (pattern-skill flavor), procedures (technique-skill flavor), remediations (technique-skill flavor), and optional enforcement (discipline-skill flavor). Its distinctive feature is the deterministic finding-classification step: every finding produced by an audit procedure routes to exactly one taxonomy category. Each category carries a `bucket` that is the **default disposition**; the audit's lane classifier assigns the FINAL per-finding disposition against explicit, fixed predicates (deterministic -- same finding, same disposition every run, so idempotency holds). Where an audit uses the four-disposition model (the md-artifact audits: FIX / SERIOUS / IMPROVE / SILENT, K -> SPECIAL) this enables parallel FIX (auto-applied) and IMPROVE (opt-in user-judgment) dispatch; the legacy AUTO / DISCUSS / SPECIAL lane names remain the structural `remediations` keys and are what references-audit still uses.

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; identity sentence; scope; subject declaration (what + subject_type from `single-file` / `corpus` / `namespace` / `stream`); >=1 criterion record (id + name + keywords + summary + severity + detail); >=1 taxonomy record (id + name + keywords + detection_signal + default_remediation + bucket); >=1 procedure record (ordered steps + gotchas); remediations dispatch (auto / discuss / special declared; auto and discuss may be empty lists; special always defined); >=1 audit-skill-level gotcha |
| **Required patterns** | activation metadata, exclusion clause, deterministic finding classification (every finding routes to one taxonomy category from a deterministic detection signal, never agent free-form judgment), disposition assigned per finding by FIXED classifier predicates (the taxonomy `bucket` is the default; the final disposition is deterministic, not runtime-improvised), FIX/AUTO remediations are mechanical (no user input required), IMPROVE/DISCUSS remediations surface options and wait for the user, SERIOUS is surfaced summarized and never auto-applied, SILENT is not surfaced, SPECIAL is the escape hatch for findings the taxonomy didn't anticipate |
| **Conditionally required patterns** | enforcement block -- IF audit findings gate downstream progress (criterion: any mention of findings blocking merges, CI, or submits); agent_template under remediations.auto -- IF AUTO remediation dispatches to a background agent (criterion: AUTO procedure invokes a sub-agent rather than running in-line) |
| **Prohibited patterns** | ad-hoc remediation (every remediation flows from a taxonomy category's default, not improvised per finding); mixed-concern procedures (an audit procedure that mutates the subject in the same pass as detection -- detection and remediation are separate phases); free-form disposition assignment (the final disposition follows FIXED classifier predicates, not runtime improvisation -- the taxonomy `bucket` is the deterministic default the predicates start from); open-ended taxonomy (every finding lands in a named category or SPECIAL -- no unclassified-by-omission); `techniques:` at root (procedures: subsumes it); `rules:` at root (criteria: is the audit's evaluable analog -- rules belong to discipline-skill); `patterns:` at root (taxonomy: embeds the pattern-shape inside the audit); `facts:` / `index:` / `members:` at root |
| **Audit** | Run the audit on a representative subject (single file, small corpus sample, or namespace). Verify: (a) detection signals are deterministic from the procedure's output, (b) every finding assigns to exactly one taxonomy category and one disposition via fixed predicates, (c) FIX/AUTO remediations run without agent reclassification, (d) IMPROVE/DISCUSS remediations surface options without overreach into autonomous action (and SERIOUS is surfaced, never auto-applied), (e) SPECIAL findings are genuinely unanticipated rather than lazy-classified, (f) re-running the audit after remediation confirms fix completeness without surfacing new findings from the fix itself. |

### Authoring note: YAML safety for taxonomy strings

Audit-skill SKILL.md files routinely embed inline-code syntax in taxonomy entries: backticks for slash-reference samples (`/example:skill`), skill-literal notation (`skill: "..."`), file paths, and code snippets. When lifting prose from reference documents into the audit-skill's YAML body (the `taxonomy:` list, `detection_signal`, `default_remediation`, `examples.before` / `examples.after`), these strings must be properly quoted. Plain (unquoted) YAML scalars fail to parse when they contain backticks: `found character '\`' that cannot start any token`. For short strings with embedded backticks, use double-quoted YAML strings: `detection_signal: "WARNING '/example:old-name'"`. Escape any internal `"` as `\"`. For longer multi-sentence text (e.g., remediation instructions), use the YAML folded block scalar `>-` to preserve readability while avoiding quote-escaping overhead. Example: a multi-sentence remediation transcription becomes `default_remediation: >- Mechanical find/replace ... If the sentence describes old behavior, also update prose ...` instead of a cramped double-quoted string. The goal is preserving the semantics from the source reference while adapting syntax to YAML's parsing rules.

### Composition story

The composition story: an audit-skill's per-artifact criterion-check loop -- "load criteria, check each, emit verdict for this artifact" -- is structurally a validation. Validation-as-shape lives inside audit-skills as a procedure-level pattern, not as a separate top-level skill type. When you see a skill that validates a single artifact against criteria, ask whether the natural scope is one artifact (audit-skill with subject_type single-file and a one-procedure body) or many (audit-skill with subject_type corpus and a scan procedure that runs the validation per artifact, classifies findings into the taxonomy, and dispatches remediations).

Examples: the `audit_references` lane (via `/md-domain audit references`) -- audits markdown corpus for broken skill cross-references; A--K taxonomy with detection signals per scanner output kind; AUTO/DISCUSS/SPECIAL dispatch routes findings to background-agent fixes or foreground user conversation. The `audit_skill` and `audit_claude_md` lanes (via `/md-domain audit skill` / `/md-domain audit claude-md`) -- single-artifact subject (or list-of-artifacts via the cwd-relative selection helper); taxonomy maps the framework's required-blocks / cohesion / hygiene check failures to remediation buckets.

### domain-skill (container)

A skill claiming this type must satisfy all five required-floor blocks. A
skill missing any of them is not a domain-skill -- it's a reference-skill
folder with friends.

| Contract | Items |
|---|---|
| **Required blocks (floor)** | SKILL.md file with frontmatter and trigger; identity sentence (one sentence stating what knowledge area this domain owns); companion declaration (explicit cross-references to sibling domains, or an explicit "no siblings"); orientation content (>=1 substantive section beyond the index -- vocabulary, pipeline overview, behavioral guardrails, or capability menu); reference index (Conditional Loading section listing every reference file with a keyword cluster) |
| **Conditionally required blocks** | sub-agent binding rule -- IF a paired `<skill-name>-a` agent exists (criterion: agent definition file present; convention codified in `domain-layering.md`); tool inventory -- IF the domain ships scripts (criterion: `scripts/` subdirectory present, or external tools cited in the SKILL.md); capability surface -- IF the domain has procedural operations the agent is expected to execute (criterion: any operation named in the SKILL.md that the agent invokes); vocabulary block -- IF reference files use canonical terms not defined in SKILL.md (criterion: scan reference files for repeated terms and check inline definition); output conventions -- IF the domain has format expectations for agent output (criterion: any consistent expected format like clickable links, YAML, etc.); behavioral guardrails -- IF the domain has known anti-patterns that have caused real failures (criterion: documented past failure modes; investigate-before-answering is the canonical guardrail when the domain spans 2+ data sources, see glossary `investigate_before_answering`); query-tool facade -- IF the domain wraps a structured catalog the agent or user references repeatedly (criterion: presence of a gazetteer / inventory / directory; convention codified in `../authoring-patterns/query-tool-pattern.md`); menu mechanic + sub-domain layering -- IF the domain has multiple sub-domains (criterion: count of distinct sub-areas; surface mechanics codified in `domain-layering.md`; sub-domains are declared ONCE, in the contract's `index:` block -- no parallel `sub_domains:` index); asset-dependency declaration -- IF the skill's bundled scripts or tool-argument examples consume repo files outside the skill directory (criterion: embedded repo paths in scripts / tool-args examples; declared via the `asset_dependencies:` portable unit, see "Runtime asset dependencies and script contracts") |
| **Required patterns** | activation metadata, exclusion clause, domain-specific organization, conditional details |
| **Conditionally required patterns** | sub-agent binding (the `agent-bundled` attribute) -- IF a paired sub-agent exists (same criterion as the binding-rule block); capability -- IF the domain has procedural operations (same criterion as the capability-surface block) |
| **Prohibited** | monolithic prose content -- meaty workflows, full reference text, or rules-with-counters belong in member skills (or in structured capability blocks), not in the container's prose; index without orientation -- a SKILL.md that contains only a conditional-loading list is routing without priming, not aggregating |
| **Audit** | Does a fresh agent dropped into the domain (a) operate fluently in vocabulary and conventions without re-orientation, (b) find and load the right member skill when a specific trigger fires, (c) recognize the boundary between this domain and its declared companions? Is the index complete relative to the actual member set on disk? |

Examples: `/ue-python-api` (plugins-kit) -- Unreal Editor automation domain with its own vocabulary, scripts, and reference set, paired with the `unreal-kit-a` agent.

## Instance examples

Minimal-valid instance blocks for the typed-unit schemas this document owns. Each block is the smallest legal instance of its root key: required fields only, list-length minimums met, no forbidden keys. The corpus audit (`check_schema_owner_docs_validate`) validates each block on every run, so these examples cannot silently drift from the schema.

Portable unit -- `references:` (a Conditional Loading entry list):

```yaml
references:
  - id: framework-doc
    path: skills/md-domain/references/skill-domain/framework.md
    keywords: [framework, contracts, type definitions]
    summary: Canonical per-type contracts for the skill-types framework.
```

Portable unit -- `facts:` (a flat list of reference-skill facts):

```yaml
facts:
  - id: schemas-are-floors
    summary: Per-type schemas declare the required minimum, not a ceiling.
    keywords: [schema floor, required minimum, extras allowed]
    detail: Authors may add load-bearing structured keys beyond what the schema enumerates; the schema enforces the floor.
```

Skill-type -- `reference_skill:` (minimal one-fact reference):

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

Skill-type -- `pattern_skill:` (one pattern with required sub-records):

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

Skill-type -- `technique_skill:` (one technique, ordered steps, required caution surface -- gotcha or anti_pattern record):

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

Skill-type -- `discipline_skill:` (rule + counter + pressure_test):

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

Skill-type -- `domain_skill:` (container with required floor blocks):

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
      - id: framework-doc
        path: skills/md-domain/references/skill-domain/framework.md
        keywords: [framework, type contracts, schemas as floors]
        summary: Canonical per-type contracts and the schemas-as-floors stance.
```

Skill-type -- `capability_skill:` (external capability + layering + capability record):

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

Skill-type -- `audit_skill:` (subject + criteria + taxonomy + procedures + remediations):

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
