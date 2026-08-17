---
_schema_version: 1
name: md-domain
author: christina
skill-type: domain-skill
description: Use when auditing, authoring, generating, or analyzing markdown -- SKILL.md, CLAUDE.md, docs. Do NOT use for knowledge-encoding or update-documentation.
disable-model-invocation: false
user-invocable: true
argument-hint: "[audit|author|generate|analyze] [skill|claude-md|project-doc|references|<directory>] [<path>|--diff] [--coverage <dir>] [--review] [--density] [--json] [--advanced] [fast]"
---

# md-domain

The single front door for four dispatch verbs over project markdown:

- **audit** -- check an existing document against its standards. Report a verdict.
- **analyze** -- read ONE directory's own code and report **coverage**: candidate
  facts, each carrying the `file:line` evidence it was derived from. Report-only.
- **author** -- write a document from content YOU supply, held to the artifact's
  standards.
- **generate** -- write a document FROM coverage, so every claim traces back to
  code a later run can re-read.

`audit` crosses four artifacts (`skill`, `claude-md`, `project-doc`,
`references`) and `author` crosses three (cross-references are not authored).
`analyze` and `generate` are not artifact-parameterized. This replaces the former
`md-audit` / `md-authoring` routers and the member skills they dispatched into.

**Coverage's subject is ONE DIRECTORY'S OWN DIRECT code files -- never a
subtree.** Assessing a directory never descends into its subdirectories: each of
those is its own subject. A parent's content comes instead from its own direct
code plus its children's finished CLAUDE.md files. The lane id
`coverage_code_subtree` and the composition name `code_subtree` are legacy
identifiers for that single-directory subject; the unit is the directory.

**AUTHOR and GENERATE differ by where the content came from, not by what they
produce.** Author takes what you give it -- a conversation, a pile of notes, an
audit remediation -- and makes it meet the standards. Generate takes coverage
produced by `analyze` and writes it up, so each claim carries the evidence a
later run can re-check. Both produce a document held to the same per-artifact
standards. Only a generated document can ALSO be checked back against its source,
and that is the whole of the difference.

**That is why `generate` exists for `claude-md` alone.** `analyze` reads code,
and code is what a CLAUDE.md is about. Nothing analyzes a codebase and emits
skill or project-doc candidates, so those two artifacts are authored. If an
analysis is ever built for them, they gain a generate lane; until then, do not
improvise one.

**REGENERATION is `generate` over a document that already exists.** Existence
decides, not a flag. Content the run can re-verify against current code is kept;
content it cannot is kept only where explicitly MARKED to retain. On the first
pass over a document carrying no markings the lane PROPOSES them and writes
nothing -- otherwise the first regeneration of every hand-written CLAUDE.md
would silently gut it. Marking syntax: `references/standards/claude-md-standards.md`
section 6.4.

One skill, one dispatch table, three procedures -- audit, producing (shared by
author and generate), and analysis. Audit, author and generate share the
per-artifact standards docs; analyze has its own criteria. The "what good looks
like" documents live in `references/standards/`, the "how to run it" procedures
live in `references/lanes/`, and the placement spine they all defer to lives in
`references/cohesion-principles.md`.

## Invocation

- **Bare** -- `/md-domain` greets with the menu below; pick a verb + artifact.
- **Argument-dispatched** -- `/md-domain audit skill <path>`,
  `/md-domain author claude-md`, `/md-domain generate claude-md <directory>`,
  `/md-domain audit references [flags]`, and
  `/md-domain analyze <directory> [--advanced]` jump straight into that lane.
- **Natural language** -- routed by the verb and subject named. Each lane
  record below declares the `invocation_phrasings` that should reach it.
- **Review mode** -- append `--review` to an `audit` dispatch on `skill`,
  `claude-md`, or `project-doc` to audit a CHANGE rather than a file. See
  "Review mode" below.
- **Analyze mode** -- name a directory or pass `--diff`; there is no whole-repo
  default. Analysis reads that directory's own direct code files -- not its
  subdirectories -- and reports coverage without editing code or markdown.
