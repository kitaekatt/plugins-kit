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
| refutation stage | advanced depth only; `verify: false` switches it off |
| input modes | inline `subjects[]`, or `subjectsFile` + `subjectCount` |
| subjects per agent | `batchSize`, default 8 |
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

## Feeding a wide run: input modes and batching

A single-directory run needs none of this. It matters when a caller wants a whole
tree covered, which is one subject per directory and can be four figures of them.

### The two input modes

`workflow/coverage-detect.js` takes its subjects one of two ways, and it is an
error to pass neither.

- **Inline `subjects[]`** -- the array `discover_coverage.py` returns, passed
  straight through. Right for a run of a few directories.
- **`subjectsFile` + `subjectCount`** -- an ABSOLUTE path to a **JSONL** file
  holding one subject record per line (each line exactly the shape of an inline
  entry), plus the number of lines in it. Right for a wide run. **Produce both
  with the shipped producer, never by hand:**

  ```
  scripts/coverage_subjects.py build <dir> [<dir> ...] --out <file.jsonl>
                               [--tree] [--overrides <file>]
  ```

  It prints the two Workflow args as JSON ready to paste, and the count it prints
  is the length of the very list it serialized -- re-read and re-counted from the
  written file before it publishes either, so a file and a count that disagree
  cannot both survive. Without `--tree`, each named directory is exactly one
  subject -- the lane's own unit.

  `--tree` makes every directory under each named directory its own subject,
  which is how a four-figure corpus is enumerated without a hand-maintained
  list. It applies three prunes, and each is `discover_coverage`'s own rule
  IMPORTED rather than restated, so a name added there reaches `--tree` with no
  change to the producer:

  - **Noise** (`NOISE_DIR_NAMES`) -- build output and tooling state:
    `Intermediate`, `Saved`, `Binaries`, `DerivedDataCache`, `__pycache__`,
    `.venv` and the rest, plus every dot-directory EXCEPT `.claude`, which holds
    hand-authored team configuration and is a legitimate subject. Matched
    case-sensitively, as that module matches it.
  - **Structural** (`root_exclusion`) -- vendored, generated, and
    content-detected vendored bundles.
  - **VCS-ignored** (`ignored_paths`).

  It then drops a directory with no direct code and nothing unrecognized.

  **Do not rely on the VCS-ignore prune to catch build output.** It does on a git
  repo whose `.gitignore` covers those directories; a Perforce workspace
  typically ignores almost nothing (`p4 ignores` may return only `.p4root` and
  `.p4config.txt`), so on Perforce the noise list is the only thing keeping
  `Intermediate/` and `Binaries/` out of the subject set.

  **`--overrides <file>` is how a directory the prunes exclude gets back in.**
  A real corpus is almost always "these trees, PLUS these specific directories,
  NOT recursively", and `--tree` is a whole-invocation flag -- so without this
  the only way to reinstate an exception is to name it as a root, which
  tree-walks it. That is not a smaller mistake than leaving it out: reinstating
  9 first-party directories parked under vendored parents this way pulled in 116
  vendored descendants, and the corpus GREW, which reads as more coverage. The
  override file lists one directory per line, each added as a single subject,
  non-recursively, regardless of any prune. Blank lines are ignored and `#`
  starts a comment. The run reports three numbers -- N from the named roots, M
  added by override, K redundant -- so the override population stays legible as
  its own figure rather than folded into a total, and a stale entry that is
  already a subject shows up as redundant instead of being silently inert. A
  listed directory that does not exist, or that has nothing to assess, is an
  ERROR that names it and writes no file.

  **An override entry is a claim the caller is making AGAINST the plugin's own
  prune, so it should carry its evidence somewhere the next reader can find.**
  The plugin cannot know that `ThirdParty/SFDate` is first-party build glue and
  should not be taught to -- that is local knowledge, and the override file is
  where it arrives as input. Record why each entry is there: a comment on the
  line, or a pointer to the audit that established it. Ours came from a
  p4-history audit recorded in the consuming project, not in the plugin.

  Two further consequences worth knowing before you read a subject list:

  - **Naming an ignored directory opts its whole tree in.** Ignore-pruning
    applies to DESCENDANTS the user did not name. If the root you point at is
    itself ignored, the pruning is off for that walk -- otherwise
    `build --tree ./Binaries` would return `Binaries` alone.
  - **First-party build glue under a vendored parent is dropped**, inheriting the
    accepted false-positive class `discover_coverage.py` documents: a
    path-segment name rule cannot tell a vendored library from a team-authored
    `Foo.Build.cs` sitting beside it under `ThirdParty/`. Reinstating one is the
    consuming repo's call -- name that directory explicitly (it then has no
    `rootExclusion`, because the rule matches a directory's own name and not its
    ancestry), or move it out from under the vendored parent.

