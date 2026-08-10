# Skill standards -- what a good SKILL.md looks like

The artifact-keyed single source of truth for the `skill` artifact. Both
md-domain lanes read this file: the audit lane applies it as detection
criteria, the generation lane applies it as production targets. There is one
statement of each standard here; neither lane restates it.

The `skill` artifact has **two subject shapes**, and the `audit_skill` lane
audits both:

- the **SKILL.md** itself -- the contract root. Sections 1-9 below.
- a **skill reference document** (`*/skills/<name>/references/*.md`) -- an L3
  member of that skill. Section 10 below. The SKILL.md contract (frontmatter,
  the typed YAML block, the mechanical validator) does NOT apply to it; its
  own criteria do.

Everything in sections 1-9 is written about the SKILL.md subject unless it
says otherwise. Section 10 is self-contained and names the criteria it
inherits rather than restating them.

Vocabulary is not redefined here. Terms (Audience-Claude, CCP / CRP / ADP
/ SSOT, the skill types, the patterns, the attributes) live in
[`../skill-domain/glossary.md`](../skill-domain/glossary.md) and are
referenced by name. Placement across artifacts (which file a fact belongs
in at all) lives in
[`../cohesion-principles.md`](../cohesion-principles.md); this file starts
from "the fact belongs in a skill" and says what the skill must look like.

## 1. Artifact identity

- **Artifact:** `SKILL.md` -- one file per skill directory, carrying YAML
  frontmatter plus a markdown body.
- **Audience:** Claude. A SKILL.md is runtime context, not human
  documentation.
- **Specialization:** `skill` is-a `md` plus the SKILL.md contract (the
  frontmatter keys and a typed YAML contract block in the body).
- **Members:** `references/*.md`, `scripts/`, `workflow/`, `tests/`, and a
  co-located `CLAUDE.md`. The SKILL.md is the load-graph root; members are
  reachable from it, never the reverse. The `references/*.md` members are
  themselves audited subjects of this lane -- see section 10.
- **Load level:** L2. The skill loads when its description trigger fires;
  its references are L3 and load by name afterwards.

### The machine-authoritative floor

The **canonical** contract per skill type is the YAML schema declared in
[`../../../../skills_kit_lib/schema_registry.py`](../../../../skills_kit_lib/schema_registry.py)
(schema literals in `../../../../skills_kit_lib/schemas/`). Each skill
carries a `<type>:` YAML block in its SKILL.md body; the mechanical
validator (`python -m skills_kit_lib.audit`) validates that block against
its type's schema. A skill is well-formed when its YAML contract block
parses successfully against its type's schema.

Schema v1 was locked 2026-04-28, after all six then-existing plugins-kit
skills converted cleanly. Future schema changes ship as v2 alongside v1;
the validator dispatches on `_schema_version:` in each skill's YAML root.

**The registry wins on divergence.** The type-contract tables in section 6
are kept for human review. When a table and the registry disagree, the
registry is authoritative and the table gets updated to match; the schema
is never loosened to match an out-of-date table.

## 2. Universal standards (every skill, every type)

### 2.1 Description requirements

The frontmatter `description` is the only signal Claude uses to decide
whether to load a skill. Every skill, regardless of type, satisfies all
five rules. These are checked first (after the mixed-type check).

| id | Rule | Why |
|---|---|---|
| `length` | Length: <=160 characters. | A description that does not fit in 160 characters is summarizing capability rather than naming a trigger. |
| `form` | Form: directive. Open with "Use when..." or "Invoke when...". | Capability summaries ("Enables...", "Provides...", "Manages...") cause Claude to follow the description as a workflow instead of reading the body. |
| `condition` | Condition: clear and unambiguous. | The trigger must name a specific situation when invocation is the right move. Vague conditions ("when you need help with X", "for any X work") are evidence the skill is doing too much or has no real role. |
| `cost_justified` | Cost-justified. | Every skill load is tokens and a tool-call boundary. A trigger that fires on topical adjacency ("...for any Python work...", "...whenever you read code...") burns tool calls without bringing value. |
| `exclusion_clause` | Exclusion clause present: append a "Do NOT use for..." clause. | Positive triggers alone do not bound activation; the exclusion is what keeps adjacent skills from triggering each other's loads. |

### 2.2 Schemas are floors, not ceilings

The per-type YAML schemas validate the required minimum: which keys must be
present, what their structure must be, and which prohibited keys signal
cross-type drift. Authors may add load-bearing structured keys beyond what
the schema enumerates -- an `exceptions:` list inside an anti-pattern entry,
a `narration:` sub-block inside a technique, a
`false_positive_guardrails:` record inside a multi-agent review technique.
The schema enumerates the floor that must be there, not every key an author
may use. This follows from the structured-data bias (2.3): forbidding extras
would push authors to unstructured prose when they want legitimate structure
the schema did not anticipate.

**Mixed-type drift** is detected via the explicit `forbidden_keys:` list on
each schema (e.g. a `rules:` key inside `reference_skill:` is forbidden
because rules belong to discipline-skills). Forbidden keys are deliberate
cross-type signals; unknown keys not in the forbidden list are permitted.

**Promoted extension -- `anti_patterns`.** Applies to `technique_skill` and
`discipline_skill`; optional; record shape `[id, name, keywords,
why_it_seems_right, why_it_is_wrong, alternative]`. Anti-patterns are the
canonical example of "structure asserts": containment in a list of records
carrying these keys implicitly asserts every item is a real anti-pattern,
where a markdown bullet list carries no such assertion. On
`technique_skill`, `anti_patterns` also participates in the caution-surface
floor -- ">=1 gotcha OR >=1 anti_pattern record" satisfies the known-gotchas
requirement, the two being alternate containers for the same surface.
Duplicating one caution across both (or across `steps[].detail` and either)
is itself an anti-pattern: the reader pays twice.

### 2.3 Content-form choice

The default for LLM-facing content is structured YAML. Skills are runtime
context for Claude; structure aids comprehension and enables routing,
keyword matching, and validation that prose cannot. **When in doubt, bias
toward structured data.**

| id | Rule | Detail |
|---|---|---|
| `use_yaml_by_default` | Use YAML by default for LLM-facing content. | Records with the same shape (facts, rules, capabilities, steps, references, anti-patterns, gotchas), lookup tables, indexes, contract data, anything where keywords route per record. Structure carries assertions prose cannot. |
| `use_prose_only_when` | Use prose only when the content is naturally narrative or hierarchy carries no meaning. | (a) an identity sentence, an orientation paragraph, a single-paragraph explanation that does not decompose into discrete records; or (b) the hierarchy carries no meaning over prose. The bar for prose is "I can articulate why this would be worse as YAML." If you cannot articulate that, default to YAML. |
| `embedded_not_pure_yaml` | Embedded YAML in markdown, not a pure-YAML SKILL.md. | SKILL.md keeps a markdown wrapper -- title, identity sentence, brief orientation -- around fenced YAML blocks. Pure-YAML SKILL.md files are harder to skim during review and lose the orientation surface. The YAML carries the load-bearing contract; the markdown carries the priming. |

The earlier default ("if unclear, prose") was the wrong direction for an
Audience-Claude framework and is superseded: structure is the default,
prose is the documented exception.

### 2.4 The conditional-requirement grammar

A *conditionally required* row in a type contract specifies a pattern or
block that becomes mandatory when a stated condition holds. Every
conditional requirement carries an explicit, testable criterion -- without
one it is not auditable and does not belong in the contract.

The structure: **IF** *condition* (testable by *criterion*) **THEN**
*pattern or block is required*.

Worked instances of the grammar:

- **Progressive disclosure** is CONSIDERED when the body exceeds 500 lines
  or 3000 tokens; the split is REQUIRED only if it passes CRP (sections
  serve different reading tasks). Criterion: line/token count triggers the
  evaluation, per-section CRP decides. If no CRP-passing decomposition
  exists, the larger SKILL.md is the right answer. Anti-pattern: a SKILL.md
  trimmed to a thin pointer at a single reference always loaded next -- two
  file loads for one reading task.