- **Generate mode** -- name a directory. Generate needs coverage: it uses this
  session's, or reads persisted reports named by `--coverage <dir>`, or runs
  `analyze` first and says so. Over a document that already exists it is
  REGENERATION and the retention rules apply.

### Bare-invocation greeting

```
How can I help you with your project markdown?

Tell me what you want in your own words. Grouped by what you want to work on:

SKILLS -- a SKILL.md and its reference documents
  "audit this skill"              Skill audit -- against its type contract
  "audit all the skills"          Skill audit, across the repo
  "write me a new skill"          Skill authoring -- I write it to the standards

CLAUDE.md
  "audit the CLAUDE.md files"     CLAUDE.md audit
  "write a CLAUDE.md for <dir>"   CLAUDE.md authoring -- from what you tell me
  "analyze <dir>"                 Code analysis. Reads the code sitting directly
                                  IN that directory -- not its subdirectories,
                                  each of which is its own run -- and reports
                                  COVERAGE: the facts its CLAUDE.md chain should
                                  carry, each with the evidence behind it.
                                  Reports only, never edits.
  "generate <dir>'s CLAUDE.md"    CLAUDE.md generation -- writes the document out
                                  of that coverage, so every claim traces back to
                                  code. I analyze first if we have none yet.
  "regenerate <dir>'s CLAUDE.md"  The same, over a document that already exists.
                                  What I can re-verify is kept; what I cannot is
                                  kept only where it is marked to retain. On a
                                  document with no markings yet I propose them
                                  and change nothing.
  "give <dir> and everything      The whole-tree form: analyze then generate per
   under it CLAUDE.md files"      directory, DEEPEST FIRST, because a parent is
                                  composed from its own code plus its children's
                                  finished documents. Committing to a directory
                                  commits to everything beneath it.

PROJECT DOCS -- READMEs, design records, references under docs/
  "audit the docs"                Project-doc audit
  "write a design doc / README"   Project-doc authoring

ACROSS ALL THREE
  "check for broken references"   Cross-reference audit
  "audit everything"              every audit above, across the repo

AUTHORING vs GENERATION is about where the content comes from. Authoring takes
what you give me and makes it meet the standards. Generation builds the document
out of analysis, so it can be checked back against the code later.

Before starting I name the analysis and its exact scope, because what a run
READS is what makes it cheap or expensive.

Or can I help you with something else?
```

Show the menu and stop; do not load a lane or a standards doc until the user picks.

Two things the greeting deliberately omits, so do not read their absence as
scope.

**Narrow selectors** -- a single file, a `list` index, `--density`,
`--advanced` -- are real and documented under "Argument grammar"; a user who
names one file is served normally.

**Review mode and the skill roster are NOT offered.** Both capabilities remain
-- review mode is how `git-kit` and `p4-kit` dispatch these lanes over a diff,
and the roster is a utility -- but neither is something md-domain solicits from
a user. Reviewing a diff is an audit of a change, which is the code-review
skills' job; md-domain informs that review and does not front-door it. An
inventory renders no verdict, so it is not an audit at all. Offering either
here invites a user to run the wrong skill.

**The producing verbs ARE offered, and must stay offered.** An earlier revision
omitted them, reasoning that "nobody arrives wanting to run generate -- they
arrive wanting a document written". A user arrived wanting to run generate, which
falsified it: the menu gave them nothing to point at, and the only mention was a
footnote below the whole audit list. Authoring and generation now appear per
artifact. Do not re-collapse them into a footnote -- see `CLAUDE.md`,
`producing_verbs_are_offered_not_footnoted`.

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

For audit and author, route by verb AND artifact. `generate` takes only
`claude-md`; `analyze` has one non-artifact subject, `code_subtree`. In every
case load the selected procedure plus its standards doc -- exactly those two,
never the whole tree.