The reason the second mode exists is a property of the carrier, not a preference:
**a workflow script has no filesystem.** Everything it is given arrives through
the ORCHESTRATOR'S context, so inline subjects cost the orchestrator roughly
2.3 KB per directory -- megabytes over a wide corpus, spent in the one context
that must stay lean. In `subjectsFile` mode the script holds only the path and a
line range per batch; the AGENTS read their own slice, and agents do have
filesystem tools.

JSONL rather than a JSON array is what makes that work: a slice is a LINE RANGE,
so an agent extracts exactly its own subjects (`sed -n 'START,ENDp'`, or a read
with an offset and a limit) and never reads the whole file. A JSON array would
force every agent to parse the entire corpus to find its own entries, which is
the cost being avoided wearing a different hat.

The path must be ABSOLUTE and the lane refuses a relative one. The agents that
read it are separate processes whose working directory the lane does not control,
so a relative path names a different file for each of them -- or none, which
would read as an empty batch rather than as an error.

`subjectCount` is required in this mode and the lane refuses without it: it
cannot count the lines itself, and a guessed count either truncates the run or
dispatches agents at empty line ranges.

**Precedence: inline `subjects[]` WINS when both are supplied**, and the ignored
file is named in a run note and in the log line -- never dropped silently. Inline
data was handed to the lane directly, and it is the only mode in which the lane
can see the subject fields, which is what lets the discovery-failure refusal run
BEFORE any tokens are spent. When two inputs disagree, the one with the stronger
guarantee wins.

### Provenance: what each mode can actually verify

Every record carries a `provenance` field, and the two values are not
interchangeable.

- **`harness-verified`** (inline mode). `root` and `codeFiles` are TRUSTED INPUT.
  Identity and anchor membership are checked against data the agent never
  supplied.
- **`agent-attested`** (subjectsFile mode). The lane has no filesystem, so `root`
  and `codeFiles` are ECHOED by the agent from the record it read. The subjectKey
  still binds each result to a line the lane requested, and anchors are still
  checked against the echoed list -- which catches the incidental mislabel,
  because an agent that assessed A while labelling it B echoes B's file list and
  anchors A's files. It does NOT catch a self-consistent fabrication: a run can
  report a clean assessment of a directory nobody opened.

The run summary says which mode was used and warns on the attested one. This is a
real difference in what the report means, not a formality.

#### Verifying an agent-attested run

The caller has the subjects file; the lane does not. So the check the lane cannot
do, the caller can, deterministically and without a model. **It ships with the
plugin -- do not hand-roll it:**

```
scripts/coverage_subjects.py verify <report.json> <subjects.jsonl>
```

Exit 0 means verified; non-zero prints every failing subject and why. It makes
exactly the three checks the lane structurally cannot:

1. **Identity.** Every requested key `L1..LN` is present exactly once, with
   `status` either `ASSESSED` or `NOT-ASSESSED`. A missing key is a subject
   neither assessed nor accounted for; a key outside the file, a duplicate key,
   or an inline-mode `S<n>` key means this is the wrong report for this file.
2. **Roots.** Every returned `root` matches the root on the line its `subjectKey`
   names. This is what catches an assessment filed under a directory it does not
   describe, and a root invented outright.
3. **Anchors.** Every candidate anchor names a file in that same line's
   `codeFiles` and carries a line number, and every `destination` is that line's
   root.

It also fails when the subjects file was edited after `build` wrote it -- the
sidecar records the count and a digest -- because a report verified against a
different file than the run consumed is not verified at all.

**Any run whose candidates will be promoted without a human reading them MUST
pass this check, or must use inline mode instead.** A `verify` that was never run
is the same gap as a criterion that was never reached.

### Batching: what it preserves, and what it only bounds

