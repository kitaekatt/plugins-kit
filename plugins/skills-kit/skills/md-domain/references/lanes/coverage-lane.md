# The coverage lane

The coverage procedure. Unlike `audit` and `generate`, this one is NOT
parameterized by artifact -- it has exactly one subject shape, and its subject is
CODE.

**What kind of operation this is.** Coverage is not a peer of `audit`. It renders
no compliance verdict, so it is not an audit; it creates no document, so it is not
generation either. It is the DISCOVERY step that feeds generation and
regeneration: it reads code, discovers facts, and names the destination each fact
belongs in. Being re-homed under that family changes nothing about what it may do
-- REPORT-ONLY IS A PROPERTY OF THIS ENTRY POINT, not an accident of the verb
being listed separately. The lane binds no remediate workflow and never writes,
whoever calls it and whatever the family is called.

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

**Unit: `(code subtree, its ambient CLAUDE.md chain)`.**

This is why coverage cannot be a criterion inside `audit_claude_md`: the per-file
lanes enumerate CLAUDE.md files, and no criterion can have a subject its lane
cannot enumerate. The decisive case is a subtree with NO CLAUDE.md at all, which
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
normally has exactly ONE subtree, so reusing that shortcut would mean the common
case silently runs off-pin. Always go through the workflow.

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

Announce the run by name and scope before Step 2, per SKILL.md's "Naming and
scope announcement": the analysis name (`Coverage analysis`), the subtree, the
code-file count, and the size of the ambient chain.

### Step 2 -- Discover (mechanical)

Run `scripts/discover_coverage.py <directory>` (or `--diff`) and consume its
`subjects[]`. It is side-effect free and reads no file contents.

Per subject it returns `root`, `rootExclusion`, `codeFiles`,
`ambientClaudeMdPaths`, `skipped`, `noisePruned`, and `unknownExtensions`.

`unknownExtensions` (a `{<ext>: <count>}` map) has the same standing as the
structural exclusions above: surface it in the report, never drop it silently.
It names every file the discovery script could not place as code, doc, or a
recognized asset type -- the set the whole capability exists to protect
against silently losing.

Two properties of the ambient chain are load-bearing and are the script's job,
not the model's -- do not recompute them by eye:

- The chain **includes** a CLAUDE.md at the subtree root itself.
- The upward walk **stops at the nearest `.git`**, so a nested repository never
  inherits the outer repository's chain.

An **empty** `ambientClaudeMdPaths` is not an error. It is the strongest possible
finding: nothing loads for this code at all.

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
- Apply the candidate ceiling, which is PER SUBTREE rather than per run -- a
  per-run cap divided across subjects gives each subtree an arbitrary share that
  shrinks as the run widens. When it is hit, SAY SO, and state the aggregate for
  a multi-subtree run. Silent truncation in the verb that reports silent
  truncation would be its own joke.

`coverage-detect.js` refuses to run while `refs.criteria` names no document, so
this seam cannot be crossed by accident.

### Step 4 -- Report and stop

```
## Coverage analysis -- <subtree>

Ambient chain (<N>, root-most first):
  - <path>
  ...
  (or: NONE -- no CLAUDE.md loads for this subtree)

Code files assessed: <N>
Skipped: <N>  (<reason> <path>, ...)
Unknown extensions: <N>  (<ext>: <count>, ...)  (or: NONE)
Candidate ceiling (per subtree): <not reached | REACHED -- N not shown>
Analysis depth: basic | advanced
<only for a non-interactive implicit basic selection: defaults: depth=basic>

### Candidates
- [<FINDING-CONVERTIBLE | CONTEXT-ONLY>] <fact>  ->  destination: <CLAUDE.md the placement algorithm selects>
  <why it belongs there, and why it is not ambient today>
  evidence: <file:line>[, <file:line> ...]

### Coverage verdict
GAPS-FOUND | COVERAGE-ASSESSED
Meaning: <basic: not found within budget | advanced: verified absent>
```

Each candidate carries the destination the placement algorithm selects --
ambient for the code it describes, per `references/cohesion-principles.md`, not
wherever is convenient. Reporting a candidate is not a commitment to write it.