| Verb x artifact or subject | Lane id | Procedure | Standards doc |
|---|---|---|---|
| audit x skill | `audit_skill` | `references/lanes/audit-lane.md` | `references/standards/skill-standards.md` |
| audit x claude-md | `audit_claude_md` | `references/lanes/audit-lane.md` | `references/standards/claude-md-standards.md` |
| audit x project-doc | `audit_project_doc` | `references/lanes/audit-lane.md` | `references/standards/project-doc-standards.md` |
| audit x references | `audit_references` | `references/lanes/audit-lane.md` (references special case) | `references/standards/references-standards.md` |
| author x skill | `author_skill` | `references/lanes/generation-lane.md` | `references/standards/skill-standards.md` |
| author x claude-md | `author_claude_md` | `references/lanes/generation-lane.md` | `references/standards/claude-md-standards.md` |
| author x project-doc | `author_project_doc` | `references/lanes/generation-lane.md` | `references/standards/project-doc-standards.md` |
| author x references | -- (no lane) | -- | -- |
| generate x claude-md | `generate_claude_md` | `references/lanes/generation-lane.md` | `references/standards/claude-md-standards.md` |
| analyze (one directory) | `coverage_code_subtree` | `references/lanes/coverage-lane.md` | `references/standards/coverage-standards.md` |

**The analyze lane's files are named for its OUTPUT, not its verb.** The lane id
`coverage_code_subtree`, `coverage-lane.md`, `coverage-standards.md`,
`discover_coverage.py`, `coverage-detect.js` and the composition `code_subtree`
all keep the word `coverage` because coverage is what `analyze` produces. The
verb was renamed; the artifact it emits was not. (`code_subtree` remains a legacy
identifier for a single-directory subject, as stated above.)

**`author x references` has no lane, deliberately.** Cross-references are not an
authored artifact -- they are an emergent property of the other three. There is
nothing to write; a request to "fix my broken references" is a REMEDIATION of the
`audit_references` lane, and a request to "add a reference" is authoring
whichever artifact carries it (`author_skill` / `author_claude_md` /
`author_project_doc`). Say so and route there rather than improvising a lane.

**`generate` has no `skill` or `project-doc` lane, for the same reason.** Nothing
analyzes a codebase and emits skill or project-doc candidates, so there is no
coverage to generate from. Those artifacts are AUTHORED. A request to "generate a
skill" is `author_skill`; say so rather than improvising a coverage input.

**Analysis then generation is a CHAIN, not a composite verb.** "Find what's
missing and write it up" is the natural end-to-end request, and it is served by
running `analyze` and then `generate x claude-md` -- two dispatches, in order,
with the user's decision in between. There is deliberately no `analyze+generate`
verb: analysis is report-only, and a single verb that discovered and wrote in one
motion would make the report a formality rather than a decision point.

Route it as a chain: run the analysis, present the coverage, and offer to write up
the candidates the user picks. Each destination is its own generation run, taking
that destination's candidates together, with `destination` treated as a
pre-resolved placement. Caller-side mechanics are in `coverage-lane.md`
("Handing the report to generation"); the intake side is `generation-lane.md`'s
precondition.

**At TREE scale the chain runs BOTTOM-UP, one directory at a time.** Each
directory is assessed on its own direct code, then composed; a parent is
composed only after every directory beneath it has a finished CLAUDE.md, because
its second input IS those documents:

```
for each directory, deepest first:
    analyze (its own direct code) -> generate (that directory)
```

Two properties follow, and both are constraints rather than conveniences.
Generating a directory COMMITS to generating every descendant of it first, so a
root regeneration is a whole-corpus operation and there is no cheap root
refresh. And a stale child document silently corrupts its parent, because the
parent trusts it as an input.

De-duplication happens during parent composition, by HOISTING: a fact found in a
child's document -- one child or several -- moves to their common ancestor when
it can be reworded so it is true as stated at that depth. It is never proposed
from below --
`fact-scoped-to-this-directory` in `references/standards/coverage-standards.md`
forbids an assessment from placing a fact outside the directory it read.