- **Explicit step-tracking** is required when a technique has more than
  three steps. Criterion: count `step` blocks; satisfied by EITHER a
  paste-able `- [ ]` checklist OR an explicit step-tracker invocation
  (`TaskCreate`, a scratch file, or equivalent) at the start of the
  procedure. The goal is the discipline, not the markdown syntax.
  **Canonical form when on the YAML contract:** the `steps:` list IS the
  step-tracking surface -- structured, schema-validated, keyword-able,
  authoritative -- and a parallel markdown checklist creates
  two-source-of-truth drift and is not required. Shape hierarchy: YAML
  `steps:` for contract skills; markdown `- [ ]` for legacy / non-contract
  skills; mixed shapes promote to YAML and drop the markdown.
- **Sub-agent binding rule** is required when a paired sub-agent exists.
  Criterion: a matching `<skill-name>-a` agent definition exists.
- **Vocabulary block** is required when reference files use canonical terms
  not defined in SKILL.md. Criterion: scan reference files for repeated
  terms; check whether each is defined inline.

**No "recommended" category.** A conditional requirement without a testable
criterion is a recommendation, not a requirement. Patterns are either
required (always or under a stated condition) or prohibited. If a real
audit shows the strictness is wrong, the contract gets revised; the
framework does not accumulate fuzzy middle ground.

### 2.5 The two framework goals

- **Auditability** -- a skill is auditable when it can be evaluated against
  a defined contract and deficiencies identified mechanically. Without a
  contract a skill is just markdown and no objective standard exists.
- **Robustness** -- a skill is robust when it behaves consistently across
  calls and resists organic decay. Meeting a user's immediate goal is fast;
  producing a robust skill is a separate discipline that raises the floor.

## 3. Content allocation across CLAUDE.md / SKILL.md / references/

A separate-from-type standard governing how content is allocated across the
three load levels. Most acutely required for capability-skills (which have
ambient L1 territory because the wrapped external thing is used widely);
applies to any skill that has SKILL.md plus references/ plus relevant
project CLAUDE.md content.