The tier prefix and the evidence line are not decoration: CV-4 requires the
classification to be REPORTED, and CV-7 is fail-severity on the file-and-line
citation. Rendering a candidate without them satisfies the criteria in the
assessment and drops them at the only point a reader sees. A CONTEXT-ONLY
candidate is a normal result -- the tier exists so a reader can tell the facts a
reviewer could act on from the ones that only orient, not to rank them.

Then STOP. No Q&A, no edits, no follow-up pass.

## Handing the report to generation

Stopping is where THIS lane ends, not where the work has to. A caller holding a
coverage report may invoke the generation lane to write any candidate up. That
is a separate run of a separate lane, and it takes nothing away from report-only:
this lane still writes nothing, whoever calls it and whatever they do next.

The two lanes already meet, and the join needs no new machinery:

- **The `destination` field IS a resolved placement.** Every candidate carries
  the CLAUDE.md the placement algorithm selected. `generation-lane.md` step 2
  recognizes exactly this case -- "when a placement decision arrives already
  resolved ... follow it. Do not re-invoke the placement framework for it."
  A coverage candidate is that case, alongside an audit remediation naming the
  destination and an orchestrator directive.
- **`fact`, `why` and `anchors` are the content.** Generation's step 1
  precondition is a fact needing a home; the candidate is a fact, its
  justification, and its `file:line` evidence, with the home already named.

Two things the caller has to do, because neither lane does them:

1. **Group by `destination` first.** Generation is single-invocation -- one run
   writes one document. A report spanning N destinations is N generation runs,
   each taking that destination's candidates together, so the document is
   authored as a whole rather than appended to N times.
2. **Carry the tier through, do not filter on it.** CONTEXT-ONLY is admissible
   content, not rejected content -- CV-4 classifies candidates, it does not gate
   them. A caller writing up only the FINDING-CONVERTIBLE ones is making an
   editorial choice and should say so; the tier is a signal to the reader, not a
   filter the lanes apply.

What the caller must NOT do is treat the report as pre-approved. Reporting a
candidate is not a commitment to write it (above), so the decision to generate
is the caller's and is made per candidate, not per report.

## Decision rules (verdict)

- `GAPS-FOUND` -- at least one candidate survived assessment.
- `COVERAGE-ASSESSED` -- the subtree was assessed and no candidate survived.

**Never emit `COVERAGE-ASSESSED` when `codeFiles` is empty and
`unknownExtensions` is non-empty.** An empty `codeFiles` normally means "no code
here to assess" -- but when the discovery script also reports unrecognized
extensions, it means the subtree was never READ, because nothing in it matched
a known code, doc, or asset type. That is a discovery failure, not a clean
result, and reporting it as `COVERAGE-ASSESSED` -- the verdict that means
"verified absent" -- claims the opposite of what happened. In this state,
report the unrecognized extensions and stop: name each extension and its
count, state that discovery could not classify the subtree, and do not assess
or emit either verdict.

**Neither verdict is ever `COMPLIANT` or `NON-COMPLIANT`, and neither alters a
document verdict.** A CLAUDE.md can be COMPLIANT while its subtree is
GAPS-FOUND at the same moment; those answer different questions, and conflating
them is the exact misread this whole capability exists to correct.

There is no `NOT-AUDITED` here. That verdict belongs to the per-file decline
contract, and this verb's subject is a directory it was handed rather than a
file it might decline. A subtree it cannot assess is reported as a skip with a
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
- Do not let a candidate's destination default to the nearest CLAUDE.md. Ambient
  budget is PER FILE; putting a fact in a file that already loads elsewhere does
  not make it reach this code.

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

## Cross-references

- Why this verb exists, and the four negative controls -- `references/coverage-gap.md`.
- What earns ambient cost, including analysis depth -- `references/standards/coverage-standards.md`.
- Where a candidate's destination goes -- `references/cohesion-principles.md`.
- The observation kinds Step 3 reuses -- `references/standards/claude-md-standards.md`.
- The audit verb's procedure (a different subject, different contract) -- `audit-lane.md`.