Depth is `shallowest_true_depth` in `references/cohesion-principles.md`: a fact
sits at the shallowest directory where it is true of everything below it, and
wording is the only test -- a hoisted fact must read as true at its new depth or
it stays in the children.

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
  - id: author_skill
    verb: author
    artifact: skill
    standards: references/standards/skill-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    input_provenance: user_supplied
    invocation_phrasings:
      - "write me a new skill"
      - "generate a new skill"
      - "refine this SKILL.md"
      - "what type should this skill be"
      - "write the contract block for this skill"
    change_driver: >-
      Changes when the SKILL.md type contract changes (same driver as
      audit_skill -- one standards doc read in the producing direction).
  - id: author_claude_md
    verb: author
    artifact: claude-md
    standards: references/standards/claude-md-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    input_provenance: user_supplied
    invocation_phrasings:
      - "write a claude_md block"
      - "write a CLAUDE.md for this directory"
      - "add an insight record to CLAUDE.md"
      - "turn what I just told you into a CLAUDE.md"
      - "write review notes for this code directory"
    change_driver: >-
      Changes when the CLAUDE.md standards change (same driver as
      audit_claude_md, producing direction).
  - id: author_project_doc
    verb: author
    artifact: project-doc
    standards: references/standards/project-doc-standards.md
    procedure: references/lanes/generation-lane.md
    verdicts: [COMPLIANT, NON-COMPLIANT]
    input_provenance: user_supplied
    invocation_phrasings:
      - "write a project document / design doc"
      - "where should this doc live"
      - "generate a README for this project"
      - "turn these notes into a reference doc"
    change_driver: >-
      Changes when the project-doc standards change (same driver as
      audit_project_doc, producing direction).
  - id: generate_claude_md
    verb: generate
    artifact: claude-md
    standards: references/standards/claude-md-standards.md
    procedure: references/lanes/generation-lane.md
    workflow_generate: workflow/claude-md-generate.js
    verdicts: [COMPLIANT, NON-COMPLIANT]
    input_provenance: coverage
    regeneration: propose-markings-first
    invocation_phrasings:
      - "generate this directory's CLAUDE.md from the analysis"
      - "regenerate the CLAUDE.md for this directory"
      - "write up the coverage we just produced"
      - "give this directory and everything under it CLAUDE.md files"
    change_driver: >-
      Changes when the CLAUDE.md standards change, when the coverage intake
      contract changes, or when the retention/verification rules for
      regeneration change.
  - id: coverage_code_subtree
    verb: analyze
    subject: code_subtree
    standards: references/standards/coverage-standards.md
    procedure: references/lanes/coverage-lane.md
    discover_script: scripts/discover_coverage.py
    workflow_detect: workflow/coverage-detect.js
    verdicts: [GAPS-FOUND, COVERAGE-ASSESSED]
    report_only: true
    depth_modes: [basic, advanced]
    invocation_phrasings:
      - "analyze this directory"
      - "analyze this code directory for missing CLAUDE.md guidance"
      - "run a coverage analysis on this directory"
      - "find what this directory's CLAUDE.md is missing"
      - "find code-derived facts that should be ambient"
    change_driver: Changes when coverage criteria, depth semantics, or the report-only procedure change.
