---
_schema_version: 1
name: md-domain
author: christina
skill-type: domain-skill
description: Use when auditing or generating project markdown -- SKILL.md, CLAUDE.md, project docs, cross-refs -- analyzing code for missing ambient CLAUDE.md coverage, or resolving CLAUDE.md placement across a tree. Do NOT use for knowledge-encoding or update-documentation.
disable-model-invocation: false
user-invocable: true
argument-hint: "[audit|generate|coverage|hierarchy] [skill|claude-md|project-doc|references|<directory>] [<path>|--diff] [--reports <dir>] [--review] [--density] [--json] [--advanced] [fast]"
---

# md-domain

The single front door for four dispatch verbs over project markdown. **Audit**
and **generate** cross four artifacts (`skill`, `claude-md`, `project-doc`,
`references`); **coverage** assesses one `code_subtree` for facts missing from
its ambient CLAUDE.md chain; **hierarchy** resolves where every fact in one
`claude_md_tree` belongs. Coverage and hierarchy are report-only and are not
artifact-parameterized. This replaces the former `md-audit` / `md-authoring`
routers and the member skills they dispatched into.

The four are dispatch entries, not four things of the same kind. Auditing is
making sure something is accurate and compliant. Generation is creating a
document that does not exist -- and regeneration where the document already
exists. Coverage is the DISCOVERY step that feeds generation and
regeneration: it reads code, discovers facts, and names where each belongs, and
it writes nothing. Hierarchy is the RESOLUTION step between them: it takes those
facts plus the tree's existing documents and selects exactly one home per fact,
and it writes nothing either.

One skill, one dispatch table, four procedures. Audit and generate share
per-artifact standards; coverage and hierarchy each have their own criteria and
procedure. The "what good looks like" documents live in `references/standards/`,
the "how to run it" procedures live in `references/lanes/`, and the placement
spine they all defer to lives in `references/cohesion-principles.md`.

## Invocation

- **Bare** -- `/md-domain` greets with the menu below; pick a verb + artifact.
- **Argument-dispatched** -- `/md-domain audit skill <path>`,
  `/md-domain generate claude-md`, `/md-domain audit references [flags]`, and
  `/md-domain coverage <directory> [--advanced]`, and
  `/md-domain hierarchy <directory> [--reports <dir>]` jump straight into that
  lane.
- **Natural language** -- routed by the verb and subject named. Each lane
  record below declares the `invocation_phrasings` that should reach it.
- **Review mode** -- append `--review` to an `audit` dispatch on `skill`,
  `claude-md`, or `project-doc` to audit a CHANGE rather than a file. See
  "Review mode" below.
- **Coverage mode** -- name a directory or pass `--diff`; there is no whole-repo
  default. Coverage reads code and reports without editing code or markdown.
- **Hierarchy mode** -- name a tree root, optionally with `--reports <dir>`;
  there is no whole-repo default. Hierarchy reads the tree's CLAUDE.md files and
  the persisted reports and emits a placement plan, editing nothing.

### Bare-invocation greeting

```
How can I help you with your project markdown?

Just tell me what you want, in your own words. Widest first:

  "audit everything"                   every analysis below, across the repo
  "audit all the skills"               Skill audit
  "audit the CLAUDE.md files"          CLAUDE.md audit
  "audit the docs"                     Project-doc audit
  "check for broken skill references"  Cross-reference audit

  "check <directory> for coverage"     Coverage analysis. Reads the CODE there
                                       and reports which facts its CLAUDE.md
                                       chain should carry so a code review can
                                       act on them. Reports only, never edits.
                                       Ask me to write the results up afterwards
                                       and I generate those CLAUDE.md files --
                                       a separate step you choose, per candidate.

  "resolve placement across <dir>"     Hierarchy resolution. Takes the coverage
                                       reports you have kept plus the CLAUDE.md
                                       files already in that tree, and works out
                                       ONE home for each fact -- what merges,
                                       what moves up, what comes back out of a
                                       document, and what to delete where. It
                                       emits the plan; it never edits anything.

Before starting I name the analysis and its exact scope, because what a run
READS is what makes it cheap or expensive.

Generation is not a separate mode -- ask me to write a skill, a CLAUDE.md, or a
doc and I apply these same standards in the producing direction.

Or can I help you with something else?
```

Show the menu and stop; do not load a lane or a standards doc until the user picks.

Three things the greeting deliberately omits, so do not read their absence as
scope.

**Narrow selectors** -- a single file, a `list` index, `--density`,
`--advanced` -- are real and documented under "Argument grammar"; a user who
names one file is served normally.

**The generation lanes** still exist in the dispatch table; the greeting frames
generation as a direction the standards are read in rather than a verb to pick,
because nobody arrives wanting to "run generate" -- they arrive wanting a
document written.

