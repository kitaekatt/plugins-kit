# The analyze lane (produces coverage)

The ANALYZE procedure. Unlike `audit` and `author`, this one is NOT parameterized
by artifact -- it has exactly one subject shape, and its subject is CODE.

**The verb is `analyze`; `coverage` is what it PRODUCES.** This file, the lane id
`coverage_code_subtree`, `coverage-standards.md`, `discover_coverage.py` and
`coverage-detect.js` all keep the word `coverage` because they are named for the
output, not the verb. Do not read those names as a surviving `coverage` verb;
there is none, and `/md-domain coverage` is not a dispatch.

**What kind of operation this is.** Analysis is not a peer of `audit`. It renders
no compliance verdict, so it is not an audit; it creates no document, so it is not
production either. It is the DISCOVERY step that feeds `generate` and
regeneration: it reads code, discovers facts, and names the destination each fact
belongs in. Being re-homed under that family changes nothing about what it may do
-- REPORT-ONLY IS A PROPERTY OF THIS ENTRY POINT, not an accident of the verb
being listed separately. The lane binds no remediate workflow and never writes,
whoever calls it and whatever the family is called.

**Its output is what makes GENERATION different from AUTHORING.** A document
written from coverage carries the `file:line` evidence behind each claim, so a
later run can re-derive it; a document authored from supplied content cannot be
re-checked against anything. That is the whole of the distinction, and it is why
this lane's report is worth persisting rather than consuming and discarding.

**Status as of 2026-08-08: filled and registered.** The assessment criteria live
in `references/standards/coverage-standards.md`, callers pass that document's
absolute path as `refs.criteria`, and `coverage_code_subtree` is registered in
SKILL.md. Registration is the go-live switch; all three parts landed together.

## What this verb is for, before anything else

md-domain is **not a code-review tool**. This verb reads code only as a SOURCE OF
INSIGHT for the CLAUDE.md that will be ambient for it. It does not hunt for
defects; finding those is the job of a code review conducted AGAINST the CLAUDE.md
this verb helps produce.

A run that returns a defect list has done the wrong work, however accurate the
list. The unit of output is always **a fact about the code that belongs in a
CLAUDE.md and is not ambient for the code it describes**.

## Subject and unit

**Unit: `(one directory's own direct code files, its ambient CLAUDE.md chain)`.**

The code-file set is NON-RECURSIVE. Assessing D reads the code files that sit
directly in D and never descends into D's subdirectories: each of those is its
own subject, assessed on its own terms
(`../standards/coverage-standards.md:10-13`). The ambient chain still walks
UPWARD without limit -- only the code-file set is bounded -- because ancestors
are what make a fact already-ambient under CV-2.

The boundary does not shortchange a parent, and the reason it does not is a
second input THIS lane never reads: the finished CLAUDE.md of each child
directory, consumed during parent composition in `generation-lane.md`. A
recursive subject would buy the same content by making every fact arrive once
per enclosing directory, so a parent's assessment would duplicate every
descendant's findings and any de-duplication downstream would compare facts
against copies of themselves (`../standards/coverage-standards.md:22-28`).

**Excluded directories.** A directory the project's VCS is configured to ignore
is not a subject: git -> `check-ignore --no-index`; Perforce -> `p4 ignores`;
neither -> nothing is excluded (`../standards/coverage-standards.md:30-34`).

The lane id `coverage_code_subtree` and the standards doc's
`applies_to: code_subtree` are stable identifiers, not descriptions of the
subject. Read them as names: what they identify is one directory's own direct
code.

This is why coverage cannot be a criterion inside `audit_claude_md`: the per-file
lanes enumerate CLAUDE.md files, and no criterion can have a subject its lane
cannot enumerate. The decisive case is a directory with NO CLAUDE.md at all, which
is exactly what this verb exists to surface and exactly what a per-file lane
never reaches.

## Parameters

| Parameter | Value |
|---|---|
| lane id | `coverage_code_subtree` |
| discovery | `scripts/discover_coverage.py` |
| detect workflow | `workflow/coverage-detect.js` |
| remediate workflow | NONE -- report-only, deliberately |
| verdicts | `GAPS-FOUND` / `COVERAGE-ASSESSED` |
| standards | `references/standards/coverage-standards.md` |
| analysis depth | `basic` / `advanced` |
| supported flags | `--diff`, `--json`, `--advanced` |