One agent per subject was deliberate CONTEXT ISOLATION: it is what stopped one
directory's code from bleeding into another directory's candidate facts. It is
also where the run's fixed cost lives -- every agent re-reads the same criteria
documents, about 180 KB across the three of them, which on a small directory is
most of what the agent spends. `batchSize` (default 8) puts several subjects
through one agent so that read is paid once per batch instead of once per subject.

**Batching does not preserve isolation. It BOUNDS contamination.** State it that
way: an earlier revision of this lane claimed preservation and an adversarial
review was right to reject the claim. Four mechanisms, of which only the last
three are enforcement:

1. **Sequential turns with a SCOPED reset.** The brief requires the subjects to
   be assessed strictly one at a time, in order, discarding the previous
   subject's code, ambient documents, and candidate facts before opening the
   next. The criteria documents are explicitly exempt -- they are identical for
   every subject, and keeping them is the whole economy. This is hygiene asked
   for in a prompt. It is not a guarantee and cannot be made one.
2. **Identity by harness-issued `subjectKey`.** Every requested subject is issued
   a key (`S1`, `S2`, ... inline; `L<line>` in subjectsFile mode) and the agent
   echoes it. Results are matched BY KEY, never by position. This is the
   mechanism that stops the worst failure: positional matching over a batch that
   omitted its middle subject shifts every later result one slot, and the inline
   root overwrite then stamps the wrong directory onto real findings --
   manufacturing the exact contamination the design exists to prevent. An
   unreturned key becomes `BATCH-INCOMPLETE`; an unrequested or duplicated key is
   discarded and counted.
3. **Anchor MEMBERSHIP against the subject's own code-file list.** Every anchor
   must name a file that is IN that subject's `codeFiles` and must carry a line
   number. Not "under the root": a path-prefix test admitted an empty string, a
   file that does not exist, a foreign file that shared a directory name, and any
   bare filename at all. A candidate with a rejected anchor is DROPPED, counted
   in `totals.isolationViolations`, and named in its subject's notes and in the
   run summary. Paths are compared after normalizing separators, `.`/`..`, and
   Unicode, case-folded only when a path is Windows-shaped.
4. **`destination` is DERIVED, not accepted.** Generation groups by that field,
   so a wrong value re-homes a fact into a document that never earned it. It is
   overwritten from the subject and corrections are counted.

A subject stripped of every candidate this way is reported as `COVERAGE-ASSESSED`
with the drop named -- not as `GAPS-FOUND` over evidence that was thrown away.

**The residual, stated plainly.** A fact REASONED from subject A's code but
ANCHORED to a real, in-list file of subject B passes every check above. Anchors
prove a file was named; they never prove a claim was derived from it. No amount
of string work closes that, so batching bounds contamination to this case and
**does not eliminate it**. A non-zero `isolationViolations` count is a signal to
re-run the affected subjects at `batchSize` 1, not a number to tolerate -- and a
zero count is not proof of clean provenance.

`batchSize` therefore is a RISK dial as well as a cost dial. 8 is the default and
the largest value this lane claims a story for; the fixed-cost saving is already
flat by then. Use 1 -- the pre-batching behaviour -- for any run whose candidates
will be promoted without a human reading them. `batchSize` does not change the
candidate ceiling, which stays PER DIRECTORY: a batch of 8 may return up to 8 x
the ceiling.

## The pipeline