| Level | Test | Lives here | Does NOT live here |
|---|---|---|---|
| **L1 -- CLAUDE.md** (ambient, always loaded) | Would the agent fail a tool call or violate a project convention without this in MOST sessions? | Common operations run constantly; project-specific syntax substitutions; tool-call gotchas that fail safety checks; hard prohibitions; one-line skill-discovery breadcrumbs. | Advanced procedures, deep mechanics, edge cases, syntax for rare operations. |
| **L2 -- SKILL.md** (triggered) | Would a fresh agent need this to navigate to the right reference doc once the trigger fires? | Identity sentence; brief orientation on the domain shape; capability surface; Conditional Loading index pointing at L3; behavioral guardrails spanning the domain. | Deep step-by-step procedures (L3); tool-specific syntax tables (L3); content already in CLAUDE.md (L1). |
| **L3 -- references/*.md** (on-demand) | Is this content only relevant when one specific advanced situation fires? | Step-by-step workflows for ONE operation; edge cases and error recovery; tool-specific syntax tables; worked examples; shared prerequisites extracted from multiple member skills. | Orientation (L2); ambient setup (L1). |

**The contested boundary, L1 vs L2, resolves on frequency of need + cost of
absence.** Frequent and tool-call-failing -> L1. Situational and
orientation-only -> L2. Specific and deep -> L3. For capability-skill this
allocation is required (the schema's `layering:` field declares the
manifest). For other types it is the default authoring guide; the schema
does not enforce it.

### 3.1 CRP is the test for L2 -> L3 splits

L2 and L3 are different load events: loading a reference is a second tool
call after the skill loads. Three principles are in tension: (1) loading
context the agent does not need is bad; (2) two tool calls where one would
suffice is bad; (3) there is a size beyond which a SKILL.md is too large to
keep monolithic.

**CRP resolves the tension:** if a reader loads one section, they should
plausibly need the rest. Sections serving different reading tasks -> the
split is legitimate. Sections always read together -> the split is
illegitimate; it manufactures a second tool call for content that always
co-loads.

Operational rule:

- The size threshold (>500 lines / >3000 tokens) signals that the skill
  DESERVES evaluation for a split. It is a signal, never a verdict.
- Enumerate the proposed sections; identify whether each fires on the same
  trigger or on independent sub-triggers.
- Split only when at least one section can be omitted on a typical
  invocation. The remaining SKILL.md must be a viable standalone for that
  case.
- If no decomposition passes CRP, keep the larger SKILL.md. An
  over-threshold SKILL.md costing one tool call beats a stub-plus-always-
  co-loaded reference costing two.

**Anti-pattern (CRP-fail split):** SKILL.md trimmed to a thin pointer at
one (or N) references that always load next. Symptoms: a short SKILL.md
(~30-100 lines) containing primarily Conditional Loading entries; every
reference loads every time the skill fires, with no sub-trigger selecting
between them. Revert by inlining the reference back into SKILL.md and
accepting the over-threshold size.

**CRP-pass split:** a domain-skill declaring N member sub-domains with one
Conditional Loading entry each. Each fires on a different sub-task, so a
typical invocation loads SKILL.md plus one reference; the second tool call
is paid only when actually navigating into the sub-domain.

### 3.2 Visibility criterion for examples and anti-patterns

The same visibility decision recurs at the example/anti-pattern grain -- a
single gotcha, a single anti-pattern record, a single escaping example:

- **L1 if COMMON.** The example fires in most sessions touching the area;
  the agent needs it ambient. Frequency dominates.
- **L2 if DIRECTLY RELATED to why the agent invokes the skill.** Even when
  also common, content that is literally the reason the skill exists
  belongs in SKILL.md. Trigger-relevance dominates frequency when both
  fire.
- **L3 if ESOTERIC.** One-in-a-hundred edge cases, third-party-tool quirks,
  environment-specific footguns most invocations never hit. Specificity
  dominates: ambient cost is not justified, but the content must be
  reachable when the rare situation fires.

Worked example -- a tool-wrapper skill for a shell with quoting gotchas. An
escape rule on the most common invocation form is BOTH common and
trigger-relevant; trigger-relevance wins, so it stays in the wrapper
SKILL.md (L2). A rare quirk specific to one obscure cmdlet is L3. A
cross-shell quoting collision hit constantly across many tasks is L1.
Counter-example: a tool-wrapper layering audit found 5 of 7 SKILL.md
gotchas were common across sessions OUTSIDE that tool's domain and belonged
in CLAUDE.md -- frequency fired and trigger-relevance did not.

## 4. Packaging standards -- one skill, several skills, or a domain

### 4.1 Compositional order

Authoring follows the type dependency graph, bottom-up:

1. **reference-skill, pattern-skill** -- atomic, no dependencies. Write
   these first.
2. **technique-skill** -- composes references and patterns into procedure.
3. **discipline-skill** -- wraps a target technique or pattern with rules
   plus counters.
4. **domain-skill** -- assemble only after the leaf members exist.

Anti-pattern: top-down domain-skill authoring, which tends to produce a
long monolithic SKILL.md that should have been five small files plus an
index. The domain-skill contract explicitly prohibits it.

### 4.2 The standalone reference skill is retired where a domain hosts it

**Standard.** Static reference text is not a skill when a domain exists to
host it. A skill earns its packaging by EXECUTING something -- running a
process, pulling dynamic information, dispatching a procedure. Content
whose whole job is to be read is a reference document of the domain that
consumes it, declared as an `index.references[]` entry, and it is packaged
that way rather than as a standalone `reference-skill` directory.

Consequences:

- When a body of static reference text has a consuming domain, fold it in
  as an L3 reference of that domain. Do not create, and do not preserve, a
  standalone reference-skill for it.
- The `reference_skill` type is NOT removed from the schema registry. It
  remains the correct contract for the *content shape* wherever that shape
  is legitimately standalone -- most importantly when no domain hosts it,
  and in the shared-reference case of 4.4 (a reference cited by 2+ sibling
  domains, which cannot fold into either without orphaning the other).
- The fold-in is a packaging move only. The content, its rules, and its
  citations are preserved; only the directory and its frontmatter go away.
  Incoming citations are re-pointed at the reference path in the same
  change -- a fold that leaves a dangling citation is a half-migration, not
  a fold.

**Worked example.** `cohesion-principles` was a standalone reference-skill
whose whole content is the placement framework (CCP/CRP/ADP, the load
graph, the per-artifact roles, the packaging razor). It executes nothing;
it is read. md-domain hosts every consumer of it -- both verb lanes and all
four artifact standards docs cite it. It therefore folds in as
[`../cohesion-principles.md`](../cohesion-principles.md), an
`index.references[]` entry of md-domain, and ceases to be a skill. Its
prior justification for staying standalone (two sibling domains,
md-authoring and md-audit, would orphan each other) expired when those two
domains merged into one.

### 4.3 When to consolidate skills into a domain (the merge direction)

`compositional_order` covers building a *new* domain bottom-up; the
mixed-type check (5.1) covers splitting *one* skill that outgrew its type.
This is the third, retroactive case: looking across a corpus at N existing
standalone skills and deciding whether they merge into one domain-skill.

A domain-skill is a container that routes among operations on one shared
subject. Consolidation is justified only when **both** hold:

1. **2+ skills share a subject** -- not co-location, not topical adjacency,
   not a shared pattern. The same *subject*. One skill is just a skill;
   never wrap a singleton in a domain.
2. **The skills are "doer" types.** Which type a skill is determines
   whether it merges as a member or folds in as supporting content:

| Type | Role in consolidation |
|---|---|
| technique / capability / audit | **Merge as members** -- they are operations over the subject; multiple operations on one subject *is* the domain. |
| reference / pattern / discipline | **Fold in, do not merge** -- knowledge, not operations. A reference becomes an L3 doc (and, per 4.2, stops being a skill); a pattern stays standalone (it applies across many subjects); a discipline becomes the domain's guardrails. None needs its own member sub-trigger. |
| domain | **May be a sub-domain member of a broader union domain** -- a thin parent that greets and argument-dispatches into one sub-domain at a time loads the router plus one sub-domain, so the top-level CRP test passes. What fails CRP is a *nest*: a parent that force-co-loads its member domains' full content. Union = selective dispatch (allowed); nest = co-load (prohibited). See 4.6. |

Consequences: *one doer + N references* is one skill with references, not a
domain. Skills sharing a **pattern** but operating on **different subjects**
are not a domain -- they reference one pattern-skill and stay independent.
Skills co-located in one **plugin** are not thereby a domain: a plugin is a
packaging unit, a domain is a subject unit, and they can diverge.

The merge passes CRP for the same reason an L2 -> L3 split does: each
member fires on a distinct sub-trigger, so a typical invocation loads the
container plus one member. If every candidate member would load on every
invocation it is a CRP-fail merge -- keep them separate.

**Corpus hook.** In a corpus inventory, cluster skills by subject and flag
any subject owning 2+ doer-type skills as a domain-consolidation candidate;
flag any domain-skill whose members all co-load as a CRP-fail to revert.

### 4.4 Shared references across sibling domains stay standalone

The merge table sends a *reference* to "fold in -> becomes an L3 doc" of
the domain it supports. That disposition assumes a **single** consuming
domain. When the same reference is cited by **2+ sibling domains** it
cannot fold into any one of them without the others losing it -- folding it
into domain A severs domain B's edge to it.

Such a reference stays standalone, exactly as a pattern-skill does, and
every consuming domain cites it. This is the cross-verb base case: a
reference that is the shared substrate of two domains is structurally a
pattern in the merge table's sense ("applies across many subjects") even
though it is typed reference-skill.

**Test:** if folding a reference into one domain would force a sibling
domain to reach across a domain boundary to read it, keep it standalone.
Note the converse, which is what 4.2 turns on: when the sibling domains
merge, the condition expires and the reference folds in.

### 4.5 Specialization by artifact (a second merge axis)

4.3 groups doers that **share a subject**. The orthogonal axis is one
subject **specialized along an artifact dimension**. Merge-by-subject asks
"do these skills operate on the same thing?"; specialization-by-artifact
asks "is this skill the general case, and that one a narrower case of the
same artifact?"

The shape: a general artifact (`md` -- any LLM-facing markdown document)
has specializations that are still that artifact *plus a contract* --
`skill` is-a `md` + the SKILL.md contract; `claude_md` is-a `md` + the
CLAUDE.md contract. A domain on this axis routes by artifact specialization
rather than by operation; the general-`md` content shared by every
specialization lives as the domain's own reference, inherited by each.

This composes with merge-by-subject rather than competing with it: a domain
may group members along **either** axis. The CRP gate is identical -- each
member fires on a distinct sub-trigger (here, "which artifact").

### 4.6 Broader union domains over sub-domains (union vs nest)

"A domain never nests" is a CRP rule, not a topology ban. A domain *member*
may itself be a domain when the parent is a **broader union domain** -- a
thin router that greets and argument-dispatches into one sub-domain at a
time. The discriminator is what the parent does on invocation:

- **Union (allowed).** The parent is a thin greeting + argument-dispatch
  surface. Invoking it loads the router plus the *one* sub-domain the
  argument selects. The sub-domain may itself be a full domain-skill.
- **Nest (prohibited).** The parent force-co-loads its member domains' full
  content on every invocation -- you wanted one sub-area and got all of
  them, two domain-indexes deep.

The test is selective-dispatch vs co-load, not "is the member a domain."

**Two ways to back a sub-domain** (mechanics in
[`../skill-domain/domain-layering.md`](../skill-domain/domain-layering.md)):
a **reference sub-area** (a `references/*.md` doc inside the parent,
declared as an `index.references[]` entry -- use when the sub-area is not a
standalone skill), or a **member skill** (a flat skill pointed at by
`index.members[]`, whose `type` may be `domain-skill` -- use when the
sub-domain is a substantial standalone skill or is itself a domain).

Sub-domains are declared ONCE, in the contract's `index:` block. A parallel
`sub_domains:` index is itself a finding: the two blocks carry
near-synonymous fields over the same files and drift.

## 5. Structural standards

### 5.1 Mixed-type check

Before classifying or running a contract, check whether the skill spans
multiple types. A skill containing rule-and-counter material *plus* lookup
tables, or how-to-procedure *plus* recognition criteria *plus* an
aggregation index, is mixed-type. Mixed-type skills are the most common
audit finding, because skills grow organically across type boundaries. The
remedy is splitting along those boundaries, not forcing the skill into one
type's contract: a mixed-type skill cannot pass any single type's contract
while it remains mixed.

Exception: a domain-skill's `orientation` may include a single
technique-flavored summary section without triggering the signal.

### 5.2 Portable typed units

A *typed unit* is a top-level YAML key with a registered schema. Skill-type
units (`reference_skill`, `pattern_skill`, `technique_skill`,
`discipline_skill`, `domain_skill`, `capability_skill`, `audit_skill`) are
the per-skill contracts. *Portable typed units* are non-skill-type units
that may appear in any of three layouts: as a sub-field of a skill-type
unit; as their own top-level unit in a separate fenced yaml block; or as
one of several top-level keys sharing a fenced block. All three are
semantically equivalent; layout is the author's choice.

| Unit root | Schema | Notes |
|---|---|---|
| `references` | `REFERENCES_SCHEMA` (list of `{id, path, keywords, summary}`) | May appear as `reference_skill.references` (nested) OR as a top-level `references:` block. Both validate. Used by reference-skill, domain-skill, capability-skill, and any document wanting a structured pointer list. |
| `facts` | `FACTS_SCHEMA` (list of fact records sharing `FACT_ITEM_RULE`) | May appear nested OR top-level (or both -- the audit unions all sources). Cross-rules (>=1 fact carries gotchas, >=1 fact carries an example, >=1 fact exists somewhere) are enforced across the union, not per-source. |
| `asset_dependencies` | `ASSET_DEPENDENCIES_SCHEMA` (list of `{path, consumer?, purpose, invariant?}`) | Declares repo files the skill consumes at RUNTIME (5.3). May appear top-level or nested inside any skill-type unit. The audit resolves every declared `path` (and every `tools[].tests` path) against the skill dir, then the project root; an unresolved path is a FAIL. |

**Mixed-type drift detection fires only on multiple skill-type roots across
a document.** Portable units coexist freely with any skill-type unit.

**Backward compatibility.** A skill keeping `references:` nested inside its
skill-type unit continues to validate. Migration to a separate block is
optional, never required.

**Cross-block validation.** The audit walker collects every recognized
typed unit across every fenced yaml block in a document and validates each.
Validation is not partitioned by block; every unit in every block is in
scope.

### 5.3 Runtime asset dependencies and script contracts

A skill whose operative core is a bundled script (a workflow `.js`, a
`scripts/*.py` helper), or whose tool-argument examples embed repo paths,
has load-graph edges the md-citation checks cannot see. Two declarations
make them auditable:

1. **`asset_dependencies:` -- the runtime consumption edge.** Any repo file
   the skill consumes at runtime that is not resolved implicitly (its own
   `references/` are already covered by citation checks) gets one record:
   `path` (skill-dir- or project-root-relative), optional `consumer`,
   `purpose`, optional `invariant`. Consuming a file owned by *another*
   skill is often the right design -- the asset stays with its CCP siblings
   and the consumer reads it out-of-band -- but only the declaration makes
   the edge survive a reorg: the audit resolves every declared path, so
   moving or renaming the asset FAILs the consumer's audit instead of
   breaking silently at runtime.
2. **The `invariant` field -- the mirrors-style script contract.** When a
   bundled script must stay in sync with a doc section, that contract
   belongs in the declaration, not in a code comment only the script's
   editor sees. The `invariant` string names the SSOT the script must track;
   the `path` it sits on is mechanically resolved, so the pointer cannot
   dangle. The sync itself stays a judgment/test concern.

**Skill-shipped script tests.** A test for a skill-bundled script lives next
to the script under the skill's `scripts/` (stdlib-only preferred) and is
declared on the tool record via the optional `tools[].tests` path
(domain-skill `tools:` block); the audit resolves `tests` paths like asset
paths. The project's test inventory names skill test roots alongside the
main suite -- a skill-shipped test only its author knows about is not part
of the inventory.

```yaml
asset_dependencies:
  - path: .claude/skills/project-design-domain/references/candidate-screening-funnel.md
    consumer: workflow.js
    purpose: runtime input -- the screening-funnel instrument the workflow lanes read
    invariant: workflow.js output records mirror the funnel's "Output schema (per game)" section
```

### 5.4 Load-graph coverage

Every file under `references/` carries an edge from SKILL.md (a citation or
an index entry); every content-bearing member directory (`tests/`,
`scripts/`, `templates/`, ...) is named somewhere in SKILL.md; and every
structured index path resolves on disk. References are **one hop deep** from
SKILL.md and must **not** cite SKILL.md sections -- a back-reference creates
a cycle and a context-loading hazard.

The judgment half is not mechanically decidable: whether an index entry's
keywords carry the terms a searcher would actually use. Test it by picking
each major contract the doc carries (its headings, entity names, script
names) and checking those exact terms against the entry's keywords.

### 5.5 Decision provenance stays out of SKILL.md

`Dec-N` entries, audit-finding logs, and decision history change with audits,
not with the skill's contract. Their home is the co-located `CLAUDE.md`. The
SKILL.md retains only the resulting rule, never the audit history that
produced it. This is always a CCP failure when violated: the content changes
for a different reason than the skill's contract.

### 5.6 YAML safety for embedded syntax

SKILL.md bodies routinely embed inline-code syntax in YAML values:
backticks for slash-reference samples, skill-literal notation, file paths,
code snippets. Plain (unquoted) YAML scalars fail to parse when they contain
backticks (`found character that cannot start any token`). For short strings
with embedded backticks use double-quoted YAML strings and escape internal
quotes; for longer multi-sentence text use the folded block scalar `>-`.
The goal is preserving the semantics of the source prose while adapting the
syntax to YAML's parsing rules.

## 6. Type contracts

A skill claiming a type must satisfy the **required** rows and the
**conditionally required** rows whose conditions hold. Glossary terms are
referenced by name; see
[`../skill-domain/glossary.md`](../skill-domain/glossary.md).

These tables are kept for human review. The canonical machine-readable
contract is
[`../../../../skills_kit_lib/schema_registry.py`](../../../../skills_kit_lib/schema_registry.py)
(schema literals in `../../../../skills_kit_lib/schemas/`). **When the two
diverge, the registry wins**; the table gets updated to match.

### reference-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 example; >=1 gotcha |
| **Required patterns** | activation metadata, exclusion clause, in-skill examples, known gotchas, context efficiency |
| **Optional fact fields** | `category:` -- an optional cluster label per fact (string). When facts are ordered by category, a flat `facts:` list reads as conceptually grouped without a separate `groupings:` block. The `groupings:` top-level block remains available for skills preferring the macro-cluster shape with per-cluster keywords. |
| **Conditionally required patterns** | progressive disclosure -- CONSIDERED if the SKILL.md body exceeds 500 lines or 3000 tokens (criterion: line/token count); REQUIRED only if a CRP-passing decomposition exists. If none passes CRP, keep the larger SKILL.md rather than create a stub-plus-always-co-loaded reference. Domain-specific organization -- IF reference content covers more than one mutually-exclusive sub-domain (criterion: are sub-domains independently loadable without cross-references) |
| **Prohibited patterns** | adversarial pressure testing, rule + counter pairs, workflow checklists |
| **Audit** | Drop a fresh agent into a topic the skill covers. Does it retrieve and apply the right fact? Are gotchas current? |
| **Packaging** | Per 4.2, this type is not packaged as a standalone skill where a domain exists to host the content; it becomes that domain's L3 reference. It stays standalone in the 4.4 shared-reference case. |

### pattern-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; recognition criteria; counter-example(s); >=1 example |
| **Required patterns** | activation metadata, exclusion clause, explain-the-why, in-skill examples |
| **Prohibited patterns** | utility bundle, workflow checklist, rule + counter pairs |
| **Audit** | Does the agent recognize when to apply *and* when not? Counter-examples must be exercised. |

### technique-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 technique with an ordered-step body (`steps:` list, min 1 step); >=1 gotcha OR >=1 `anti_patterns` record. Gotchas and `anti_patterns` records are alternate containers for the same caution surface; state each caution once. `output_template:` is an optional companion to `steps:` carrying the output-shape contract for the agent's reply -- not a substitute for steps. Even user-only slash-command skills reduce to a 1-step procedure ("invoke command; render output") and write that step explicitly. |
| **Required patterns** | activation metadata, exclusion clause, technique, known gotchas (satisfiable by gotchas OR `anti_patterns` records) |
| **Conditionally required patterns** | explicit step-tracking -- IF the technique has more than 3 steps (criterion + OR-form + YAML-canonical note: see 2.4); utility bundle -- IF the procedure has deterministic steps that would otherwise be regenerated each call (criterion: any step whose output depends only on input); self-correcting loop -- IF the procedure produces output that can be programmatically validated (criterion: a validator script or rubric exists); plan-validate-execute -- IF the procedure has batch operations or irreversible side effects (criterion: any step that modifies external state at scale or is hard to undo) |
| **Prohibited patterns** | adversarial pressure testing |
| **Audit** | Can the agent apply the method to a novel scenario? Try variation and missing-information tests. |

### capability-skill

Conceptually IS-A technique-skill: capabilities are techniques+. The schema
requires `capabilities:` at root in place of technique-skill's
`techniques:`, plus three capability-specific blocks: `external_capability`,
`layering`, and capability records carrying structural metadata.

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; identity sentence; scope; `external_capability` declaration (kind: tool / mcp_server / api / service / ide / framework / harness, plus name + description); `layering` manifest (claude_md + skill_md + references lists declaring L1/L2/L3 content allocation); >=1 capability record (id + keywords + user_objective + operation + optional sub_cases / scope_axes / reference_section / inline steps / gotchas); >=1 capability-skill-level gotcha. **Optional `subdomain_config:`** at root for capability-skills with 2+ sub-areas -- one record per sub-area carrying optional `state_terms` / `operations` / `scope_axes` / `canonical_phrasing` / `llm_dependent_content` / `dependency_order`; only `name` is required per record. See [`../skill-domain/subdomain-schema.md`](../skill-domain/subdomain-schema.md). |
| **Required patterns** | activation metadata, exclusion clause, capability (each capability is a structured operation), known gotchas |
| **Conditionally required patterns** | members + Conditional Loading reference index -- IF capabilities grow into separate member skills (criterion: presence of a `members:` block); aggregated capability surface listing each member's contribution -- IF members exist; companion declaration -- IF a wrapper sibling skill exists; progressive disclosure -- CONSIDERED over threshold, REQUIRED only if a CRP-passing decomposition exists |
| **Prohibited patterns** | adversarial pressure testing (inherited from technique-skill); rule + counter pairs; `techniques:` at root (`capabilities:` subsumes it); `index:` at root (members + Conditional Loading is the canonical shape) |
| **Audit** | Does the capability surface accurately enumerate the operations a user might invoke? Does the layering manifest match the actual content allocation across CLAUDE.md / SKILL.md / references/? Are capability records structured (user_objective + operation + optional metadata), not freeform prose? |

Harness-targeted skills are eligible when their content shape is
capabilities-wrapping-an-external-thing (`kind: harness`). Harness-targeted
skills whose content is rules / lookup tables / techniques stay in their
respective non-capability types and use the `harness-targeted: true`
frontmatter flag for cross-cutting categorization. The flag is taxonomic;
the type is structural.

### discipline-skill

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; >=1 rule + counter pair; rationalization counter table |
| **Required patterns** | activation metadata, exclusion clause, adversarial pressure testing (applied to this skill's own rules -- the rationalization counter table must reflect observed agent failures, not hypothetical ones), rationalization counter table, red flags list, control tuning (low freedom), explain-the-why on rules |
| **Conditionally required patterns** | autonomy calibration -- IF the skill invokes specific tools whose autonomy scope matters (criterion: any `Bash` or external-tool invocation that should be pre-approved or restricted) |
| **Prohibited patterns** | high-freedom phrasing in rule statements; softening hedges that weaken a rule's core. An exception clause naming a specific known legitimate case is permitted -- the rule's core stays sharp and the exception is bounded. |
| **Audit** | Does the rule hold under combined pressures (time + sunk cost + fatigue)? Run an adversarial subagent. |

### audit-skill (container)

A container type for quality-evaluation operations over a corpus,
namespace, or stream. Audit-skill composes primitives borrowed from other
types: criteria (reference flavor), taxonomy (pattern flavor), procedures
and remediations (technique flavor), optional enforcement (discipline
flavor). Its distinctive feature is the deterministic finding-classification
step: every finding routes to exactly one taxonomy category. Each category
carries a `bucket` that is the **default disposition**; the lane classifier
assigns the FINAL per-finding disposition against explicit fixed predicates,
so idempotency holds. Under the four-disposition model (FIX / SERIOUS /
IMPROVE / SILENT, K -> SPECIAL) this enables parallel FIX (auto-applied) and
IMPROVE (opt-in) dispatch; the AUTO / DISCUSS / SPECIAL lane names remain
the structural `remediations` keys.

| Contract | Items |
|---|---|
| **Required blocks** | SKILL.md file with frontmatter and trigger; identity sentence; scope; subject declaration (what + subject_type from `single-file` / `corpus` / `namespace` / `stream`); >=1 criterion record (id + name + keywords + summary + severity + detail); >=1 taxonomy record (id + name + keywords + detection_signal + default_remediation + bucket); >=1 procedure record (ordered steps + gotchas); remediations dispatch (auto / discuss / special declared; auto and discuss may be empty lists; special always defined); >=1 audit-skill-level gotcha |
| **Required patterns** | activation metadata, exclusion clause, deterministic finding classification (every finding routes to one taxonomy category from a deterministic detection signal, never agent free-form judgment), disposition assigned per finding by FIXED classifier predicates, FIX/AUTO remediations are mechanical (no user input required), IMPROVE/DISCUSS remediations surface options and wait for the user, SERIOUS is surfaced summarized and never auto-applied, SILENT is not surfaced, SPECIAL is the escape hatch for findings the taxonomy did not anticipate |
| **Conditionally required patterns** | enforcement block -- IF audit findings gate downstream progress (criterion: any mention of findings blocking merges, CI, or submits); `agent_template` under `remediations.auto` -- IF AUTO remediation dispatches to a background agent |
| **Prohibited patterns** | ad-hoc remediation; mixed-concern procedures (detection and remediation are separate phases); free-form disposition assignment; open-ended taxonomy (every finding lands in a named category or SPECIAL); `techniques:` at root (`procedures:` subsumes it); `rules:` at root (`criteria:` is the audit's evaluable analog); `patterns:` at root (`taxonomy:` embeds the pattern shape); `facts:` / `index:` / `members:` at root |
| **Audit** | Run the audit on a representative subject. Verify: (a) detection signals are deterministic from the procedure's output, (b) every finding assigns to exactly one taxonomy category and one disposition via fixed predicates, (c) FIX/AUTO remediations run without agent reclassification, (d) IMPROVE/DISCUSS remediations surface options without overreach (and SERIOUS is surfaced, never auto-applied), (e) SPECIAL findings are genuinely unanticipated rather than lazy-classified, (f) re-running after remediation confirms fix completeness without surfacing new findings from the fix itself. |

**Composition note.** An audit-skill's per-artifact criterion-check loop --
"load criteria, check each, emit a verdict for this artifact" -- is
structurally a validation. Validation-as-shape lives inside audit-skills as
a procedure-level pattern, not as a separate top-level skill type. When a
skill validates a single artifact against criteria, ask whether the natural
scope is one artifact (`subject_type: single-file`, one procedure) or many
(`subject_type: corpus`, a scan procedure running the validation per
artifact).

### domain-skill (container)

A skill claiming this type must satisfy all five required-floor blocks. A
skill missing any of them is not a domain-skill -- it is a reference folder
with friends.

| Contract | Items |
|---|---|
| **Required blocks (floor)** | SKILL.md file with frontmatter and trigger; identity sentence (one sentence stating what knowledge area this domain owns); companion declaration (explicit cross-references to sibling domains, or an explicit "no siblings"); orientation content (>=1 substantive section beyond the index -- vocabulary, pipeline overview, behavioral guardrails, or capability menu); reference index (Conditional Loading section listing every reference file with a keyword cluster) |
| **Conditionally required blocks** | sub-agent binding rule -- IF a paired `<skill-name>-a` agent exists; tool inventory -- IF the domain ships scripts (criterion: `scripts/` present, or external tools cited in SKILL.md); capability surface -- IF the domain has procedural operations the agent executes; vocabulary block -- IF reference files use canonical terms not defined in SKILL.md; output conventions -- IF the domain has format expectations for agent output; behavioral guardrails -- IF the domain has known anti-patterns that caused real failures (investigate-before-answering is the canonical guardrail when the domain spans 2+ data sources); query-tool facade -- IF the domain wraps a structured catalog referenced repeatedly; menu mechanic + sub-domain layering -- IF the domain has multiple sub-domains (declared ONCE in the contract's `index:` block -- no parallel `sub_domains:` index); asset-dependency declaration -- IF bundled scripts or tool-argument examples consume repo files outside the skill directory (5.3) |
| **Required patterns** | activation metadata, exclusion clause, domain-specific organization, conditional details |
| **Conditionally required patterns** | sub-agent binding (the `agent-bundled` attribute) -- IF a paired sub-agent exists; capability -- IF the domain has procedural operations |
| **Prohibited** | monolithic prose content -- meaty workflows, full reference text, or rules-with-counters belong in member skills or structured capability blocks, not in the container's prose; index without orientation -- a SKILL.md containing only a conditional-loading list is routing without priming, not aggregating |
| **Audit** | Does a fresh agent dropped into the domain (a) operate fluently in vocabulary and conventions without re-orientation, (b) find and load the right member skill when a specific trigger fires, (c) recognize the boundary between this domain and its declared companions? Is the index complete relative to the actual member set on disk? |

## 7. Audit direction -- criteria, taxonomy, dispositions

The audit lane ([`../lanes/audit-lane.md`](../lanes/audit-lane.md)) applies
the standards above as detection criteria against one SKILL.md at a time.
The criterion ids, taxonomy ids, severities, and buckets below are the
contract surface: they are stable identifiers referenced by the workflow
lanes, the rule catalog, the golden corpus, and any user-authored
`standards_set` overlay. **Do not rename them.**

Section 7 covers the SKILL.md subject only. The reference-document subject
has its own criteria and taxonomy in section 10; the two sets never run on
the same file.

### 7.1 Criteria (severity is the verdict weight)

| Criterion id | Standard | Severity | Detection |
|---|---|---|---|
| `required_frontmatter` | Required frontmatter fields present and well-formed: `name`, `description`, a valid `skill-type`; name conforms to the lowercase-hyphen pattern. | FAIL | Mechanical, `audit.py` universal-rules section. Missing or malformed frontmatter blocks all downstream analysis. |
| `description_quality` | 2.1 -- directive form ("Use when..." / "Invoke when..."), a "Do NOT use for..." exclusion clause contrasting neighbors, <=160 characters. | FAIL | Mechanical, `audit.py`. Vague or under-specified descriptions fail to route when the model picks among skills. |
| `yaml_contract_block` | The body carries a fenced yaml block with the declared skill-type's root key, and it validates against that type's schema (section 6 / the registry). | FAIL | Mechanical schema validator. Captures missing required keys, wrong types, forbidden cross-type keys, contract-floor violations. |
| `mixed_type_signal` | 5.1 -- exactly one canonical contract root key appears in the body YAML. | FAIL | Mechanical `detect_mixed_type_yaml`. Becomes a judgment finding when the orientation-summary exception applies. |
| `ccp_placement` | 3 / CCP -- SKILL.md content belongs there only when it changes with the skill's contract. Project-convention content belongs in the co-located CLAUDE.md. | JUDGMENT | Judgment per `cohesion-principles` `per_artifact_role.skill_md.audit_rules`: does this content change with the skill's contract or with project conventions? |
| `crp_placement` | 3.1 -- content in SKILL.md is read together; references/ load on-demand for distinct sub-tasks. Splitting must serve different reading tasks, not arbitrary size reduction. | JUDGMENT | Judgment. Body length is a signal that the split deserves evaluation, not a verdict. |
| `adp_back_reference` | 5.4 -- references are one hop deep from SKILL.md and must not cite SKILL.md sections. | FAIL | Partially mechanical (`audit.py` one-hop-deep check); judgment for back-reference detection inside reference body text. |
| `references_reachable_from_skill_md` | 5.4 -- every member is reachable from SKILL.md; every structured index path resolves. | FAIL | Mechanical `check_references_reachable_from_skill_md`. FAIL: a true orphan under references/, or an index/members path pointing at a missing file. JUDGMENT: a two-hop-only reference, a non-md references/ file with no edge, a member directory with no SKILL.md edge, or an index entry whose keywords omit a searcher's terms. |
| `decision_provenance` | 5.5 -- Dec-N entries, audit-finding logs, and decision history do not bleed into SKILL.md. | FAIL | Scan the body for Dec-N patterns or "audit-finding" / "decision log" markers. Always a CCP failure. |
| `hygiene_thresholds` | 3.1 -- body length above 500 lines or 3000 tokens is a signal to evaluate splitting. | INFO | Mechanical line/token count. Surfaces a CRP-evaluation prompt; never gates compliance on its own. |

### 7.2 Taxonomy (bucket is the default disposition)

Every finding routes to exactly one category. The `bucket` is the
deterministic default; the lane classifier assigns the final per-finding
disposition from fixed predicates.

| Taxonomy id | Name | Bucket | Detection signal |
|---|---|---|---|
| `A_missing_required_frontmatter` | Missing or malformed required frontmatter field | FIX | `audit.py` FAIL on a universal-rules row (frontmatter.name, frontmatter.description, skill-type value, name charset). |
| `B_description_quality` | Description fails directive-form / exclusion-clause / length checks | IMPROVE | `audit.py` FAIL on description length (>160), missing "Use when" / "Invoke when" prefix, or missing "Do NOT use for" clause. |
| `C_wrong_skill_type` | Declared skill-type does not match content shape | IMPROVE | `classify.py` suggested type differs from the declared type with single-type confidence >= 2. |
| `D_mixed_type_signal` | Multiple contract root keys or cross-type content drift | IMPROVE | `detect_mixed_type_yaml` returns >1 canonical root, OR the mixed-type heuristic score >= 2. |
| `E_schema_validation_failure` | Body YAML block fails schema validation | FIX | `audit.py` reports a contract validation failure: missing required key, wrong type, list below min_len, forbidden key present. |
| `F_ccp_misallocation` | CCP violation -- project-convention content in SKILL.md | IMPROVE | Judgment from `per_artifact_role.skill_md.audit_rules`: a body section changes with project conventions rather than the skill's contract. |
| `G_crp_violation` | CRP violation -- SKILL.md should split into references/ | IMPROVE | Judgment: body sections serve genuinely different reading tasks, body length is over thresholds, and a CRP-passing split exists. Offerable only with a NAMED extraction candidate; a bare over-threshold nudge is SILENT. |
| `H_adp_back_reference` | Reference doc cites its own SKILL.md sections | FIX | A back-reference to the owning skill inside a doc under that skill's references/. |
| `I_decision_provenance` | Dec-N entries or audit-finding logs in the SKILL.md body | FIX | Body contains Dec-N patterns, "audit-finding-N" tags, or decision-log entries dated by audit pass. |
| `J_hygiene_threshold` | Body over line / token threshold (CRP evaluation prompt) | IMPROVE | `audit.py` reports line count > 500 or token count > 3000. INFO severity -- a prompt to evaluate CRP, not a verdict. Escalates to G when a candidate is named; SILENT when none is. |
| `K_unclassified` | Unclassified / special case | SPECIAL | The finding matches no other detection signal after a deliberate attempt. |
| `L_load_graph_gap` | Load-graph edge gap -- a member exists but SKILL.md cannot route to it | IMPROVE | `audit.py` FAIL or JUDGMENT on `references_reachable_from_skill_md`: an orphaned references/ file, a member directory with no SKILL.md edge, a two-hop-only reference, or a dangling index path. Judgment extension: an index entry whose keywords omit the terms a searcher would use. A dangling index path with an identified correct target is a mechanical FIX; an accepted internal-helper orphan is SILENT. |
| `M_ancestor_convention_violation` | SKILL.md violates a convention an ancestor CLAUDE.md explicitly declares (H-11) | FIX | The SKILL.md violates a convention EXPLICITLY declared in an ancestor CLAUDE.md, with the declared rule quotable VERBATIM from that ancestor (no inferred or generic conventions). Fires only when the ancestor chain is supplied and non-empty. SERIOUS instead when the violation reveals a real-world problem the rule exists to prevent. |
| `N_user_standard_violation` | SKILL.md violates a user-authored standards criterion (`standards_set`) | SERIOUS | A criterion from a resolved `*-standards.md` governing the `skill_md` primitive is violated, with the statement quotable VERBATIM. Judgment criteria only -- mechanical criteria are `audit.py`'s job under `--config`. Suppressed when the criterion id is in `disabledCriteria`. Disposition follows the criterion's declared severity: fail -> SERIOUS, info -> IMPROVE, judgment -> JUDGMENT. Never auto-applied. |

### 7.3 Verdict

- Any FAIL finding -> **NON-COMPLIANT**.
- Only PASS / INFO / JUDGMENT findings -> **COMPLIANT** (judgment-required
  calls noted).
- INFO findings are advisory; they do not escalate to FAIL on later runs.
- Review mode (change-scoped): any *attributable* FAIL ->
  **NON-COMPLIANT**; otherwise **DIFF-CLEAN**. Non-attributable FAILs do not
  gate, but a non-attributable SERIOUS is still reported above the verdict.
- A file the lane declines (the artifact shape test does not match the
  `skill` artifact) -> **NOT-AUDITED**, plus an IMPROVE routing finding.

### 7.4 Standing audit anti-patterns

- **Audit-then-self-remediate.** Mixing detection and remediation in one
  pass invalidates the idempotency contract and lets the agent silently
  mutate the subject. Detection and remediation are separate phases; the
  re-run is the verification step.
- **Hygiene-as-verdict.** Treating a threshold breach as a FAIL. The
  threshold is a CRP-evaluation signal. Splitting a SKILL.md whose sections
  all serve one reading task is a tool-call doubling.
- **Re-implementing the mechanical validator.** `audit.py` is canonical for
  the mechanical rows; the lane consumes its JSON and adds the cohesion
  judgment on top.
- **Re-ranking findings between runs.** Criteria, taxonomy, and bucket
  assignments are fixed: the same input produces the same verdict.

## 8. Generation direction

The generation lane
([`../lanes/generation-lane.md`](../lanes/generation-lane.md)) applies these
same standards forward -- producing a compliant SKILL.md rather than
detecting an incompliant one. The standards are not restated for the
generation direction; only the order of application differs.

Order of application when generating or refining a SKILL.md:

1. **Placement first.** Confirm the content belongs in a skill at all --
   [`../cohesion-principles.md`](../cohesion-principles.md) decides WHICH
   file. 4.2 decides standalone skill vs reference of an existing domain;
   4.3-4.6 decide membership in a domain.
2. **Type second.** Pick the type from the content shape (section 6), then
   author bottom-up per 4.1. Run the 5.1 mixed-type check on the draft: a
   skill spanning two types splits along the boundary rather than being
   forced into one contract.
3. **Description third.** Write it against all five rules in 2.1. It is the
   only routing signal -- author it deliberately, do not derive it from the
   title.
4. **Content-form fourth.** Default to YAML per 2.3; keep the markdown
   wrapper. Where prose is chosen, be able to articulate why it would be
   worse as YAML.
5. **Allocation fifth.** Apply section 3 per fact and 3.2 per example or
   anti-pattern. Do not split on size alone -- 3.1 gates the split.
6. **Declare the edges.** Every member gets an index entry or citation
   (5.4); every runtime-consumed repo file gets an `asset_dependencies`
   record (5.3); keyword clusters carry the terms a searcher would use.
7. **Keep provenance out.** Decision history goes in the co-located
   CLAUDE.md (5.5), never in the SKILL.md.
8. **Validate mechanically.** Run `python -m skills_kit_lib.audit <path>`
   from the plugin root. Zero FAILs is well-formed -- but the schema is a
   floor (2.2), so a clean run is the starting bar, not the finish line; the
   judgment criteria in 7.1 (`ccp_placement`, `crp_placement`) still apply.

Two generation norms that fall out of the standards: **schemas are floors
when generating too** -- add load-bearing structured keys freely (2.2)
rather than dropping to prose because the schema does not enumerate the key
you need; and **verify every verbatim command** -- a command written into a
SKILL.md is executed by an agent as written, so run it before shipping (see
[`../skill-domain/example-verification.md`](../skill-domain/example-verification.md)).

## 9. Minimal-valid instance blocks

The minimal-valid instance blocks -- the smallest legal instance of each
skill-type root key, plus the portable `references:` and `facts:` units -- live
in [`../skill-domain/schema-fixtures.md`](../skill-domain/schema-fixtures.md).
That file is the md-domain tree's `owner_doc` target for the schemas registered
via `schema_registry.py`: the mechanical check `check_schema_owner_docs_validate`
validates every block there against its schema on each run, so the fixtures
cannot drift from the contracts stated above. The fixtures exist for that
mechanical check, not for reading the standards -- nothing in sections 1-8
depends on them.

## 10. Skill reference documents (`references/*.md`)

The second subject shape of the `skill` artifact. A **skill reference
document** is a markdown file under a skill directory's `references/`
folder (`*/skills/<name>/references/*.md`, at any nesting depth inside it).

- **Audience:** Claude, same as the SKILL.md.
- **Load level:** L3. It loads by name after its owning SKILL.md has already
  fired, for ONE specific situation (section 3).
- **Contract:** none. A reference document carries no frontmatter, no typed
  YAML block, and no schema. It is judged on its PROSE.

**Boundary.** These criteria judge the document. They do NOT judge the code,
tool, or system the document describes -- reading source is admissible only
to verify a claim the document already makes (SR-2, SR-3). A defect found in
the described system is a code-review finding and belongs to the code
reviewer, not to this lane.

### 10.1 What the lane does NOT apply to a reference document

Stated explicitly, because half of section 7 looks applicable and is not:

| Not applied | Why |
|---|---|
| `required_frontmatter`, `description_quality`, `yaml_contract_block`, `mixed_type_signal`, `hygiene_thresholds` | All are SKILL.md contract rows. A reference has no frontmatter and no type, and a reference being long is the point of L3. |
| The mechanical validator (`skills_kit_lib.audit`) | Its subject is a SKILL.md or a CLAUDE.md. Do not run it on a reference; do not emit a "validator unavailable" finding for one either. |
| `ccp_placement`, `crp_placement` | The L2 -> L3 split decision belongs to the owning SKILL.md's audit, which sees both sides of it. A reference-document audit does not re-litigate whether the reference should exist. |
| `references_reachable_from_skill_md` | The reachability edge is owned by the SKILL.md subject (5.4); auditing the reference alone cannot see the index. |
| `decision_provenance` | That row is about provenance leaking into a SKILL.md. Provenance inside a reference is SR-4. |

### 10.2 Criteria that already cover this shape -- referenced, not restated

These run on any subject the lane audits, reference documents included. They
are named here so section 10 does not grow a second copy of them:

- **Non-ASCII look-alikes, hardcoded absolute / foreign-machine paths, drifted
  line numbers** -- the built-in universal-convention FIX in the detect lane's
  disposition classifier.
- **A convention an ancestor CLAUDE.md declares verbatim** (ASCII-only
  mandates, temporal-deixis bans, path rules) -- criterion H-11, taxonomy
  `M_ancestor_convention_violation`, with the same verbatim-quote posture and
  the same scoped-exception carve-out.
- **A user-authored `standards_set` criterion** governing this artifact --
  taxonomy `N_user_standard_violation`.
- **A back-reference to the owning SKILL.md's sections** -- criterion
  `adp_back_reference` (5.4), taxonomy `H_adp_back_reference`, applied with
  the reference as subject rather than reached through the SKILL.md.
- **Broken OUTBOUND links and dead cross-references across the corpus** --
  the `audit_references` lane. Do not re-derive its scan here.

### 10.3 Criteria

Four, all about prose. Each is required; there is no recommended tier (2.4).

#### SR-1. Inbound anchor integrity

**Rule:** an inbound citation that would BREAK must not break. Two citation
forms break: an **anchor link** into this document (`<path>#<anchor>`), and a
citation that quotes one of this document's headings **verbatim**. Both must
resolve to a heading present under that exact name.

**Not a violation, and this is the load-bearing half:** an informal prose
pointer that names a section approximately -- different case, a prefix, a
paraphrase, a bolded inline label rather than a heading -- and resolves
unambiguously to exactly one place in the document. Nothing is broken and no
reader is misled, so grading it is noise. A pointer that is genuinely
AMBIGUOUS (it could mean two sections, or none) IS a violation, at severity
JUDGMENT.

**Test:** collect the document's headings. Search the owning skill directory,
then the wider repo where the search is cheap, for citations naming this
document. Classify each citation by form before judging it. In review mode the
pre-image makes the FAIL case direct: a heading present in the pre-image and
absent now, with at least one inbound anchor link or verbatim quote naming it,
is an attributable violation. Anchor the finding on the CITING file and line,
and name the current heading as the correct target -- a violation is not
raisable without both halves.

**Severity:** FAIL for a broken anchor link or a broken verbatim heading
quote; JUDGMENT for an ambiguous prose pointer. **Taxonomy:**
`O_broken_inbound_anchor`, bucket FIX (the correct target is identified, so
the citer update is mechanical). Bucket IMPROVE when the heading was deleted
outright and no successor can be named.

#### SR-2. Internal consistency

**Rule:** the document does not contradict itself. Two passages must not
assert mutually exclusive things about the same fact, and an example must not
violate a rule the same document states.

**Test:** for each load-bearing claim -- an imperative, a guarantee, a bound,
a count, a name -- check whether another passage asserts its negation, a
different value, or an instance that breaks it. The finding MUST quote both
passages with line numbers; a one-sided suspicion is not an SR-2 finding.

**Severity:** FAIL. **Taxonomy:** `P_internal_contradiction`, bucket IMPROVE
-- the auditor usually cannot decide which side is true, and picking wrong
writes a confident falsehood over a visible conflict. It is FIX when one side
is FALSIFIED by a verified reading (the classifier's standing rule for
falsified content), and SERIOUS when the contradiction is about a protective
mechanism, since one of the two readings leaves an invariant unguarded.

#### SR-3. Claim calibration

**Rule:** a claim about behavior is stated at the strength its basis
supports. An unhedged universal or guarantee -- "always", "never", "every",
"cannot", "guaranteed", "impossible", "silently handles" -- about how a
system, tool, or command behaves requires a basis the reader can reach: a
named source (a path, a test, a command to run), or the mechanism that makes
it true, stated in the document. Otherwise the claim carries the
qualification its actual basis supports.

**Test:** locate universal or guarantee phrasing about runtime behavior. For
each, ask what in the document establishes it. No basis and no hedge is a
violation, anchored on the claim.

**Scope guard, load-bearing -- THREE genres, only the first is in scope:**

1. A **claim** reports what the system DOES. "This file can never drift." In
   scope.
2. An **instruction** tells the reader what to do. "Never hand-edit this
   file." Out of scope -- the document is entitled to state a rule
   absolutely.
3. A **normative design principle or declared invariant** states what the
   system MUST hold, as a rule it holds itself to rather than a report of
   observed behavior. "A pipeline never wildcard-adds"; "every stochastic
   decision is seeded deterministically". Out of scope. A document whose
   declared genre is principles (a Principle / Why / Embodied-by structure,
   an "invariants" section) is made of these, so treating them as claims
   makes the whole document a finding.

Genres 2 and 3 are the criterion's two measured false-positive modes. When a
declared invariant is contradicted by the document's own text, that is SR-2,
not SR-3.

**Consequence bar.** Even inside genre 1, raise it only when a reader who
believed the claim AS STATED could act wrongly -- rely on a guarantee that
does not hold, or skip a check the claim says is unnecessary. A rhetorical
universal inside an argument, where the argument survives the qualification,
is not a finding: the remediation would be cosmetic and the reader was never
going to be misled. This bar is what separates the two measured survivors of
the genre guard from a real overstatement.

**Severity:** JUDGMENT. **Taxonomy:** `Q_overstated_claim`, bucket IMPROVE.

#### SR-4. Reader fit -- L3 guidance, not a maintainer record

**Rule:** the document's content is what its READER needs when the situation
that loads it fires. Material whose only reader is someone maintaining the
document's own PRODUCTION PIPELINE belongs elsewhere: decision provenance and
derivation history (5.5 -- the co-located CLAUDE.md), regeneration
instructions naming a tool the reader's install does not contain, and
generator plumbing colocated with the artifact for build convenience.

**Scope guard:** guidance addressed to someone maintaining the SYSTEM the
document describes is content the reader needs, not maintainer-only material.
Only the document's own production pipeline is in scope. Reading the Rule
sentence more broadly than the Test is this criterion's measured
false-positive mode.

**Test:** per section, name the reader and the situation that loads it. A
section whose only reader is someone editing the document's own production
pipeline is a violation. The sharpest signal is an instruction the reader
cannot execute -- a header telling them to re-run a script that does not
exist on their machine.

**Severity:** JUDGMENT. **Taxonomy:** `R_maintainer_only_material`, bucket
IMPROVE (a relocation, never a silent deletion -- the content has a correct
home).

### 10.4 Taxonomy

| Taxonomy id | Name | Bucket | Detection signal |
|---|---|---|---|
| `O_broken_inbound_anchor` | A cited heading no longer exists under the cited name | FIX | SR-1: an inbound ANCHOR LINK or VERBATIM heading quote names a section absent from the current document (FAIL), or a prose pointer is ambiguous (JUDGMENT). FIX when a successor heading is identifiable, IMPROVE when none is. An unambiguous informal pointer is not a finding. |
| `P_internal_contradiction` | Two passages of the document contradict each other | IMPROVE | SR-2: two quotable passages assert mutually exclusive things about one fact. FIX when one side is falsified by a verified reading; SERIOUS when the contradiction concerns a protective mechanism. |
| `Q_overstated_claim` | A behavioral claim is stated beyond its basis | IMPROVE | SR-3: an unhedged universal or guarantee about behavior with no reachable basis. Never fires on an instruction to the reader. |
| `R_maintainer_only_material` | Maintainer-only material on a reader-facing surface | IMPROVE | SR-4: a section whose only reader is someone editing the document's production pipeline -- derivation logs, regeneration headers naming absent tools, generator plumbing. |

The lettered ids continue the section 7.2 sequence and are scoped to this
lane, exactly like `A_*`..`N_*`. The shared ids that also fire on this shape
(`H_adp_back_reference`, `M_ancestor_convention_violation`,
`N_user_standard_violation`, `K_unclassified`, `none`) keep their section 7.2
meanings.

### 10.5 Verdict

Identical vocabulary and identical rules to 7.3, over the criteria above:
any FAIL -> `NON-COMPLIANT`; only PASS / INFO / JUDGMENT -> `COMPLIANT`;
review mode -> any attributable FAIL -> `NON-COMPLIANT`, else `DIFF-CLEAN`.

A file that is neither a `SKILL.md` nor a `*/skills/*/references/*.md` ->
`NOT-AUDITED` plus the routing finding, per the decline contract in
[`../lanes/audit-lane.md`](../lanes/audit-lane.md) step 2a. The shape test
now admits both subject shapes; a `CLAUDE.md` and a standalone project
document are still declined.