**Review mode and the skill roster are NOT offered.** Both capabilities remain
-- review mode is how `git-kit` and `p4-kit` dispatch these lanes over a diff,
and the roster is a utility -- but neither is something md-domain solicits from
a user. Reviewing a diff is an audit of a change, which is the code-review
skills' job; md-domain informs that review and does not front-door it. An
inventory renders no verdict, so it is not an audit at all. Offering either
here invites a user to run the wrong skill.

The right-hand labels are the canonical analysis names that "Naming and scope
announcement" below requires you to echo. The left column is only the entry
point.

### Naming and scope announcement (applies to every run)

The names above are the CANONICAL vocabulary for what this skill does. They are
not menu decoration:

- **Announce before running.** State the analysis by its exact menu name, then
  the scope -- the concrete file set with a count, or the directory and what was
  excluded from it. "Running a CLAUDE.md audit over 3 files: a/CLAUDE.md,
  b/CLAUDE.md, c/CLAUDE.md."
- **Echo, do not paraphrase.** A user who picked "Cross-reference audit" must
  see "Cross-reference audit" when it starts. Inventing a synonym per run is how
  a user loses track of which analysis they authorized.
- **Name every analysis you run.** A dispatch that runs two -- an audit plus a
  roster inventory, which is a separate operation and not part of the audit --
  announces both, not just the headline one.
- **Scope is part of the announcement, never implied.** "the corpus" is not a
  scope; the count and the roots are. When a selector expands to more files than
  the user likely pictured, say the number before starting, not after.

## Dispatch table

For audit and generate, route by verb AND artifact. Coverage and hierarchy each
have one non-artifact subject -- `code_subtree` and `claude_md_tree`. In every
case load the selected procedure plus its standards doc -- exactly those two,
never the whole tree.

| Verb x artifact or subject | Lane id | Procedure | Standards doc |
|---|---|---|---|
| audit x skill | `audit_skill` | `references/lanes/audit-lane.md` | `references/standards/skill-standards.md` |
| audit x claude-md | `audit_claude_md` | `references/lanes/audit-lane.md` | `references/standards/claude-md-standards.md` |
| audit x project-doc | `audit_project_doc` | `references/lanes/audit-lane.md` | `references/standards/project-doc-standards.md` |
| audit x references | `audit_references` | `references/lanes/audit-lane.md` (references special case) | `references/standards/references-standards.md` |
| generate x skill | `generate_skill` | `references/lanes/generation-lane.md` | `references/standards/skill-standards.md` |
| generate x claude-md | `generate_claude_md` | `references/lanes/generation-lane.md` | `references/standards/claude-md-standards.md` |
| generate x project-doc | `generate_project_doc` | `references/lanes/generation-lane.md` | `references/standards/project-doc-standards.md` |
| generate x references | -- (no lane) | -- | -- |
| coverage (code subtree) | `coverage_code_subtree` | `references/lanes/coverage-lane.md` | `references/standards/coverage-standards.md` |
| hierarchy (claude_md_tree) | `hierarchy_claude_md_tree` | `references/lanes/hierarchy-lane.md` | `references/standards/hierarchy-standards.md` |

**`generate x references` has no lane, deliberately.** Cross-references are not a
generated artifact -- they are an emergent property of the other three. There is
nothing to generate; a request to "fix my broken references" is a REMEDIATION of
the `audit_references` lane, and a request to "add a reference" is generating
whichever artifact carries it (`generate_skill` / `generate_claude_md` /
`generate_project_doc`). Say so and route there rather than improvising a lane.

**Coverage then generation is a CHAIN, not a composite verb.** "Find what's
missing and write it up" is the natural end-to-end request, and it is served by
running `coverage` and then `generate x claude-md` -- two dispatches, in order,
with the user's decision in between. There is deliberately no `coverage+generate`
verb: coverage is report-only, and a single verb that discovered and wrote in one
motion would make the report a formality rather than a decision point.

Route it as a chain: run coverage, present the report, and offer to write up the
candidates the user picks. Each destination is its own generation run, taking
that destination's candidates together, with `destination` treated as a
pre-resolved placement. Caller-side mechanics are in `coverage-lane.md`
("Handing the report to generation"); the intake side is `generation-lane.md`'s
precondition.

**At TREE scale the chain gains a middle link.** When several subtrees were
assessed and their reports kept, the same fact arrives once per sibling -- a
sibling's CLAUDE.md is not ambient for a subtree, so per-subtree coverage
re-reports it correctly -- and no leaf can judge whether its fact belongs at a
parent. `hierarchy` is the phase that sees the whole tree at once:

```
coverage (per leaf, N runs) -> hierarchy (tree, 1 run) -> generate (per destination, M runs)
```

with the user's decision between each arrow. It is a CHAIN for the same reason
the two-link form is: hierarchy is report-only, and a verb that resolved and
wrote in one motion would make the plan a formality. As shipped the middle link
is OPT-IN -- it never runs as a side effect of a coverage or generation
dispatch, and a tree-scale regeneration request offers it by name rather than
running it.