## Model pinning (not negotiable)

The detect workflow pins `opus` + `high` effort, per
`plugins/skills-kit/CLAUDE.md`.

**The workflow is entered regardless of subject count.** This is the one place
coverage must NOT copy the audit lane. `audit-lane.md:110-117` runs a
single-subject job INLINE, inheriting whatever model the session happens to be
on -- which is fine when the subject is one small markdown file. A coverage run
normally has exactly ONE directory, so reusing that shortcut would mean the
common case silently runs off-pin. Always go through the workflow.

There is no remediate lane, so the `sonnet` + `low` remediation pin and
`scripts/gen_workflow_js.py` do not apply here at all.

## The pipeline

```
Step 1  Resolve + INTENT GATE
Step 2  Discover (mechanical, side-effect free)
Step 3  Assess (coverage-standards.md)
Step 4  Report and stop
```

There is no Q&A gate and no remediation phase. Nothing is ever applied.

### Step 1 -- Resolve and gate on intent

Code analysis costs materially more than a document audit, so an off-by-default
flag is not sufficient protection.

1. This verb NEVER runs as part of an `audit` or `generate` invocation, and never
   as a side effect of any other lane.
2. It runs only on **expressed user intent** to analyze code for CLAUDE.md
   content.
3. When intent is **ambiguous** -- the request could be read as a document audit,
   or the scope is unstated -- CONFIRM with `AskUserQuestion` before running.
   Put the cost AND the scope in the same question. Never infer intent from the
   mere presence of code in a named directory.
4. Resolve analysis depth before discovery. An explicit `--advanced` flag wins
   silently. Otherwise, in an interactive dispatch, PROMPT via
   `AskUserQuestion`: `basic` is the bounded power-user run and `advanced` is the
   full experience. In a non-interactive dispatch, take `basic` and disclose
   exactly `defaults: depth=basic`. Never silently choose between the modes in
   an interactive run.

Resolve the target: a named directory, or `--diff`. **There is no whole-repo
default** -- an unbounded default is how this becomes expensive and
non-idempotent. If neither is given, say so and stop; do not pick one.

A named directory means THAT DIRECTORY, not the tree under it. When the caller
wants a tree covered, that is one run per directory in the tree, and the order
those runs are consumed in matters downstream -- see `generation-lane.md`, parent
composition, for the bottom-up constraint. This lane never widens a target on the
caller's behalf.

Announce the run by name and scope before Step 2, per SKILL.md's "Naming and
scope announcement": the analysis name (`Code analysis` -- the menu label, echoed
verbatim), the directory, the direct-code-file count, and the size of the ambient
chain.

### Step 2 -- Discover (mechanical)

Run `scripts/discover_coverage.py <directory>` (or `--diff`) and consume its
`subjects[]`. It is side-effect free and reads no file contents.

Per subject it returns `root`, `rootExclusion`, `codeFiles`,
`ambientClaudeMdPaths`, `skipped`, `noisePruned`, and `unknownExtensions`.
`codeFiles` holds the code files sitting DIRECTLY in `root` -- the script does
not descend, so an empty list on a directory whose descendants are full of code
is a correct result and not a discovery failure.

`unknownExtensions` (a `{<ext>: <count>}` map) has the same standing as the
structural exclusions above: surface it in the report, never drop it silently.
It names every file the discovery script could not place as code, doc, or a
recognized asset type -- the set the whole capability exists to protect
against silently losing.

Two properties of the ambient chain are load-bearing and are the script's job,
not the model's -- do not recompute them by eye:

- The chain **includes** a CLAUDE.md in the assessed directory itself.
- The upward walk **stops at the nearest `.git`**, so a nested repository never
  inherits the outer repository's chain.

An **empty** `ambientClaudeMdPaths` is not an error. It is the strongest possible
finding: nothing loads for this code at all.

The chain is the only relation this lane reads, and it reads ancestors solely to
establish what is already ambient. A CHILD directory's CLAUDE.md is not read
here: it is an input to COMPOSING this directory's document, which happens in
`generation-lane.md`, not to assessing this directory's own code.