```
Step 1  Resolve + INTENT GATE
Step 2  Discover (mechanical, side-effect free)
Step 3  Assess (coverage-standards.md)
Step 3b REFUTE surviving candidates (advanced depth only)
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
JavaScript workflow.

**The same rule holds for ANY carrier, not just the workflow.** If the
assessment is handed to a subagent, a background CLI, or a hand-written brief,
that carrier gets the standards document verbatim by absolute path too. A
summary of the criteria is not the criteria, and worked examples inside a brief
outrank the brief's own abstract rules -- a brief that forbids repo-wide facts
while illustrating "good" facts with repo-wide project rules will produce
repo-wide bloat. Criteria travel whole or they do not travel.

Apply the depth selected at the intent gate: `basic` uses a
bounded read and one assessment pass; `advanced` reads every source file
completely, discovers invariants before assessment, and hands the surviving
candidates to the separate refutation stage below. That stage is a DISPATCH of
its own, not a clause of this one -- an agent re-reading its own candidates in
the context that produced them is a self-check, and Step 3b is what the phrase
"verified absent" names.

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

### Step 3b -- Refute (advanced depth only)

Everything the reducer does enforces FORM: subject identity, anchor membership,
`destination`, the verdict rule. Whether a candidate is TRUE is a semantic
property, and no string check reaches it. This stage is the one that does.
`../standards/coverage-standards.md:270-292` is the authority on what "verified
absent" may claim; this section is what the lane runs to earn it.

**Shape.** After reconciliation and before the report, ONE agent dispatch per
subject whose `status` is `ASSESSED` and which holds at least one candidate.
Subjects with no candidates are not dispatched -- there is nothing to refute.
The dispatch is pinned `opus` + `high` for the same reason detection is:
refuting a universal claim means reading every file in a directory and noticing
the one that does not conform.

Each dispatch carries three things and nothing else:

- the subject `root`;
- that subject's DIRECT code-file list, stated in the brief as exhaustive. The
  reducer keeps this list aside keyed by subject precisely for this stage --
  the echoed list is stripped from every returned record, so nothing extra
  travels back to the orchestrator that `subjectsFile` mode exists to keep out;
- the candidates with their anchors, each under an index the prompt ISSUES.
  Verdicts are matched back by that index, never by position in the returned
  array -- the same rule `reconcileBatch` applies to subject keys.

The context is FRESH: the verifier has not seen the assessment's reasoning and
is not asked to agree with it.

**Per subject, not per candidate.** A subject's candidates share one read of one
directory, so per-subject amortizes that read across them; per-candidate would
pay it again for every fact.

**When it runs.** `advanced` depth AND `verify` not set to `false`. Passing
`verify: false` switches it off at advanced depth, and nothing at `basic` depth
runs it. **When it does not run, the run says so on its own summary line** --
the caller is never left to infer it from the depth, and `COVERAGE-ASSESSED`
from an unverified run means "not found within budget", NOT "verified absent".
`totals.verifyRan` is the field a downstream consumer reads before believing
the stronger reading.

**Its scope is TRUTH ONLY, and that is measured rather than stylistic.** It may
delete a candidate for being FALSE and for nothing else -- not because the fact
looks unworthy of ambient cost, not because a comment at the site would serve
better, not because the evidence is dull. Those criteria are applied once, at
assessment. The boundary is drawn from measurement: an improvised gate over one
corpus ran four checks, two testing truth (universal quantifiers, ordering) and
two re-judging admission (evidence location, already-stated-at-site). Re-judged
blind against the shipped criteria, the two admission checks OVERTURNED their
own prior kills at 76% and 67%, while the two truth checks held at 33% and 50%.
Refutation is a posture that finds what it is pointed at: pointed at truth it
corrects the record, pointed at value it manufactures rejections.

**What a verdict may do to a record.** Five cases, and only the first two change
anything:

1. **`STANDS`** -- the candidate survives. It gains `verified: true` only when
   the verdict's `filesRead` equals its `filesInDir`; on a shortfall it gains
   `verified: false`, `readComplete: false`, and the two figures, because the
   file the refuter did not open is exactly where a counterexample to a
   universal claim would sit (`totals.verifyPartialStands`). An optional
   `narrowing` field carries the restatement that WOULD stand when one
   over-reaching clause is the only problem. It is handed to the caller and is
   never auto-applied: a fact rewritten by its verifier has been proposed by
   nobody.
2. **`FALSIFIED` with a `file:line` counterexample** -- the candidate is DELETED,
   and the full record of the deletion -- fact, anchors, tier, counterexample,
   quote -- lands in the subject's `falsified` array. The notes carry a short
   summary naming the first three; the array is the record, and it never
   truncates. A partial read does not withhold a kill: an unread file can only
   ADD counterexamples, so it cannot rescue a fact a read file contradicts.
3. **`FALSIFIED` with no counterexample** -- the VERDICT is discarded and the
   candidate is KEPT, `verified: false`, with a note naming it
   (`totals.verifyUnsupported`). An unsupported deletion is the same
   unaccountable rejection this stage exists to replace.
4. **No verdict row for a candidate**, in a subject whose other candidates WERE
   answered -- kept, `verified: false`
   (`totals.verifyCandidatesUnanswered`), with a note. Counted apart from case 5
   because it is a different failure: a verdict set complete except at its tail
   is what output truncation looks like, and folding it into the whole-subject
   number would point an operator at the dispatch instead of at the budget.
5. **No verdict set returned for the subject at all** -- every candidate is
   kept, the subject is marked `verified: false`, and its notes say to treat its
   candidates as depth `basic`, because it has not earned "verified absent".
   Its whole candidate count lands in `totals.verifySubjectsUnreturned`. Dropping
   candidates here would let an infrastructure failure read as a clean
   directory, which is the confusion `DISCOVERY-FAILED` exists to prevent one
   step upstream.

**The verdict is re-derived after deletions**, with the same expression the
reducer uses, so "GAPS-FOUND iff candidates" stays true by construction rather
than being asserted in two places that can disagree. A subject whose every
candidate was falsified therefore reports `COVERAGE-ASSESSED` with the
deletions named.

**Every verdict carries a VERBATIM QUOTE for any criterion it invokes.** The
schema has a `quote` field for it, and empty is correct for a pure
falsification, which needs no criterion at all. A verifier that applies a rule
it cannot quote from the criteria document has INVENTED that rule -- see "The
promotion gate" below for the measured case that motivated the field.

**Read counts are reported and tallied.** Each verdict states `filesRead`
against `filesInDir`; a shortfall is counted in `totals.verifyPartialReads` and
noted on the subject. A universal claim judged without opening every file in the
directory has not been checked, and the two counts are what make that visible
afterwards.

**Totals this stage adds**, all on the run's `totals` object:

| Field | Meaning |
|---|---|
| `verifyRan` | whether the stage ran at all |
| `verified` | surviving candidates a verdict upheld on a COMPLETE read |
| `falsified` | candidates deleted against a counterexample |
| `verifySubjectsUnreturned` | candidates kept because their subject got no verdict set at all |
| `verifyCandidatesUnanswered` | candidates kept because their own row was missing from an answered set |
| `verifyUnsupported` | `FALSIFIED` verdicts discarded for naming no counterexample |
| `verifyPartialReads` | verdicts judged against fewer files than the directory holds, in BOTH directions |
| `verifyPartialStands` | of those, the ones that STOOD -- the exposure, since a partial FALSIFIED is sound |

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
Refutation: RAN (<N> falsified, <N> upheld) | DID NOT RUN
<only for a non-interactive implicit basic selection: defaults: depth=basic>

### Candidates
- [<FINDING-CONVERTIBLE | CONTEXT-ONLY>] <fact>  ->  destination: <assessed directory>/CLAUDE.md
  <why it belongs there, and why it is not ambient today>
  evidence: <file:line>[, <file:line> ...]
  <verified: yes | no -- and, when one was returned, narrowing: <restatement>>

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

The refutation line is not decoration either. `COVERAGE-ASSESSED` means two
different things depending on whether Step 3b ran, and a reader holding only the
report has no other way to tell which they have. Render it even when the stage
did not run -- especially then, since that is the case a reader is most likely to
over-read. A `narrowing` is rendered as the verifier returned it and is never
folded into the fact.

Then STOP. No Q&A, no edits, no follow-up pass.

## The promotion gate

This lane hands candidates back for a decision the CALLER makes per candidate.
At one directory that decision is a person reading five facts. At corpus scale
it is a FILTERING PIPELINE over hundreds of them -- and a pipeline is a thing
with steps, an order, and a rule about what may reject. Both corpus-scale runs
of this lane had to improvise one, because the lane shipped the judgment without
shipping the procedure, and both improvised versions drifted from the criteria
document they claimed to apply.

So the procedure is named here and is not a caller's invention. **Every
corpus-scale consumer runs these four steps, in this order:**

1. **Filter to `status: ASSESSED`.** Nothing else is a result. `NOT-ASSESSED`
   covers `BATCH-INCOMPLETE` and `DISCOVERY-FAILED`, whose empty candidate lists
   mean "never read", not "nothing found" -- see "Decision rules (verdict)".
2. **Run the mechanical checks.** `scripts/coverage_subjects.py verify
   <report.json> <subjects.jsonl>` -- identity, roots, anchor membership,
   destinations, and the subjects-file digest. Deterministic, no model, exit 0
   or a list of failing subjects. It is mandatory for any agent-attested run
   whose candidates will be promoted without a human reading them; see
   "Verifying an agent-attested run".
3. **Run the semantic refutation stage.** Step 3b, which means running the lane
   at `advanced` depth without `verify: false`. No separate entry point ships
   for it -- a report produced at `basic` depth, or with the stage switched off,
   has had no independent check of any candidate's truth, and reaching this step
   means re-running those subjects at advanced depth rather than approximating
   the stage by hand.
4. **Hand the survivors to generation**, per the section below -- grouped by
   `destination`, ordered bottom-up, tier carried through.

Steps 2 and 3 are not interchangeable and neither substitutes for the other:
step 2 checks that a record describes the directory it claims to, step 3 checks
that its claims are true of that directory. Step 2 catches a misfiled
assessment; step 3 catches a fabricated invariant in a correctly filed one.

### The guardrail: a judge that cannot quote the document has invented the rule

**Every verdict in every judging step must carry a VERBATIM QUOTE from the
criteria document for the rule it applied.** Step 3b enforces this in its schema
(`quote`, empty only for a pure falsification). Any judging a caller adds around
these steps carries the same obligation, and the obligation is what makes drift
DETECTABLE -- a rule with no quotable source in
`../standards/coverage-standards.md` was invented by the judge, and that is
readable off the record afterwards instead of requiring someone to reconstruct a
brief nobody kept.

This is not a hypothetical failure mode; it is the measurement that motivated
the field. On the corpus-scale run that prompted this section, 68 of 81
rejections were recorded under a letter from the improvised brief rather than
under any named criterion from the standards document -- so the rejections
looked systematic while being traceable to nothing. One invented rule, that
evidence outside the assessed directory fails the evidence floor even when the
fact is true, accounted for 25 of them; the standards document contains no such
rule and CV-1's own ADMIT example contradicts it, and 19 of those 25 rejections
were wrong. The quote requirement is what let that be established from the
record rather than re-litigated from memory.

Two consequences worth stating plainly:

- **A rejection is as accountable as a deletion.** Step 3b already refuses to
  delete on a `FALSIFIED` verdict that names no counterexample. A caller-side
  rejection that names no criterion is the same unaccountable act arriving one
  step later, where no schema is watching for it.
- **An improvised brief outranks its own abstract rules in practice.** The
  criteria travel whole and by absolute path for exactly this reason (Step 3);
  a restatement of them inside a filtering brief becomes the operative document
  the moment the two disagree, and nobody notices, because the brief is the only
  thing the judge actually read.

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

**`BATCH-INCOMPLETE` is the batching-era sibling of the discovery failure.** A
batch agent is asked for N result objects and may return fewer -- a key it
omitted, a blank or malformed JSONL line, a line range past the end of the file.
Each unreturned key is emitted as `BATCH-INCOMPLETE` with an empty candidate list
and tallied apart from both verdicts. "The agent skipped it" and "the directory
was assessed and nothing was found" are the two states this lane exists to keep
apart, and folding the first into the second is the same fake pass the
discovery-failure refusal prevents. Re-run those subjects, alone or at a smaller
`batchSize`. Result objects the batch was NOT asked for are discarded and counted
(`totals.extraReturned`, `totals.identityUnmatched`); a `root` returned twice is
counted (`totals.duplicateRoots`) rather than resolved, because the lane cannot
tell which of the two is real.

**Every record carries a `status`, and it is the only safe gate.** `ASSESSED` or
`NOT-ASSESSED`. It exists because an empty `candidates` list means two opposite
things -- "assessed, nothing found" and "never read" -- and a consumer reading
only `candidates` cannot tell them apart. `generation-lane.md`'s entry check is
exactly such a consumer: it admits a subject that has either a `reportPath` or a
non-empty `candidates` list and never looks at the verdict.

So the caller's gate, and it belongs to the caller because neither lane applies
it: **filter a persisted report to `status: ASSESSED` before handing any part of
it to generation.** A run whose summary shows fewer COMPLETED than REQUESTED is
not a finished report; finish it, or hand over only the assessed subjects and say
which were left out.

The run summary states `<completed> of <requested> requested directory/ies
COMPLETED (<n> NOT assessed)` for the same reason -- a wide run must not read as
whole because no single line looks wrong.

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
- **Improvising a promotion pipeline.** The four steps and the quote guardrail
  are shipped; re-deriving them per run is how a judging rule that appears in no
  criteria document ends up applied to hundreds of candidates. See "The
  promotion gate".
- **Letting the refutation stage re-judge admission value.** It deletes for
  falsity and nothing else, and the measurement behind that boundary is in
  Step 3b. A stage pointed at value does not raise precision; it manufactures
  rejections.
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