### Lane records

Every lane record carries the two REQUIRED fields -- `invocation_phrasings`
(>= 3 natural-language phrasings that should route there) and `change_driver`
(one line naming what class of change makes the lane's content change). A lane
record missing either is invalid and the registry-integrity test fails.

```yaml
lanes:
  _schema_version: "1"
  records:
  - id: audit_skill
    verb: audit
    artifact: skill
    standards: references/standards/skill-standards.md
    procedure: references/lanes/audit-lane.md
    discover_script: scripts/discover_skill.py
    workflow_detect: workflow/skill-detect.js
    workflow_remediate: workflow/skill-remediate.js
    verdicts: [COMPLIANT, NON-COMPLIANT, DIFF-CLEAN, NOT-AUDITED]
    review_mode: true
    subject_shapes: [skill_md, skill_reference]
    invocation_phrasings:
      - "audit this SKILL.md"
      - "check my skill against its type contract"
      - "does this skill satisfy the framework"
      - "audit this skill reference document"
    change_driver: >-
      Changes when the SKILL.md type contract changes -- skill-standards.md,
      schema_registry.py, or a new per-type rule id -- or when the
      skill-reference prose criteria (skill-standards.md section 10) change.
  - id: audit_claude_md
    verb: audit
    artifact: claude-md
    standards: references/standards/claude-md-standards.md
    procedure: references/lanes/audit-lane.md
    discover_script: scripts/discover_claude_md.py
    workflow_detect: workflow/claude-md-detect.js
    workflow_remediate: workflow/claude-md-remediate.js
    verdicts: [COMPLIANT, NON-COMPLIANT, DIFF-CLEAN, NOT-AUDITED]
    review_mode: true
    density_lens: true
    invocation_phrasings:
      - "audit this CLAUDE.md"
      - "check my claude_md block"
      - "is this CLAUDE.md too verbose / audit for token efficiency"
      - "review the directory review-notes file"
    change_driver: >-
      Changes when the CLAUDE.md standards change -- the C/R/A/H rule set, the
      CD code-directory dimension, the DD density lens, or CLAUDE_MD_SCHEMA.
  - id: audit_project_doc
    verb: audit
    artifact: project-doc
    standards: references/standards/project-doc-standards.md
    procedure: references/lanes/audit-lane.md
    discover_script: scripts/discover_project_doc.py
    workflow_detect: workflow/project-doc-detect.js
    workflow_remediate: workflow/project-doc-remediate.js
    verdicts: [COMPLIANT, NON-COMPLIANT, DIFF-CLEAN, NOT-AUDITED]
    review_mode: true
    invocation_phrasings:
      - "audit the docs in .claude/docs"
      - "should this design doc graduate to a skill"
      - "is this project document an orphan"
      - "audit my README"
    change_driver: >-
      Changes when the project-doc standards change -- PD-1..PD-11, the
      maturation pipeline, or a named-role definition (readme, generated).
  - id: audit_references
    verb: audit
    artifact: references
    standards: references/standards/references-standards.md
    procedure: references/lanes/audit-lane.md
    scanner_script: scripts/references_audit.py
    taxonomy_doc: references/references-finding-taxonomy.md
    workflow_classify: workflow/references-classify.js
    workflow_remediate: workflow/references-remediate.js
    verdicts: [AUTO, DISCUSS, SPECIAL]
    review_mode: false
    invocation_phrasings:
      - "find broken skill references"
      - "check for dangling skill: hard deps"
      - "scan the corpus for cross-reference rot"
      - "did any skill rename break a reference"
    change_driver: >-
      Changes when the scanner's detection surface changes -- a new
      reference syntax, a new escape convention, or a new false-positive class.
  - id: generate_skill
    verb: generate
    artifact: skill
    standards: references/standards/skill-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    invocation_phrasings:
      - "generate a new skill"
      - "refine this SKILL.md"
      - "what type should this skill be"
      - "write the contract block for this skill"
    change_driver: >-
      Changes when the SKILL.md type contract changes (same driver as
      audit_skill -- one standards doc read in the producing direction).
  - id: generate_claude_md
    verb: generate
    artifact: claude-md
    standards: references/standards/claude-md-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    invocation_phrasings:
      - "write a claude_md block"
      - "generate a CLAUDE.md for this directory"
      - "add an insight record to CLAUDE.md"
      - "write review notes for this code directory"
    change_driver: >-
      Changes when the CLAUDE.md standards change (same driver as
      audit_claude_md, producing direction).
  - id: generate_project_doc
    verb: generate
    artifact: project-doc
    standards: references/standards/project-doc-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    invocation_phrasings:
      - "write a project document / design doc"
      - "where should this doc live"
      - "generate a README for this project"
      - "turn these notes into a reference doc"
    change_driver: >-
      Changes when the project-doc standards change (same driver as
      audit_project_doc, producing direction).
  - id: coverage_code_subtree
    verb: coverage
    subject: code_subtree
    standards: references/standards/coverage-standards.md
    procedure: references/lanes/coverage-lane.md
    discover_script: scripts/discover_coverage.py
    workflow_detect: workflow/coverage-detect.js
    verdicts: [GAPS-FOUND, COVERAGE-ASSESSED]
    report_only: true
    depth_modes: [basic, advanced]
    invocation_phrasings:
      - "analyze this code directory for missing CLAUDE.md guidance"
      - "run coverage analysis on this subtree"
      - "find what this directory's CLAUDE.md is missing and write it up"
      - "find code-derived facts that should be ambient"
    change_driver: Changes when coverage criteria, depth semantics, or the report-only procedure change.
  - id: hierarchy_claude_md_tree
    verb: hierarchy
    subject: claude_md_tree
    standards: references/standards/hierarchy-standards.md
    procedure: references/lanes/hierarchy-lane.md
    discover_script: scripts/discover_hierarchy.py
    workflow_detect: workflow/hierarchy-detect.js
    verdicts: [CHAIN-COHERENT, RESOLUTION-PROPOSED]
    report_only: true
    consumes_reports: true
    invocation_phrasings:
      - "resolve CLAUDE.md placement across this tree"
      - "merge these coverage reports into one plan"
      - "which of these facts should move up to a parent CLAUDE.md"
      - "de-duplicate the facts my sibling subtrees both reported"
    change_driver: >-
      Changes when the placement-resolution criteria (hierarchy-standards.md),
      the persisted-report input contract, or the report-only procedure change.
```

## Argument grammar

Audit/generate positional form: `<verb> <artifact> [selector] [flags]`.
Coverage form: `coverage (<directory> | --diff) [--json] [--advanced]`.
Hierarchy form: `hierarchy <directory> [--reports <dir>] [--json]`.
Verb and subject may be inferred from natural language; when a required part is
ambiguous, ask rather than guessing.

- **Verb** -- `audit` | `generate` | `coverage` | `hierarchy`. Absent and
  unrecoverable from phrasing -> show the menu.
- **Artifact** (audit / generate only) -- `skill` | `claude-md` | `project-doc`
  | `references`.
- **Selector** (audit lanes) -- `(none)` audits the cwd artifact if present;
  `list` emits a numbered list from the lane's discover script and stops;
  `<path>` targets a file or directory; `<numbers>` selects by index from the
  last `list` output. `audit skill` also accepts `roster` / `hierarchy` (with an
  optional output path or `-` for stdout) for corpus inventory. The grammar puts
  them under `audit skill` because they share its subject; an inventory renders
  no verdict, so it is NOT an audit -- announce it as its own operation.
- **Coverage subject** -- a named directory or `--diff`. There is NO whole-repo
  default: if neither is present, say so and stop rather than choosing the cwd.
- **Hierarchy subject** -- a named tree root. There is NO whole-repo default and
  no `--diff` form: the leaf enumeration must cover a whole tree for the input
  inventory to mean anything.
- **`--reports <dir>`** -- hierarchy-only. A directory of persisted coverage
  reports (JSON), one or more subjects each. Absence is not an error; it selects
  a run with no candidates.
- **`--diff` / `--json`** -- `--diff` is coverage-only and resolves changed code
  into subtree subjects; `--json` emits the report as structured JSON on either
  report-only lane.
- **`--advanced`** -- coverage-only exhaustive reads plus invariant-discovery
  and verification passes. Without an explicit depth, prompt interactively; a
  non-interactive dispatch takes basic and discloses `defaults: depth=basic`.
- **`fast` / `--fast` / `--yes` / `-y`** -- non-interactive: skip the Q&A round
  and infer every IMPROVE/SPECIAL decision. FIX applies by definition, SERIOUS is
  surfaced. Prose intent ("just apply everything, don't ask me") sets the same flag.
- **`review` / `--review`** -- review mode (audit lanes on skill / claude-md /
  project-doc only). Prose intent ("review my changes before I submit", "audit
  the diff") sets the same flag. Mutually exclusive with `fast`. Off by default.
- **`density` / `--density`** -- pass-through to the `audit_claude_md` lane only:
  adds the opt-in DD-1..DD-4 lens. Advisory only, never FAIL. Prose intent ("is
  this CLAUDE.md too verbose", "audit for token efficiency") sets the same flag.
  Off by default; the lens never runs unless requested. On any other lane, say the
  flag does not apply there and continue without it.
- **`--scope skills|references|md|all`, `--path`, `--json`, `--verbose`,
  `--ignore-dir`, `--ignore-file`** -- pass-through to the `audit_references`
  scanner.

## Review mode

Review mode audits a CHANGE rather than a file: same criteria, same lanes, but
findings the change did not cause are suppressed and nothing is auto-applied. It
exists to gate a submit / publish / handback. The full mechanics (attributability
filter, pre-image materialization, the two documented limits) live in
`references/lanes/audit-lane.md`. The routing rules that belong HERE:

- `--review` flows to the `audit_skill`, `audit_claude_md`, and
  `audit_project_doc` lanes.
- **`--review` NEVER flows to `audit_references`.** That lane does not implement
  it. If `--review` arrives on a references dispatch, **say so and stop** rather
  than passing it through. The lane would ignore the token silently and return a
  whole-corpus scan, which a caller gating a submit would read as a passed
  change-scoped gate. A fake gate is worse than no gate.
- **`NOT-AUDITED` is not a pass.** It is the verdict a detect lane returns for a
  file it DECLINED as outside its artifact shape (see the decline contract in
  `references/lanes/audit-lane.md`). A declined file was never read. It is counted
  APART from `diffClean` and must never be folded into the clean count or reported
  as a pass -- a caller gating on `DIFF-CLEAN` must be able to tell "clean" from
  "nobody read it". This is the same fake-gate failure as the `references` case above.
- Review mode's fan-out threshold is 1 (always the Workflow path), because a gate
  must not inherit whatever model the session happens to be running.

## Domain contract

```yaml
domain_skill:
  _schema_version: "1"
  identity: The single front door for four dispatch verbs over project markdown -- auditing and generating SKILL.md (and its reference documents), CLAUDE.md, project documents, and skill cross-references, plus report-only coverage analysis over a code subtree and report-only placement resolution over a CLAUDE.md tree.
  companions:
    siblings: []
    note: |
      No sibling domains. The former md-audit / md-authoring routers and their member
      skills are folded in here. Adjacent non-member skills in skills-kit:
      knowledge-encoding (encoding a newly discovered insight into a persistent home),
      update-documentation (end-of-session doc review), materialized-output (the
      insight-view pattern). This domain owns placement, content shape, and the
      per-artifact standards; it does not own the encode-an-insight or
      end-of-session-review triggers.
  scope:
    covers:
      - dispatching audit, generation, coverage, or hierarchy intent to exactly one lane
      - owning the four per-artifact standards docs (what good looks like for skill / claude-md / project-doc / references)
      - owning coverage-standards.md for the code_subtree composition
      - owning hierarchy-standards.md for the claude_md_tree composition
      - owning four procedures (shared audit, shared generation -- new documents and regeneration of existing ones -- report-only coverage discovery, and report-only hierarchy resolution)
      - owning the placement spine (cohesion-principles) and the shared audit framework, configuration, and content-shape references
    excludes:
      - encoding a newly discovered insight into a persistent location (use knowledge-encoding)
      - end-of-session review of what the work implies for the docs (use update-documentation)
      - designing a materialized-insight tool (use materialized-output)
      - invoking the skills being audited or generated
  orientation:
    summary: |
      One skill, one dispatch table, four procedures. Audit and generate select an artifact,
      then load its standards plus the verb procedure. Coverage selects code_subtree
      and loads coverage-lane.md plus coverage-standards.md; hierarchy selects
      claude_md_tree and loads hierarchy-lane.md plus hierarchy-standards.md. Audit uses
      DETECT -> Q&A gate -> REMEDIATE, generate uses confirm -> place -> apply -> shape ->
      validate, coverage uses discover -> assess -> report and STOP, and hierarchy uses
      discover -> resolve -> report and STOP. Neither coverage nor hierarchy remediates
      code or markdown. Placement -- which file a fact belongs in -- defers to
      references/cohesion-principles.md; it is never re-derived in a lane or standards doc.
    behavioral_guardrails:
      - Announce every run by its canonical analysis name plus the concrete file scope BEFORE starting (see "Naming and scope announcement"). Echo the menu's name verbatim rather than paraphrasing it, name every analysis a dispatch runs rather than only the headline one, and give scope as a count plus roots -- never as "the corpus". The names are the user's only handle on which analysis they authorized.
      - Route by verb AND subject. Audit and generate require an artifact; coverage accepts only code_subtree and hierarchy only claude_md_tree, and neither is artifact-parameterized. Do not run a SKILL.md audit on a CLAUDE.md, and do not apply the generation lane's producing direction when the user asked for a verdict.
      - One lane at a time. On a bare invocation show the menu and wait; do not co-load standards docs or verb procedures. A typical invocation loads this SKILL.md plus one lane plus one standards doc.
      - Detection and remediation are separate phases for audit. The audit pass produces a verdict; it does not silently mutate the subject. Remediation is dispatched after the Q&A gate, as its own work. Coverage and hierarchy have no remediation phase and must stop after reporting.
      - An affirmative verdict is never emitted over inputs the run did not have. Coverage refuses DISCOVERY-FAILED subtrees; hierarchy reports INPUTS-INCOMPLETE, with no verdict, whenever an enumerated leaf has no input, a document went unread, or an input candidate was not accounted for. Both are computed from an inventory the report carries in full, not asserted by the assessment.
      - Audit findings carry a four-disposition classification (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL), assigned instance-level by the lane's detect classifier -- the taxonomy `bucket` is only the default. Report contract, in this order and with no hedging - SERIOUS summarized at the TOP (never auto-fixed), FIX as an applied count landing in a reviewable CL (review mode - PROPOSED, never applied), IMPROVE as a count plus one-line pitches (opt-in discussion), SILENT omitted entirely. The references lane retains the legacy AUTO / DISCUSS / SPECIAL lanes. Coverage uses only GAPS-FOUND / COVERAGE-ASSESSED and never remediates.
      - Defer placement to references/cohesion-principles.md -- which CLAUDE.md, which skill, whether content graduates. Do not re-derive the placement algorithm inside a lane, a standards doc, or from memory. A placement already resolved upstream (an audit remediation naming the destination, an orchestrator directive) is followed, not re-derived.
      - Summarize-and-reference, do not restate. Keep a fact in its SSOT and reference it elsewhere; the compact form is a reminder plus a reference, and only when the fact fits about a dozen tokens -- beyond that, reference only. See cohesion-principles `summarize_and_reference` and its loss-free-deletion guard.
      - Size is a SIGNAL, not a verdict. An over-threshold file prompts a CRP evaluation (do sections serve different reading tasks?), never an automatic split -- and a split is offered (IMPROVE) only with a named extraction candidate, else it stays SILENT.
      - Do not author a recommended pattern -- only required, conditionally required, prohibited.
  index:
    references:
      - id: skill_standards
        path: references/standards/skill-standards.md
        keywords: [skill.md standards, type contract, required conditional prohibited, description requirements, frontmatter, mixed-type, schemas are floors, content allocation, skill reference document, references/*.md prose, SR-1 SR-2 SR-3 SR-4, inbound anchor integrity, internal contradiction, claim calibration, reader fit, maintainer-only material]
        summary: What a good SKILL.md looks like -- per-type contract tables, description requirements, content-form choice, L1/L2/L3 allocation, hygiene thresholds -- plus section 10, the prose criteria for the artifact's second subject shape, a skill reference document. Read by both the audit_skill and generate_skill lanes. Paired with skills_kit_lib/schema_registry.py, which wins on divergence.
      - id: claude_md_standards
        path: references/standards/claude-md-standards.md
        keywords: [claude.md standards, claude_md block, C-1 R-1 A-1 H-1, ccp crp adp rules, code-directory review notes, CD-1, density DD-1, insight record shape, scope covers excludes]
        summary: What a good CLAUDE.md looks like -- the classic C/R/A/H rule set, the CD code-directory review-notes dimension (shapes, observation kinds, anchoring discipline), and the opt-in DD density lens. Read by both the audit_claude_md and generate_claude_md lanes.
      - id: coverage_standards
        path: references/standards/coverage-standards.md
        keywords: [coverage standards, code subtree, ambient claude.md, absent facts, CV criteria, basic advanced, analysis depth, candidate admission]
        summary: What makes a code-derived fact earn ambient CLAUDE.md cost -- CV admission criteria, the basic/advanced depth contract, evidence floor, suppression rules, and report-only boundary. Read by coverage_code_subtree.
      - id: hierarchy_standards
        path: references/standards/hierarchy-standards.md
        keywords: [hierarchy standards, claude_md_tree, placement resolution, one home per fact, HR criteria, input inventory, unplaceable, subtraction, disposition re-judged, merge precision]
        summary: What makes a placement resolution over a whole CLAUDE.md tree honest -- HR-1..HR-7 (one home per fact, shallowest true depth, precedent over hoisting, complete input inventory, downward-only disposition flips, merge preserves precision, unplaceable declared), the inventory the verdict is computed from, and the report-only boundary. Read by hierarchy_claude_md_tree.
      - id: project_doc_standards
        path: references/standards/project-doc-standards.md
        keywords: [project document standards, PD-1, maturation, graduate to skill, orphan, discoverability, one hop, readme role, generated artifact, ancestor convention]
        summary: What a good project document looks like -- PD-1..PD-11 (placement, maturation, CRP unitary reading task, ADP discoverability and one-hop, CCP no-skill-duplication, named roles, hygiene). Read by both the audit_project_doc and generate_project_doc lanes.
      - id: references_standards
        path: references/standards/references-standards.md
        keywords: [cross-reference standards, hard dep, soft ref, name mismatch, shadowing, documentation convention, example prefix, proposed prefix, allow-stale, code fence masking]
        summary: What good cross-references look like -- the four scanner criteria (hard-dep missing, soft-ref missing, name mismatch, shadowing), the escape-prefix Documentation Convention, and the per-file allow-stale mechanism. Read by the audit_references lane.
      - id: cohesion_principles
        path: references/cohesion-principles.md
        keywords: [placement, where does this live, ccp crp adp, load graph, per artifact role, placement algorithm, skill packaging razor, placement follows trigger shape, summarize and reference, maturation pipeline]
        summary: The placement spine -- the content_allocation framework (load graph, CCP/CRP/ADP applied to placement, the placement algorithm, per-artifact roles, the skill maturation pipeline, placement_follows_trigger_shape, the two-step skill_packaging_razor, summarize_and_reference). Every lane and every standards doc defers here for WHERE a fact lives; on divergence, this document wins.
      - id: coverage_gap
        path: references/coverage-gap.md
        keywords: [what the audit does not check, coverage not assessed, clean audit missed hazards, compliant is not sufficient for review, absent content, gotcha crawler, ambient coverage, hazard sweep rejected, coverage lane design, held-out validation, documenting a bug fossilizes it]
        summary: What md-domain does NOT check and why -- the gap between "internally coherent and locally accurate" (what COMPLIANT asserts) and fitness for code review, the six mechanisms behind it, why the obvious hazard-sweep fix was rejected on review, the opt-in coverage-lane design that would close it, and the held-out validation discipline criteria changes are held to. Read before proposing a change that makes the audit look harder, or before reading COMPLIANT as an endorsement.
      - id: audit_framework
        path: references/audit-framework.md
        keywords: [audit framework, glossary, subject, primitive, composition, audit-kind, rule, finding, severity, taxonomy, bucket, corpus, scaffolding]
        summary: Canonical glossary for the audit family -- the vocabulary (subject / primitive / composition / discovery / audit-kind / rule / finding / severity / taxonomy / bucket / corpus / scaffolding) every lane declares its subject and rules in terms of. Also the SSOT for the four-disposition bucket model.
      - id: audit_framework_data
        path: references/audit-framework.yaml
        keywords: [audit framework data, primitives, compositions, audit-kind registry, rules per composition, lane bindings, machine-readable]
        summary: The machine-readable data side of the framework -- primitives, compositions, and the audit-kind registry (which rule ids bind to which compositions per lane). Authoritative on divergence with the markdown tables.
      - id: configuring_standards
        path: references/configuring-standards.md
        keywords: [configure standards, disable rule, tune threshold, rules off, config.yaml, config.local.yaml, layer model, rule-id catalog, disabledCriteria, thresholds]
        summary: User-and-Claude-facing configuration reference -- the layer model and precedence, config.yaml format, the generated rule-id catalog by bucket, the thresholds, additive standards files, how disables surface in reports, and troubleshooting. Load when a user wants to configure which opinions skills-kit enforces.
      - id: authoring_standards
        path: references/authoring-standards.md
        keywords: [author standards file, standards_set block, applies_to, criteria, severity, enforcement, verbatim quote, standards schema, additive standards]
        summary: Authoring spec for an additive standards file -- the standards_set block schema, filename-to-primitive convention, severity (fail/info/judgment) and enforcement (mechanical/judgment) semantics, verbatim-quote posture, and a complete valid example.
      - id: audit_lane
        path: references/lanes/audit-lane.md
        keywords: [audit procedure, detect remediate, q and a gate, workflow fan-out, pre-image, review mode, non-interactive, decline contract, not-audited, output template, four dispositions]
        summary: The ONE audit procedure, parameterized by artifact -- DETECT (opus/high) -> Q&A gate -> REMEDIATE (sonnet/low), the fan-out thresholds, pre-image materialization, the report contract, non-interactive inference, the generalized decline contract, and the references-lane special case.
      - id: references_finding_taxonomy
        path: references/references-finding-taxonomy.md
        keywords: [references finding taxonomy, A-K categories, hard dep missing, soft ref missing, name mismatch, shadowing, detection signals, disposition defaults, background-agent brief]
        summary: The references lane's A-K classification taxonomy -- detection signals, default remediations, the scanner-rule disposition table, and the background-agent brief template for cross-reference findings.
      - id: generation_lane
        path: references/lanes/generation-lane.md
        keywords: [generation procedure, confirm artifact, placement, apply standards, shape content, validate, single invocation, produce compliant]
        summary: The ONE generation procedure, parameterized by artifact -- confirm the artifact, resolve placement via cohesion-principles, apply the artifact's standards doc in the PRODUCING direction, shape per the authoring-patterns cluster, validate. Single-invocation; no fan-out machinery.
      - id: coverage_lane
        path: references/lanes/coverage-lane.md
        keywords: [coverage procedure, code subtree, ambient chain, report only, gaps found, coverage assessed, refs.criteria, analysis depth, no remediation]
        summary: The coverage procedure for the non-artifact code_subtree subject -- intent and depth gate, mechanical discovery, criteria-bound assessment, report shape, and STOP. It reads code and never remediates.
      - id: hierarchy_lane
        path: references/lanes/hierarchy-lane.md
        keywords: [hierarchy procedure, claude_md_tree, placement resolution, persisted coverage reports, input inventory, INPUTS-INCOMPLETE, chain coherent, resolution proposed, subtraction order, report only]
        summary: The hierarchy procedure for the non-artifact claude_md_tree subject -- intent gate, independent leaf enumeration and inventory building, criteria-bound resolution over persisted reports plus the tree's documents, the computed verdict and its refusal conditions, the report shape, and STOP. It resolves placement and never writes.
      - id: authoring_patterns
        path: references/authoring-patterns/
        keywords: [content shape, three surfaces, yaml header markdown embedded yaml, structure asserts, area ownership, area config, actions pattern, query tool pattern, how to shape a fact]
        summary: The verb-generic content-shape cluster -- content-authoring.md (the three content-form surfaces and the choice framework), three-surfaces.md, area-ownership.md, area-config.md, actions-pattern.md, query-tool-pattern.md. The HOW a fact is shaped, orthogonal to the per-artifact WHAT and to cohesion-principles' WHERE.
      - id: skill_domain
        path: references/skill-domain/
        keywords: [glossary, vocabulary, type contract tables, framework records, example audit, example verification, scripts reference, report usage, schema fixtures, domain layering, subdomain schema, patterns actions, skill authoring deep refs]
        summary: The skill-artifact deep reference cluster -- glossary.md (canonical vocabulary), framework.md (type-contract tables plus structured framework records; schema_registry.py wins on divergence), example-audit.md, example-verification.md, scripts.md (audit/classify/tag CLI + skills_kit_lib.corpus), report-usage.md (roster/hierarchy CLI), schema-fixtures.md (owner_doc validation fixtures), domain-layering.md, subdomain-schema.md, patterns-actions.md. Loaded on demand by the skill lanes.
      - id: provenance
        path: references/provenance/
        keywords: [decision provenance, dec_N, why the contract looks like this, framework decision log, folded skill histories]
        summary: Inherited decision-provenance logs (the dec_N framework decisions and the per-skill CLAUDE.md histories of the folded skills). Read when reconstructing WHY a contract or standard looks the way it does; never a runtime dependency of a lane.
  capabilities:
    - id: audit
      keywords: [audit, contract check, validate skill, run audit, schema validation]
      description: Run deterministic contract checks against a SKILL.md or CLAUDE.md (generation-time and audit-time validation).
      operation: python -m skills_kit_lib.audit <path>
      tool: skills_kit_lib/audit.py
      scope_axes: [single-skill]
      reference_section: skill-domain/scripts.md (audit)
    - id: classify
      keywords: [classify, infer type, type detection, mixed-type detection, suggest type]
      description: Infer a SKILL.md's type from content shape and YAML root.
      operation: python -m skills_kit_lib.classify <path>
      tool: skills_kit_lib/classify.py
      scope_axes: [single-skill]
      reference_section: skill-domain/scripts.md (classify)
    - id: tag
      keywords: [tag, write skill-type, frontmatter tagging, idempotent skill-type write]
      description: Write a skill-type value into a SKILL.md's frontmatter idempotently.
      operation: python -m skills_kit_lib.tag <path> <skill-type>
      tool: skills_kit_lib/tag.py
      scope_axes: [single-skill]
      reference_section: skill-domain/scripts.md (tag)
  tools:
    - name: audit
      command: python -m skills_kit_lib.audit
      description: YAML-first schema validator with markdown-heuristic fallback for legacy skills. Run from the plugin root (the -m form needs skills_kit_lib importable; see skill-domain/scripts.md).
    - name: classify
      command: python -m skills_kit_lib.classify
      description: Type inference across the canonical skill types.
    - name: tag
      command: python -m skills_kit_lib.tag
      description: Idempotent frontmatter tagger; refuses to invent or overwrite without --force.
```

## Cross-references

- **Where a fact lives (the placement spine)** -- `references/cohesion-principles.md`.
- **How to run an audit** -- `references/lanes/audit-lane.md`.
- **How to generate** -- `references/lanes/generation-lane.md`.
- **How to run coverage analysis** -- `references/lanes/coverage-lane.md`.
- **What earns a coverage candidate** -- `references/standards/coverage-standards.md`.
- **How to run a hierarchy resolution** -- `references/lanes/hierarchy-lane.md`.
- **What makes a placement resolution honest** -- `references/standards/hierarchy-standards.md`.
- **Encoding a newly discovered insight into a persistent home** -- `knowledge-encoding` (in skills-kit).
- **End-of-session review of what the work implies for the docs** -- `update-documentation` (in skills-kit).
- **Designing a materialized-insight tool** -- `materialized-output` (in skills-kit).