Structural exclusions (vendored, generated, symlink-resolving-outside, nested
repo) are already applied and RECORDED. Surface them in the report; never drop
them silently.

### Step 3 -- Assess

Apply `references/standards/coverage-standards.md` verbatim. The caller MUST
resolve that document to an ABSOLUTE path and pass it as `refs.criteria` to
`workflow/coverage-detect.js`; never embed or paraphrase the criteria in the
JavaScript workflow. Apply the depth selected at the intent gate: `basic` uses a
bounded read and one assessment pass; `advanced` reads every source file
completely, discovers invariants before assessment, and verifies surviving
candidates after assessment.

The following settled rules remain part of the assessment contract:

- It reuses the AUTHORING direction's existing observation kinds
  (`claude-md-standards.md:385-390`), which already define what is worth writing
  up. The gap was never a missing vocabulary; it is that the audit direction
  refuses to look (`:431-433`, "a validator over existing claims, not a gotcha
  crawler").
- Every finding is JUDGMENT severity. Nothing here is mechanical.
- A fact already carried by an ambient claim that RESOLVES is not a candidate.
  This is an assessment-time suppression, NOT a pre-read exclusion -- establishing
  it requires reading the ambient document and usually the source it anchors to.
- **Every candidate's destination is the assessed directory.** `CV-3`
  (`fact-scoped-to-this-directory`) is fail-severity on this: a candidate must be
  a fact about this directory's own direct code, and an assessment never proposes
  a fact for a subdirectory, a sibling, or an ancestor. Each of those is assessed
  on its own terms and would receive the fact from its own run, and an assessment
  that read only this directory cannot know whether the fact holds of code it
  never opened. A fact that genuinely governs a wider area reaches that area by
  HOISTING at the parent during composition -- never by nomination from here.
- Apply the candidate ceiling, which is PER DIRECTORY rather than per run -- a
  per-run cap divided across subjects gives each directory an arbitrary share
  that shrinks as the run widens. When it is hit, SAY SO, and state the aggregate
  for a multi-directory run. Silent truncation in the verb that reports silent
  truncation would be its own joke.

`coverage-detect.js` refuses to run while `refs.criteria` names no document, so
this seam cannot be crossed by accident.

### Step 4 -- Report and stop

```
## Coverage analysis -- <directory>

Ambient chain (<N>, root-most first):
  - <path>
  ...
  (or: NONE -- no CLAUDE.md loads for this directory)

Direct code files assessed: <N>
Skipped: <N>  (<reason> <path>, ...)
Unknown extensions: <N>  (<ext>: <count>, ...)  (or: NONE)
Candidate ceiling (per directory): <not reached | REACHED -- N not shown>
Analysis depth: basic | advanced
<only for a non-interactive implicit basic selection: defaults: depth=basic>

### Candidates
- [<FINDING-CONVERTIBLE | CONTEXT-ONLY>] <fact>  ->  destination: <assessed directory>/CLAUDE.md
  <why it belongs there, and why it is not ambient today>
  evidence: <file:line>[, <file:line> ...]

### Coverage verdict
GAPS-FOUND | COVERAGE-ASSESSED
Meaning: <basic: not found within budget | advanced: verified absent>
```

Every candidate's `destination` is the assessed directory's CLAUDE.md, so the
field is degenerate by construction rather than a per-candidate decision. Render
it anyway: it is what makes a persisted report self-describing, and a
`destination` naming anything else is a CV-3 violation to fix, not a hint to
follow. Reporting a candidate is not a commitment to write it.

The tier prefix and the evidence line are not decoration: CV-4 requires the
classification to be REPORTED, and CV-7 is fail-severity on the file-and-line
citation. Rendering a candidate without them satisfies the criteria in the
assessment and drops them at the only point a reader sees. A CONTEXT-ONLY
candidate is a normal result -- the tier exists so a reader can tell the facts a
reviewer could act on from the ones that only orient, not to rank them.

Then STOP. No Q&A, no edits, no follow-up pass.

## Handing the report to generation

Stopping is where THIS lane ends, not where the work has to. A caller holding a
coverage report may invoke the producing lane (as `generate`) to write any candidate up. That
is a separate run of a separate lane, and it takes nothing away from report-only:
this lane still writes nothing, whoever calls it and whatever they do next.

The two lanes already meet, and the join needs no new machinery:

- **The `destination` field IS a resolved placement, and it is always the
  assessed directory.** `generation-lane.md` step 2 recognizes exactly this case
  -- "when a placement decision arrives already resolved ... follow it. Do not
  re-invoke the placement framework for it." A coverage candidate is that case,
  alongside an audit remediation naming the destination and an orchestrator
  directive. Because the destination is fixed to the subject, a report can never
  hand generation a placement argument to re-litigate.
- **`fact`, `why` and `anchors` are the content.** Generation's step 1
  precondition is a fact needing a home; the candidate is a fact, its
  justification, and its `file:line` evidence, with the home already named.

Three things the caller has to do, because neither lane does them:

1. **Group by `destination` first.** Generation is single-invocation -- one run
   writes one document. A report spanning N destinations is N generation runs,
   each taking that destination's candidates together, so the document is
   authored as a whole rather than appended to N times. With a fixed destination
   per report, N is the number of directories assessed.
2. **Order the generation runs BOTTOM-UP.** Composing a directory's document
   takes its children's finished documents as a second input, so a parent's run
   depends on every descendant's run having already happened. A caller feeding a
   tree's reports to generation in arbitrary order composes parents against stale
   or absent children. The constraint and its consequences belong to
   `generation-lane.md`, parent composition; the caller's job is to respect the
   order it states.
3. **Carry the tier through, do not filter on it.** CONTEXT-ONLY is admissible
   content, not rejected content -- CV-4 classifies candidates, it does not gate
   them. A caller writing up only the FINDING-CONVERTIBLE ones is making an
   editorial choice and should say so; the tier is a signal to the reader, not a
   filter the lanes apply.

What the caller must NOT do is treat the report as pre-approved. Reporting a
candidate is not a commitment to write it (above), so the decision to generate
is the caller's and is made per candidate, not per report.

### Two RETIRED carriage fields

A candidate record may still carry `scope` (`LEAF-ONLY` or `PROMOTE -> <dir>`)
and `sibling_overlap` (a sibling document stating the fact, and whether it
reaches this directory's author). **Neither is produced, and neither is a
criterion.** They are read-only compatibility surface -- reports written before
this model carry them and a loader must not choke on them --
`../standards/coverage-standards.md:190-212` is the authority.

They were the promotion machinery: an assessment nominating a destination above
itself. CV-3 forbids exactly that, for the reason the fields half-admitted --
judging whether a fact belongs here or at a parent means reading the parent or a
sibling, which is outside the subject. A fact reaches a wider area by HOISTING
instead, at the parent, where the documents being compared have actually been
read.

**Do not emit either field, and do not reintroduce an equivalent under another
name.**

### Persisting a report

`--json` emits the report as structured JSON: one or more subjects, each with a
`root` and a `candidates` list. Writing those to a caller-chosen directory is
what lets a set of per-directory runs be handed to generation later rather than
consumed immediately. There is no registry and no state directory: the report
this lane already emits IS the durable representation, and where it lives is
the caller's choice.

A subject whose `candidates` list is empty is an EXPLICIT assessed-null, and it
is worth persisting for exactly that reason -- downstream, "assessed, nothing
found" and "never assessed" must not look alike.

## Decision rules (verdict)

- `GAPS-FOUND` -- at least one candidate survived assessment.
- `COVERAGE-ASSESSED` -- the directory was assessed and no candidate survived.

Both verdicts are about the assessed directory alone. `COVERAGE-ASSESSED` on D
says nothing about D's subdirectories, and it must not be read or reported as
covering them.

**Never emit `COVERAGE-ASSESSED` when `codeFiles` is empty and
`unknownExtensions` is non-empty.** An empty `codeFiles` normally means "no code
directly in this directory to assess" -- but when the discovery script also
reports unrecognized extensions, it means the directory was never READ, because
nothing in it matched a known code, doc, or asset type. That is a discovery
failure, not a clean result, and reporting it as `COVERAGE-ASSESSED` -- the
verdict that means "verified absent" -- claims the opposite of what happened. In
this state, report the unrecognized extensions and stop: name each extension and
its count, state that discovery could not classify the directory, and do not
assess or emit either verdict.

**Neither verdict is ever `COMPLIANT` or `NON-COMPLIANT`, and neither alters a
document verdict.** A CLAUDE.md can be COMPLIANT while its directory is
GAPS-FOUND at the same moment; those answer different questions, and conflating
them is the exact misread this whole capability exists to correct.

There is no `NOT-AUDITED` here. That verdict belongs to the per-file decline
contract, and this verb's subject is a directory it was handed rather than a
file it might decline. A directory it cannot assess is reported as a skip with a
reason, which is the honest equivalent.

**Idempotency is NOT claimed.** Candidate selection is a judgment over ~10^4
source constructs; re-runs may differ. Say so in the report rather than implying
a stable result. The honest posture is "advisory, re-runs may differ, nothing
auto-applies".

**A report is a SAMPLE, not an inventory.** Two thorough reviewers over one
corpus found largely DIFFERENT facts. The report must not imply exhaustiveness.

## The severe-deficiency carve-out

If assessment incidentally establishes that code is defective, that is reportable
ONLY when severe, and only ever as CLAUDE.md content. The bar is deliberately
high:

- **Documenting a hazard can FOSSILIZE a bug.** Writing "this silently truncates
  at 65536" into ambient prose enshrines as behaviour-to-preserve something whose
  right answer was a code change.
- **A stated invariant the code contradicts is a contradiction to surface**, not
  to write down twice.

When in doubt, do not report it. A missed deficiency is recoverable by a code
review; a fossilized one is not.

## Gotchas

- Do not reuse the document lanes' ancestor resolver. It starts at the target's
  PARENT because the target is the CLAUDE.md; here the target is a directory, and
  its own CLAUDE.md is the most ambient file it has.
- Do not treat an empty ambient chain as an error or a skip. It is the finding.
- Do not enter the inline single-subject path. See "Model pinning".
- Do not descend. A file in a subdirectory is not evidence for this directory's
  candidates, and a fact about it is a CV-3 violation however true it is.
- Do not retarget a candidate to the nearest EXISTING CLAUDE.md when the assessed
  directory has none. Ambient budget is PER FILE; putting a fact in a file that
  already loads elsewhere does not make it reach this code. The destination is
  the assessed directory's CLAUDE.md whether or not that file exists yet --
  proposing content for a file that does not exist is the canonical case.
- Do not read the fact that a directory holds only subdirectories as nothing to
  report. It has no direct code, so it has no candidates from THIS lane; its
  content comes from composing its children's documents in `generation-lane.md`.

## Anti-patterns

- **Building this as a hazard sweep.** Two independent adversarial reviews
  rejected that design; the reasons are in `references/coverage-gap.md` under
  "Why the obvious fix is wrong". The predicate is not enumerable, ambient budget
  does not net out across files, and documenting a hazard can fossilize a bug. Do
  not rediscover it.
- **Emitting a defect list.** See the opening section. This is the failure the
  scope correction exists to prevent.
- **Routing candidates to code fixes or tests.** An earlier revision of the spec
  routed code-fix -> make-it-loud -> document, with documentation last. That is
  retired: it answered a question this verb is not asking.
- **Reporting a fact that is already ambient.** Duplication the placement spine
  forbids, and the second of the four negative controls.
- **Nominating a destination above the subject.** Naming a parent because the
  fact "feels broader" is a judgment made without the evidence -- the parent's
  other children were never opened. Report it at the subject and let hoisting at
  the parent decide, where the documents being compared have been read.
- **Widening the subject to a subtree.** It looks like better recall and is
  strictly worse: the same fact then arrives once per enclosing directory, so a
  parent duplicates every descendant's findings and downstream de-duplication
  compares facts against copies of themselves.

## Cross-references

- Why this verb exists, and the four negative controls -- `references/coverage-gap.md`.
- What earns ambient cost, including analysis depth -- `references/standards/coverage-standards.md`.
- Composing a parent from its children's documents, and the bottom-up order that requires -- `generation-lane.md`.
- Where content sits WITHIN the destination document (the destination itself is the assessed directory) -- `references/cohesion-principles.md`.
- The observation kinds Step 3 reuses -- `references/standards/claude-md-standards.md`.
- The audit verb's procedure (a different subject, different contract) -- `audit-lane.md`.