```

## Argument grammar

Audit/author positional form: `<verb> <artifact> [selector] [flags]`.
Analyze form: `analyze (<directory> | --diff) [--json] [--advanced]`.
Generate form: `generate claude-md <directory> [--coverage <dir>]`.
Verb and subject may be inferred from natural language; when a required part is
ambiguous, ask rather than guessing.

- **Verb** -- `audit` | `author` | `generate` | `analyze`. Absent and
  unrecoverable from phrasing -> show the menu. **"generate a skill" and
  "generate a README" route to `author`**, because no analysis produces coverage
  for those artifacts; take the intent, not the token.
- **Artifact** (audit / author only) -- `skill` | `claude-md` | `project-doc`
  | `references` (`references` is audit-only). `generate` takes `claude-md` and
  nothing else.
- **`--coverage <dir>`** -- generate-only. A directory of persisted coverage
  reports (JSON) to write up, as emitted by `analyze --json`. Absent, generate
  uses the coverage from this session, and runs `analyze` first if there is none.
- **Selector** (audit lanes) -- `(none)` audits the cwd artifact if present;
  `list` emits a numbered list from the lane's discover script and stops;
  `<path>` targets a file or directory; `<numbers>` selects by index from the
  last `list` output. `audit skill` also accepts `roster` / `hierarchy` (with an
  optional output path or `-` for stdout) for corpus inventory. The grammar puts
  them under `audit skill` because they share its subject; an inventory renders
  no verdict, so it is NOT an audit -- announce it as its own operation.
- **Analyze subject** -- a named directory or `--diff`. There is NO whole-repo
  default: if neither is present, say so and stop rather than choosing the cwd.
- **`--diff` / `--json`** -- both analyze-only. `--diff` resolves changed code
  into per-directory subjects; `--json` emits the coverage report as structured
  JSON.
- **`--advanced`** -- analyze-only exhaustive reads plus invariant-discovery
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
  identity: The single front door for four dispatch verbs over project markdown -- auditing SKILL.md (and its reference documents), CLAUDE.md, project documents and skill cross-references; authoring any of those from content the user supplies; generating a CLAUDE.md from analysis-produced coverage so its claims stay re-checkable; and report-only analysis of one directory's direct code.
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
      - dispatching audit, author, generate, or analyze intent to exactly one lane
      - owning the four per-artifact standards docs (what good looks like for skill / claude-md / project-doc / references)
      - owning coverage-standards.md for the code_subtree composition (one directory's direct code, not a subtree)
      - owning three procedures (shared audit; ONE producing procedure serving both author and generate, including regeneration and its retention rules; and report-only analysis)
      - owning the placement spine (cohesion-principles) and the shared audit framework, configuration, and content-shape references
    excludes:
      - encoding a newly discovered insight into a persistent location (use knowledge-encoding)
      - end-of-session review of what the work implies for the docs (use update-documentation)
      - designing a materialized-insight tool (use materialized-output)
      - invoking the skills being audited or generated
  orientation:
    summary: |
      One skill, one dispatch table, three procedures. Audit and author select an artifact,
      then load its standards plus the verb procedure; generate takes claude-md and nothing
      else. Analyze selects code_subtree and loads coverage-lane.md plus
      coverage-standards.md. Audit uses DETECT -> Q&A gate -> REMEDIATE; author and generate
      SHARE confirm -> place -> apply -> shape -> validate, with generate adding a coverage
      intake in front and, on regeneration, a retention pass; analyze uses
      discover -> assess -> report and STOP and never remediates code or markdown.
      Author and generate differ ONLY by input provenance -- user-supplied versus
      analysis-produced coverage -- and that difference is what makes a generated document
      re-checkable against its source. Placement -- which file a fact belongs in -- defers to
      references/cohesion-principles.md; it is never re-derived in a lane or standards doc.
    behavioral_guardrails:
      - Announce every run by its canonical analysis name plus the concrete file scope BEFORE starting (see "Naming and scope announcement"). Echo the menu's name verbatim rather than paraphrasing it, name every analysis a dispatch runs rather than only the headline one, and give scope as a count plus roots -- never as "the corpus". The names are the user's only handle on which analysis they authorized.
      - Route by verb AND subject. Audit and author require an artifact; generate takes claude-md only; analyze accepts only code_subtree and is not artifact-parameterized. Do not run a SKILL.md audit on a CLAUDE.md, and do not apply the producing direction when the user asked for a verdict.
      - Author and generate are chosen by INPUT PROVENANCE, never by the word the user typed. Content the user supplies is authored; coverage from an analyze run is generated. "Generate a skill" and "generate a README" are author dispatches, because no analysis produces coverage for those artifacts -- say which lane you are taking and why, rather than silently honouring or silently overriding the token.
      - Regeneration never destroys what it cannot re-verify. Generating over a document that already exists keeps content it can verify against current code, keeps marked content unconditionally, and on a document carrying no markings PROPOSES them and writes nothing. Detect that state; do not ask the user to configure it, and never take an unmarked legacy document as licence to overwrite.
      - One lane at a time. On a bare invocation show the menu and wait; do not co-load standards docs or verb procedures. A typical invocation loads this SKILL.md plus one lane plus one standards doc.
      - Detection and remediation are separate phases for audit. The audit pass produces a verdict; it does not silently mutate the subject. Remediation is dispatched after the Q&A gate, as its own work. Analyze has no remediation phase and must stop after reporting.
      - An affirmative verdict is never emitted over inputs the run did not have. Analyze refuses DISCOVERY-FAILED directories, and that refusal is computed from an inventory the report carries in full, not asserted by the assessment.
      - Audit findings carry a four-disposition classification (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL), assigned instance-level by the lane's detect classifier -- the taxonomy `bucket` is only the default. Report contract, in this order and with no hedging - SERIOUS summarized at the TOP (never auto-fixed), FIX as an applied count landing in a reviewable CL (review mode - PROPOSED, never applied), IMPROVE as a count plus one-line pitches (opt-in discussion), SILENT omitted entirely. The references lane retains the legacy AUTO / DISCUSS / SPECIAL lanes. Analyze uses only GAPS-FOUND / COVERAGE-ASSESSED and never remediates.
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
        keywords: [coverage standards, one directory not a subtree, direct code files, non-recursive subject, ambient claude.md, absent facts, CV criteria, basic advanced, analysis depth, candidate admission, hoisting, vcs ignore exclusion]
        summary: What makes a code-derived fact earn ambient CLAUDE.md cost -- CV admission criteria, the basic/advanced depth contract, evidence floor, suppression rules, and report-only boundary. Read by coverage_code_subtree.
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
      - id: capability_boundaries
        path: references/capability-boundaries.md
        keywords: [what md-domain does not do, code review, code audit, does not exist, division of labour, external capability, defect, potential defects, release valve, informs review, accountable for covering, subject is a change, subject is a codebase]
        summary: The capability boundaries -- md-domain owns a document, code review owns a CHANGE (git-kit, p4-kit), and code audit owns a CODEBASE at rest and DOES NOT EXIST YET. Carries the specification for that absent capability so its absence is not read as an md-domain gap, the one-directional relationship to review (md-domain informs, review raises), the CLAUDE-potential-defects.md hand-off, and the accountable-for-covering test. Read before letting any lane report a code defect.
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
        keywords: [producing procedure, author lane, generate lane, confirm artifact, placement, apply standards, shape content, validate, coverage intake, regeneration, retention marking, propose markings, input provenance]
        summary: The ONE producing procedure, shared by author and generate and parameterized by artifact -- confirm the artifact, resolve placement via cohesion-principles, apply the artifact's standards doc in the PRODUCING direction, shape per the authoring-patterns cluster, validate. Also the coverage intake that distinguishes generate from author, and the retention rules regeneration runs under.
      - id: coverage_lane
        path: references/lanes/coverage-lane.md
        keywords: [analyze procedure, coverage procedure, one directory, direct code, non-recursive, ambient chain, report only, gaps found, coverage assessed, refs.criteria, analysis depth, no remediation]
        summary: The ANALYZE procedure for the non-artifact code_subtree subject -- intent and depth gate, mechanical discovery, criteria-bound assessment, the coverage report shape, and STOP. It reads code and never remediates. Named for its output (coverage), not its verb.
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
- **How to author or generate a document** -- `references/lanes/generation-lane.md`.
- **How to run an analysis** -- `references/lanes/coverage-lane.md`.
- **What earns a coverage candidate** -- `references/standards/coverage-standards.md`.
- **Retention marking for regeneration** -- `references/standards/claude-md-standards.md`, section 6.4.
- **What this domain does NOT do, and who owns it instead** -- `references/capability-boundaries.md`.
- **Encoding a newly discovered insight into a persistent home** -- `knowledge-encoding` (in skills-kit).
- **End-of-session review of what the work implies for the docs** -- `update-documentation` (in skills-kit).
- **Designing a materialized-insight tool** -- `materialized-output` (in skills-kit).
