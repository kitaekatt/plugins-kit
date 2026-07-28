# Audit framework

How skills-kit audits things. This is the canonical glossary for audit-related skills; the operational skills that consume it -- `skill-audit` (skill-shape audits over the User + Project + Plugins corpus; via `/md-audit skill`), `claude-md-audit` (CLAUDE.md cohesion/hygiene; via `/md-audit claude-md`), `project-doc-audit` (standalone project-document cohesion/placement; via `/md-audit project-doc`), and `references-audit` (cross-reference and orphan audits over markdown corpora; via `/md-audit references`) -- each reference it. When a term defined here appears in a member skill, the definition lives here; the skill describes only how the audit applies the term.

## Composition with `skills-kit:skill-authoring`

skill-authoring defines the `audit_skill` schema -- the structural shell every audit-skill must fill (`identity`, `scope`, `subject`, `criteria`, `taxonomy`, `procedures`, `remediations`, `enforcement`, `anti_patterns`). This audit framework provides the **vocabulary and registry** those fields are populated against. The two compose: the schema is what an audit-skill IS, the framework is what auditing it MEANS. Where they would overlap -- a term like `subject` or `taxonomy` appears in both -- the framework owns the definition; the schema owns the structural slot.

The canonical contracts of individual rules also live in each audit-skill's SKILL.md `criteria:` block, not in the framework. The framework only catalogs which audit-kinds exist and which rule ids they bind to which compositions; the rule's severity, summary, and detail stay in the owning SKILL.md (single source of truth per rule, no duplicated drift).

## Data side

The data side of the framework -- the primitives, compositions, and audit-kind registry -- lives at `audit-framework.yaml` alongside this doc. The glossary below names the concepts; the YAML names the instances. When scripts need machine-readable structural facts (what counts as a skill, what files a plugin contains, which audit-kinds bind which rules), they load the YAML. This file is the human-review surface; the YAML is authoritative on divergence, mirroring the `framework.md` / `schemas.py` split in skill-authoring.

## Glossary

### subject

The thing being audited. A subject is either a primitive (a single file) or a composition (a directory aggregating primitives). Every audit declares its subject up front -- without it, neither rules nor findings can be bound to anything.

### subject_type

The cardinality axis of an audit-skill's subject, declared in the SKILL.md's `audit_skill.subject.subject_type` field. Two values today: `single-file` (the audit evaluates one subject per invocation) and `corpus` (the audit operates over a discovered set, like skill-audit over the User + Project + Plugins skill pool). `subject` (kind) and `subject_type` (cardinality) are orthogonal: skill-md-audit has `subject_type: corpus` but each rule application targets one `skill_md` primitive at a time inside a `skill` composition.

### procedure

A named operation declared in an audit-skill's `audit_skill.procedures` block. An audit-skill hosts:

- **One+ findings-bearing procedure** -- the namesake audit operation. Runs the scaffolding, classifies findings into the taxonomy, assigns each a disposition (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL -- legacy AUTO / DISCUSS / SPECIAL for references-audit).
- **Zero+ supporting procedures** -- inventory or report procedures over the shared subject (e.g. skill-audit's `roster` and `hierarchy`). Share the subject; do not exercise the findings/remediation machinery.

Procedures within a skill share the subject but do not have to share rules; the framework permits multiple distinct audit-kinds inside one audit-skill if their procedures share a meaningful subject.

### primitive

An atomic, identifiable content kind -- a Markdown file, a YAML file or fenced block, a JSON manifest, a script -- optionally refined into sub-kinds (`md` splits into `skill_md` / `reference_doc` / `claude_md` / `plain_md`). The registered instances, each with its detection rule and sub-kinds, live in `audit-framework.yaml::primitives`; this doc does not restate the list. Adding a primitive means declaring a detection rule and the sub-kinds that matter in that registry.

### composition

A directory structure that aggregates primitives (and possibly other compositions) under a named rule set. Each declares a marker (e.g. a `SKILL.md` at the root marks a `skill`; `.claude-plugin/plugin.json` marks a `plugin`), what it contains, and what it nests inside. Compositions nest: a plugin contains skills; a project contains plugins. The registered instances -- with markers, `contains` / `nests` specs, and discovery priorities -- live in `audit-framework.yaml::compositions`; this doc does not restate the list.

### discovery

The act of finding subjects in scope. Two sub-shapes:

- **Tree-walk discovery** -- walking a scan tree and toggling rule sets per discovered composition. When the walker enters a directory, it checks markers in registered priority order (each composition's `discovery_priority` in `audit-framework.yaml`) and activates that composition's rules for the subtree. Rules layer rather than override.
- **Corpus discovery** -- enumerating a known namespace (the User + Project + installed-Plugin skill pool, via `skills_kit_lib.corpus`). Returns a flat list of subjects without rule toggling.

Discovery is its own scaffolding (e.g. `discover.py`, `skills_kit_lib.corpus`) -- separate from the evaluator scaffolding that applies rules. Discovery answers "what subjects?"; the evaluator answers "what findings on this subject?".

### audit-kind

A named audit, declared by:

- which **primitives** it consumes (which file kinds it parses)
- which **compositions** it traverses (where its rules apply)
- which **rules** it applies per composition (the bindings table)
- which **taxonomy** it uses to categorize findings

The registered audit-kinds -- with their consumed primitives, traversed compositions, and exact rule-id bindings -- live in `audit-framework.yaml::audit_kinds`; this doc does not restate the roll-call (a restated list here has drifted from the registry before). The framework is open under addition: a new audit-kind declares its primitives, compositions, rule bindings, and taxonomy in `audit-framework.yaml`. No framework-side code change is needed beyond the registry entry.

### rule

A single check that takes a subject and returns one of: PASS, FAIL, JUDGMENT, INFO. Rules are deterministic where possible (mechanical schema checks, regex scans, manifest reads); judgment rules return JUDGMENT and surface the question to the agent. A rule does not classify or remediate -- it only detects.

### finding

A rule's output, bound to a subject location. Every finding carries: rule id, subject path (file + optional line), severity, and a one-line message. Findings are the input to taxonomy classification; they are not consumed directly by remediation.

### severity

The intrinsic weight of a rule outcome, independent of remediation strategy. Three levels:

- **FAIL** -- gates compliance. The subject is NON-COMPLIANT until resolved.
- **JUDGMENT** -- the rule cannot decide mechanically; the agent or user decides. Does not gate compliance.
- **INFO** -- advisory only. Surfaces a signal worth knowing about (e.g. a size threshold breached); never gates compliance and never escalates to FAIL on re-run.

Severity belongs to the rule, not to the finding. A rule does not return FAIL on Monday and INFO on Tuesday; the level is part of the rule's contract.

### taxonomy

A per-audit-kind categorization of findings into remediation-shaped groups (typically labeled A, B, C... K). Each category names a detection signal (which rule output matches it), a default remediation, and a bucket. Two audits may share severity levels and rule shapes while having entirely different taxonomies -- the taxonomy is the audit-kind's remediation vocabulary, not a shared cross-audit concept.

### bucket

A category's DEFAULT disposition (a starting point); the final per-finding disposition is assigned instance-level by the audit's detect/classify lane against explicit predicates. The md-artifact audits (skill-md, claude-md, project-doc) use the **four-disposition model**:

- **FIX** -- decidable by a verified fact plus a documented convention; auto-applied and lands in a reviewable CL (a correction against a verified fact, deletion of falsified content, a convention-violation fix, dedup under the summarize-and-reference rule, a default/obvious trim). The bar: would a reasonable owner accept this diff in CL review without discussion?
- **SERIOUS** -- surfaced summarized at the TOP of the report, never auto-fixed, never buried: secrets, a protective rail whose documented mechanism is fictional (the real finding is the unprotected invariant), or a doc problem that reveals a real-world problem.
- **IMPROVE** -- reported as a count + one-line pitches; discussion is opt-in. Structural moves (graduate / fold / absorb / split / orphan-link / placement) and trims of true content that pass the one-line test. Declined IMPROVEs are recorded per-file (`md-audit-declined:` frontmatter) so re-audits do not re-pitch them.
- **SILENT** -- not surfaced at all, no hedging: do-nothing conclusions, validator detection artifacts, accepted structural patterns (agent-definition files with zero inbound citations, historical records, companion-source PDFs).
- **SPECIAL** -- not one of the four: the category-K escape hatch, when the finding did not fit any other category and the user proposes a strategy.

Report contract: SERIOUS (summarized, top) -> FIX (applied count + CL) -> IMPROVE (count + one-liners, opt-in); SILENT omitted. FIX and IMPROVE dispatch in parallel; the user's foreground IMPROVE answers do not gate the background FIX edits; both merge at the end.

The **legacy AUTO / DISCUSS / SPECIAL** lanes (mechanical / judgment-required / escape-hatch) are still in use by the `references-audit` member and remain the structural `remediations` lane names in every audit-skill's YAML contract (auto = FIX, discuss = SERIOUS + IMPROVE + SILENT-default, special = K). AUTO maps to FIX; DISCUSS splits into SERIOUS + IMPROVE under the four-disposition model.

### corpus

The User + Project + installed-Plugin skill pool used as a resolution namespace. The corpus is what makes a reference like `/example:some-skill` resolvable -- the auditor looks the name up in the corpus and reports MISSING when it does not resolve. The corpus is discovered via the shared `skills_kit_lib.corpus` module and is the same for every audit-kind that needs name resolution.

### scaffolding

A Python (or other) script that replaces inference-based decisioning or multi-tool-call orchestration with deterministic code. Every audit-kind has two scaffolding shapes:

- **Discovery scaffolding** -- finds subjects (e.g. `skills_kit_lib.corpus` for the skill pool, `discover.py` for cwd-relative SKILL.md enumeration).
- **Evaluator scaffolding** -- applies rules to a subject and emits findings (e.g. `audit.py` for skill-md schema validation, `references_audit.py` for cross-reference resolution).

The skill describes when to run each scaffolding and how to interpret its output; the scaffolding is what makes the audit repeatable, idempotent, and cheap. A purely inferential audit (every rule re-derived per file from agent reading) is slower, more expensive, and non-idempotent -- the same SKILL.md scored against the same rules might return different findings on different runs.

Scaffolding is the load-bearing convention this framework rests on. Any operation that requires multiple tool calls to perform as one repeatable step, or any decision tree that would otherwise be inference-based, belongs in scaffolding.

## Principles

- **Scaffolding over inference.** Any operation that requires multiple tool calls to perform as one repeatable step, or any decision tree that would otherwise be inference-based, belongs in a Python script. Skills describe when to run the scaffolding and how to interpret its output; they do not re-derive rules per session.
- **Idempotency.** Same input produces the same verdict. Rules, severities, taxonomy categories, and disposition predicates are fixed; the same finding gets the same disposition every run (the `bucket` default plus the classifier predicates are deterministic). Do not re-rank or re-order findings session-to-session. The auditor must be able to re-run the audit after remediation and see only the findings the remediation did not resolve.
- **Compositional discovery.** What rules apply to a subtree is decided by what marker is at the subtree's root. Compositions stack rather than override; a plugin containing skills runs plugin rules over the plugin and skill rules over each skill.
- **Severity is intrinsic; disposition is dispatch.** Whether a finding is FAIL / JUDGMENT / INFO is part of the rule. Whether the remediation is FIX / SERIOUS / IMPROVE / SILENT (legacy AUTO / DISCUSS / SPECIAL for references-audit) is assigned by the lane classifier; the taxonomy's `bucket` is only the default. The two are independent axes; a FAIL finding can be FIX (mechanical fix) or IMPROVE (judgment-required mapping). Do not collapse them.
- **Detection and remediation are separate phases.** An audit pass produces findings. A remediation pass consumes findings. Mixing the two in one procedure breaks idempotency -- the audit must produce the same findings on rerun, regardless of which remediations have been applied in between.
- **Rules live where they are owned.** Each rule's canonical definition (id, severity, summary, detail) lives in the SKILL.md `criteria:` block of the audit-skill that owns it. The framework registry references rule ids; it does not redefine them. A rule change touches one file, not two.
- **Open under addition.** Primitives, compositions, audit-kinds, and rule bindings grow as needed. Each addition is a registry entry (in `audit-framework.yaml`) plus the rule definition in the owning SKILL.md; no framework-side refactor.
- **Build only what we need today.** The framework supports exactly the audit-kinds registered in `audit-framework.yaml::audit_kinds`. Forward concerns are registry stubs, not implemented surfaces: planned audit-kinds and primitives under the YAML's `future:` key, planned rules under `future_rules:` -- nothing in either list is checked by any audit today.

## How the skills use this framework

### `references-audit` (via `/md-audit references`)

Operationalizes the **references-audit** audit-kind. The skill:

1. Picks the **subject** from arguments (a path, a scope, or the default skills-corpus).
2. Walks the subject performing **discovery**; toggles plugin / skill / directory rules per subtree.
3. Runs the **scaffolding** (`references_audit.py`) to produce findings against the corpus-resolved skill pool.
4. Each finding is a `(rule, subject_path, severity, message)` tuple. Severities are FAIL (broken hard-dep), JUDGMENT (none today), INFO (broken soft ref, name mismatch, shadowed skill).
5. Classifies findings into its **taxonomy** (A renamed, B retired, ... K unclassified) and dispatches AUTO / DISCUSS / SPECIAL **buckets** in parallel.

In framework terms: references-audit's subject is one of `directory | skill | plugin | project`; the primitive it parses is `md`; the rules per composition come from the audit-kind's bindings table in `audit-framework.yaml`. The skill's body documents the operational steps; the rule set is canonical in the YAML.

### `skill-audit` (via `/md-audit skill`)

Operationalizes the **skill-md-audit** audit-kind (plus two corpus-wide inventory procedures, roster and hierarchy, that share the same subject but do not exercise findings/remediation). The skill:

1. Picks one or more **subjects** of type `skill_md` (one `SKILL.md` per file).
2. Runs the **scaffolding** (`python -m skills_kit_lib.audit`, from the plugin root) for mechanical schema validation, plus agent-judgment passes for CCP / CRP / ADP placement.
3. Classifies findings into its **taxonomy** (A missing required, B description quality, ... K unclassified) and assigns each a **disposition** (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) instance-level.
4. Renders a per-file COMPLIANT / NON-COMPLIANT verdict from the FAIL findings.

In framework terms: the subject is `skill_md` inside a `skill` composition; the primitives consumed are `skill_md` and `yaml` (the embedded contract block); rules come from the audit-kind's bindings table. The skill's existing `criteria:` block names the same rules that the framework's bindings table references -- the YAML and the SKILL.md must stay in sync.

### `claude-md-audit` (via `/md-audit claude-md`)

Operationalizes the **claude-md-audit** audit-kind. The skill:

1. Picks its **subjects** of type `claude_md` via `discover.py`, which enumerates the CLAUDE.md files visible from cwd and tags each with a role (root / ancestor / child / local) and a dimension (`classic` | `code-directory`, the Level-1 trigger).
2. Applies the cohesion/hygiene criteria via agent-judgment lanes, plus the mechanical schema-validation and record-floor rows from `skills_kit_lib/audit.py`. The `cd_*` criteria run only for `code-directory` targets; the `dd_*` density lens only when requested.
3. Classifies findings into its **taxonomy** and assigns each a **disposition** (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL) instance-level.

In framework terms: the subject is `claude_md` over the `directory` / `project` compositions; rules come from the audit-kind's bindings table.

### `project-doc-audit` (via `/md-audit project-doc`)

Operationalizes the **project-doc-audit** audit-kind. The skill:

1. Picks one or more **subjects** of type `plain_md` -- standalone project documents (`Docs/`, `.claude/docs/`, `<subsystem>/docs/`, READMEs, design notes) that sit outside any skill's `references/` folder and outside the CLAUDE.md hierarchy.
2. Runs the **scaffolding** (`discover.py`) to enumerate candidates and compute the mechanical signals (effective lines, approx tokens, inbound-citation count); the orphan signal is `inbound_citations == 0`.
3. Applies the cohesion-principles `project_reference_md` role + `skill_maturation_pipeline` via agent-judgment lanes: Placement (graduate / fold / absorb), CRP (single reading task), ADP (discoverability + one-hop + no-back-reference), CCP (no skill-content duplication).
4. Classifies findings into its **taxonomy** (A misclassified, B graduate, ... K unclassified, plus the mechanical FIX ids N-R) and assigns each a **disposition** (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL). This audit is NO LONGER blanket no-AUTO: the mechanical convention checks (N broken-link-with-target, O non-ASCII, P foreign-abs-path, Q line-drift, R stale-anchor) plus I dedup are FIX; placement/maturation ids stay IMPROVE.
5. Renders a per-file COMPLIANT / NON-COMPLIANT verdict from the FAIL findings.

In framework terms: the subject is `plain_md` over the `directory` / `project` compositions; the only mechanical step is `discover.py` (there is no separate evaluator binary -- the lanes do the judgment); rules come from the audit-kind's bindings table. Skill-attached `reference_doc` is audited transitively via `skill_md_audit`; `claude_md` via `claude_md_audit` -- this audit owns only the standalone project document.

## Beyond audits: viewer scaffolding

The substrate this framework defines -- primitives, compositions, discovery, scaffolding -- is broader than audits. A **viewer scaffolding** family rides on the same substrate but produces a representation (typically a self-contained HTML) instead of findings.

A viewer-kind is the analogue of an audit-kind:

- It declares the **subject** it visualizes (typically a composition: marketplace, plugin, skill, project).
- It uses **discovery** to find subjects in scope (the same walk-and-mark logic; marketplaces are a corpus, projects are single subjects).
- It declares **per-primitive summary projections** -- the short representation each primitive contributes at each container level (e.g. a `skill_md` contributes name + description + skill-type; a `reference_doc` contributes filename + first heading).
- It supports **layered personalization** through per-composition override YAML files (each authored by a different party: the marketplace maintainer, the plugin author, the project owner, the viewer's operator). The override files only customize what would otherwise default.
- It MAY be **self-parameterizing** -- generating a skeleton override with inferred defaults when one is missing, so the operator edits a real file rather than writing one from a blank page. Whether a viewer-kind does this is declared per entry in the registry (`self_parameterizing`); a viewer-kind may instead gate participation on an override file existing (`discovery_gate`).
- It uses **viewer scaffolding** (a generator script) to walk the discovered tree, apply projections, fill in defaults, and emit the representation.

The registered viewer-kinds -- with their subjects, traversals, override layers, and self-parameterization flags -- live in `audit-framework.yaml::viewer_kinds`; this doc does not restate the roll-call.

Audit scaffolding produces findings; viewer scaffolding produces representations. The substrate is shared. Adding a viewer-kind is the same as adding an audit-kind from the framework's perspective: a registry entry + a generator script. The framework does not need to change.

## Extending the framework

Treat additions as encoding decisions per `skills-kit:knowledge-encoding`. Before adding a term, verify it is genuinely shared across audits (or about to be); if it lives in only one skill, it stays in that skill, not here. The framework's value is the canonical reference; expanding it with audit-specific concepts defeats that.

Mechanical extension paths:

- **New primitive** -- declare it (and any sub-kinds) in `audit-framework.yaml::primitives`; describe detection. Update existing audit-kinds only if they should consume the new primitive.
- **New composition** -- declare marker, contains spec, and any default rules in `audit-framework.yaml::compositions`. Decide its nesting (what it can contain, what it nests inside).
- **New audit-kind** -- declare the consumes/traverses/bindings table in `audit-framework.yaml::audit_kinds`. Author the scaffolding script and the operational skill body. The framework does not need a code change; the new audit-kind sits alongside the existing ones in the registry.
- **New rule** -- name the rule in the relevant audit-kind's bindings, declare its severity, implement the detection in the scaffolding. The rule is owned by the audit-kind, not by the framework; the framework only knows it exists.

Decisions that change the framework itself (a new principle, a new severity level, a new bucket) are framework-level changes and need a Dec-N provenance entry in `plugins/skills-kit/skills/skill-audit/CLAUDE.md` or `plugins/skills-kit/skills/skill-authoring/CLAUDE.md`, mirroring the audit-driven refinement discipline that shaped skill-authoring.
