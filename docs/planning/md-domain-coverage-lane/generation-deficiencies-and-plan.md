# CLAUDE.md generation: measured deficiencies and a remediation plan

Status: Part 1 (measured deficiencies) is settled measurement with its own
retractions. Part 2 is the plan after its one blocking decision was made --
P0 was answered on 2026-08-12 by owner decision (Option C) and every other item
has been reviewed against it. Nothing in Part 2 is implemented, and the
shipped-document and workflow-prompt amendments Part 2 lists have NOT been made.

Subject: the md-domain `coverage` -> `generate` chain, as exercised on
`D:/dev/woodworking-sim` (46 coverage reports, 310 candidates, 45 emitted
CLAUDE.md files, 4 recorded null branches).

Standard: the two references in `D:/dev/code-review-research`,
`docs/reference/claude-md-generation-method.md` (the executed six-phase
pipeline) and `docs/reference/md-domain-coverage-gaps.md` (that method compared
against md-domain 0.43.0). Both are pinned to 0.43.0, so their DESCRIPTION of
md-domain is stale; their normative principles are not.

Deficiencies are marked `[REF]` when a reference states the principle, and
`[OBS]` when the corpus shows a defect neither reference addresses. Every
number below was computed from the reports and the emitted corpus, not
estimated.

## Part 1 -- measured deficiencies

### G1 `[OBS]` Three incompatible candidate schemas in one corpus

The 46 reports carry four distinct candidate shapes:

| Shape | Reports | Candidates |
|---|---:|---:|
| `convertibility, destination, evidence, fact, rationale, scope, sibling_overlap` | 28 | 155 |
| `anchors, destination, fact, tier, why` | 10 | 99 |
| `anchors, destination, fact, severeDeficiency, tier, why` | 7 | 56 |
| (no candidates) | 1 | 0 |

The same concepts appear under different keys -- `tier` vs `convertibility`,
`why` vs `rationale`, `anchors` vs `evidence`. No downstream consumer can read
a field without first sniffing which shape it got. This is the root cause of
several numbers below, because half the corpus is measured against a key the
other half does not have.

Neither reference anticipates this. `md-domain-coverage-gaps.md:289` notes a
single contract-to-schema seam (CV-4's tier has no schema field); it does not
contemplate concurrent divergent shapes.

### G2 `[OBS]` `destination` is unnormalized free text

203 of 310 candidates (65.5%) carry a destination that is not a clean
repo-relative path. Observed forms, all in the same corpus:

```
CLAUDE.md (repo root)
D:/dev/woodworking-sim/src/CLAUDE.md
D:/dev/woodworking-sim/src/inventory/CLAUDE.md (new)
src/CLAUDE.md (does not exist yet)
```

Absolute Windows paths, parenthetical annotations, and a bare `CLAUDE.md` with
a prose gloss. `scope` is worse: five candidates put an entire paragraph of
prose into the field where an enum belongs. A router cannot act on any of it.

`md-domain-coverage-gaps.md:427-432` frames representation as an open question.
The free-text drift is ours.

### G3 `[REF]` Candidates route to documents that cannot exist

20 candidates name a destination with no document on disk:

| Destination | Candidates | Direct code files |
|---|---:|---:|
| `godot/` | 13 | 0 |
| `kernel/` | 6 | 0 |
| `godot/extensions/woodkernel/` | 1 | 0 |

All three have zero direct code files, so under the settled model they are not
coverage subjects and no generation run will ever write them. **Coverage's
CV-3 placement rule and generation's subject rule disagree about what a valid
destination is**, and 20 admitted facts fall into the gap.

This is audit finding S1 measured. The reference's answer is the structural
unit: "A parent with multiple meaningful child units can become a structural
unit even when the parent has no direct source"
(`claude-md-generation-method.md:85-87`).

### G4 `[REF]` A quarter of candidates name a directory the run will not write

78 of 310 (25.2%) name a destination other than the assessed directory, while a
generation run writes exactly one document -- that directory's. Some are later
picked up when the parent is composed; nothing records which, so the corpus
cannot distinguish "hoisted at the parent" from "silently lost".

### G5 `[REF]` Generation loses 23% of admitted candidates, and mostly not for structural reasons

Measured across 46 of the 48 reports and all 45 documents: **310 candidates
measured, 235 written, 4 hoisted, 71 absent (23%)**. "Absent" means the fact is
expressed neither in the assessed directory's document nor at its stated
destination. (The corpus is 48 reports / 316 candidates; the two dot-prefixed
reports, `.claude.json` and `.claude-hooks.json` with 6 candidates between them,
were missed by the measurement's file glob and are unmeasured.)

**Adversarial review dissolved the four-mechanism account below into one
dominant defect (G12) plus a measurement artifact.** The loss is not
distributed across independent causes; it is very nearly a single one:

| Mechanism | Absent | Share |
|---|---:|---:|
| PROMOTE-scoped candidate, which no consumer exists for (G12) | ~57 | 80% |
| Measurement artifact: report post-dates its document (G5d, revised) | 14 | 20% |

The earlier decomposition into "plain omission / unwritable destination / stale
document / null branch" was an artifact of not checking the `scope` field. All
four buckets are dominated by the same thing: 78 of 316 candidates are
PROMOTE-scoped, and in ten reports the absent count EXACTLY equals the promote
count (`godot-scripts` 11/11, `kernel-src` 3/3, `src-effects` 3/3,
`pass2-src-debug` 3/3, `pass2-src-devtools` 3/3, `pass2-bots-lib` 5/5, and
four more). The null branches (G5b) are not a separate mechanism either -- all
11 of their candidates are PROMOTE.

**Two mirrored directory names caused a real analytical error, and the same
confusion may exist in the pipeline.** `kernel/src` (C++) and `src/kernel` (JS)
are different subjects whose reports are `kernel-src.json` and `src-kernel.json`
under the pipeline's path-flattening scheme. An earlier draft cited `kernel/src`
as proof of plain omission; it is not -- three of its four candidates target the
unwritable `kernel/CLAUDE.md` and the fourth, which targets its own directory,
was correctly written. `kernel/src` is evidence for G3, not for omission.

### G12 `[OBS]` Coverage emitted a retired placement vocabulary that generation could not consume

**This was the dominant defect, and this section is the PRE-DECISION diagnosis
that led to P0.** It is retained in its diagnostic form because the measurement
is what made the decision possible; the decision itself is recorded in P0, and
the disposition below is stated in the past tense because it has been answered.

78 of 316 candidates carry `scope: PROMOTE` with a destination above the
assessed directory. Under the model in force when the corpus was produced,
promotion is retired -- a fact reaches a wider scope only by HOISTING, which the
PARENT performs after noticing the fact in its children's finished DOCUMENTS.
Nothing is ever nominated from below.

That left a PROMOTE candidate with no path to any document:

- the child's own run is forbidden to write it (`fact-scoped-to-this-directory`);
- the parent's run composes from child DOCUMENTS, not child REPORTS, and the
  child was forbidden to write the fact, so the parent never saw it;
- the destination it names is advisory text no stage reads.

The correlation is close to exact in ten reports. It is NOT deterministic,
which is its own finding: `pass2-src-api` has 7 candidates, all 7 PROMOTE, and
loses none -- so some runs write a PROMOTE candidate locally, ignoring the
destination, and others drop it. The field was honoured inconsistently rather
than uniformly ignored.

This subsumes G3 (an unwritable `godot/` destination is one PROMOTE
destination among others), G4, and G5b (all 11 null-branch candidates are
PROMOTE). It was one decision, not four fixes.

**The decision it forced** was whether coverage stops emitting non-local
destinations, whether composition gains child REPORTS as a third input, or
whether the existing two-input composition is widened at the point where it
already concedes a gap. That question was settled on 2026-08-12; see P0.

### G5d `[REVISED]` A report produced after its document measures nothing

Originally claimed as "a stale document suppresses regeneration". The timeline
is real -- `src/kernel/CLAUDE.md` was committed 2026-08-10 by the pilot
(`e654891`, `c637e3b`) while every other document landed 2026-08-12, and its
report post-dates it -- but the conclusion was wrong.

`src-kernel.json:11-12` names the existing document among its inputs and lists
six facts "Suppressed as already ambient (src/kernel/CLAUDE.md)" -- exactly the
document's six sections. CV-2 suppression makes a post-document re-assessment's
candidates DISJOINT from that document by construction. So the 14 "absent"
facts are not a loss at all: they are the candidates CV-2 was designed to
surface, and no generation run ever consumed them.

Two consequences:

1. The 23% headline is not a usable baseline. Any report produced after its
   document scores ~100% absent by design. The measurement must exclude that
   class before a regeneration can be scored against it.
2. The real defect here is thinner than claimed but still present: nothing
   records which report composed which document, so this class cannot be
   identified without reading commit dates by hand.

### G5d-orig `[OBS]` An existing document suppresses regeneration, discarding the newer report

`src/kernel/CLAUDE.md` was committed 2026-08-10 by the pilot run (`e654891`,
corrected in `c637e3b`). Every other document in the corpus landed 2026-08-12
(`eb46f45` 34 documents, `ca3f6a3` 9, `b371a26` 1). Its coverage report
`src-kernel.json` post-dates the document -- the report's own
`sibling_overlap` field cites the document's existing "Zero-copy mesh views"
section.

So wave 0 skipped the directory because a document was already present, and all
14 of its admitted candidates were discarded without a record. The document is
not wrong; it is answering an older question.

This is the regeneration failure mode specifically: the chain has no notion of
a document being STALE with respect to a newer report. Re-running coverage on a
documented directory produces candidates nothing will ever consume.

Two secondary signals:

- Reports named `pass2-*` lose **55%** of their candidates (16 of 29) against
  **20%** for ordinary reports (55 of 281). Whatever the second-pass route was,
  it is materially worse, and nothing records what distinguished it.
- Loss is not explained by a destination pointing elsewhere. `godot/stations`
  has an unwritable destination and loses nothing; `pass2-src-api` routes all
  seven candidates elsewhere and loses nothing. Conversely `src/kernel` routes
  nothing elsewhere and loses everything.

There is currently no artifact that answers "was every admitted candidate
written, hoisted, or deliberately declined?" -- so all 71 losses are silent.

### G5b `[OBS]` The null branch discards admitted candidates

Three of the four recorded null branches had non-empty reports: `bots/lib` (5
candidates), `src/debug` (3), `src/devtools` (3). "No insight worth capturing at
this scope" was recorded over 11 facts a coverage pass had already admitted,
with no per-candidate reason. For `bots/lib` the task record asserts the
decline was correct under `fact-scoped-to-this-directory`; for the other two
nothing is recorded either way.

A null branch over a GAPS-FOUND report is a contradiction that should have to
be justified per candidate, not per directory.

### G5c RETRACTED -- two documents appeared to have no report

Claimed that `.claude/CLAUDE.md` and `.claude/hooks/CLAUDE.md` had no coverage
report. False: `.claude.json` and `.claude-hooks.json` exist and were missed by
a `glob('*.json')` that does not match dot-prefixed files. Retained here as a
retraction because the same glob bug produced the retracted G10 and the
310-vs-316 denominator above.

### G6 `[REF]` Emitted `Verify` commands are never executed

The reference makes Verify a first-class field -- "A concrete source, symbol,
command, or comparison that tests it" (`claude-md-generation-method.md:154`) --
and runs a separate post-emission source-verification phase (`:53`).

We emit Verify text and never run it. Consequences in the corpus:
`src/CLAUDE.md:54` states a grep lacking `--include=*.js`, so it counts
CLAUDE.md files as code (claimed 15, truth 13, now returns 16);
`test/fixtures/CLAUDE.md:17-18` states a check that contradicts its own
correct conclusion.

### G7 `[REF]` No verification pass over the emitted document

Distinct from G6: claims that were wrong at write time and that nothing
re-checks. `godot/tests/CLAUDE.md:120-121` asserts three files repeat a seed
"with no rationale"; all three carry a rationale comment. The repo root
retains at least eight stale claims, and it sits on every file's ancestor
chain.

### G8 `[REF]` No corpus pass, so duplication is contractual rather than accidental

Phase 6 "compares every parent/child CLAUDE.md pair across the generated
hierarchy, simulates moving or removing text, and checks the resulting
inherited context before retaining a change"
(`claude-md-generation-method.md:239-244`).

We have hoisting instead, and hoisting is strictly weaker by construction: one
run writes exactly one document, so a parent that hoists a fact cannot remove
the child copies. Six clusters survive, roughly 250-350 lines, and the
duplication has already produced a factual error (G6's 15-vs-13) one commit
after the hoist.

Note the reference does NOT demand subtraction: consolidation "can retain a
repeated rule after testing its inherited visibility"
(`md-domain-coverage-gaps.md:216-219`). It demands a tested decision per pair.

### G9 `[REF]` Authoring form rules are not applied

"No title header and no directory inventory"
(`claude-md-generation-method.md:299-302`). 45 of 45 documents open with an H1.
The root carries a stale 75-line directory tree that six children then write
corrective text against.

### G10 RETRACTED -- report count appeared not to match the enumeration

Claimed 46 reports for 48 in-scope directories. False: there are 48 reports.
The two dot-prefixed ones were missed by a `glob('*.json')`. No enumeration
mismatch exists. Retained as a retraction, and as the reason every count in
this document names its denominator.

### G11 `[OBS]` The cross-tree reach failure is a quality failure, not a model limit

The audit reported ~20 cross-tree obligations documented on the side that does
not need them, and it was tempting to blame the direct-code subject rule -- a
directory cannot document an obligation it has no way to discover.

Measured, that excuse mostly does not hold. Share of each receiving directory's
own files that name the other tree:

| Directory | Files naming the other tree |
|---|---|
| `kernel/src` | 10 / 10 (100%) |
| `kernel/include/woodkernel` | 11 / 11 (100%) |
| `kernel/tests` | 2 / 2 (100%) |
| `godot/stations` | 5 / 8 (62%) |
| `src/kernel` | 6 / 10 (60%) |
| `godot/scripts` | 34 / 61 (55%) |
| `godot/tests` | 7 / 57 (12%) |
| `src/ui` | 2 / 28 (7%) |

Every `kernel/src` file opens with `// Port of src/kernel/<name>.js ...
PORTING.md rules apply`. The signal was in the subject the run was handed. The
genuinely-undiscoverable class exists but is small, and by the owner's ruling it
is out of scope; what remains is a reading failure.

G11 is load-bearing for P0's measurement: four of the twelve `godot/` facts were
found by only one report while their evidence sits in several children's own
files, which is a recall failure at the sibling runs rather than a limit of the
composition model.

### Deliberately NOT treated as deficiencies

- A directory with no local signal cannot discover an obligation owed to
  another tree. Owner's ruling: not a problem this workflow needs to solve.
  Per G11 this class is smaller than the audit implied.
- Document size against the reference's 30-50/10-30 budgets. The reference
  states these are budgets, not limits, and its own corpus missed both ends.
  Seven of eight parents were judged to earn their length.
- Defect enumeration. md-domain declines it by contract (CV-8), correctly.

## Part 2 -- remediation plan

**The ordering premise, stated honestly, after two corrections.** The first
draft asserted that measurability (P1-P3) must come first because no later fix
could otherwise be shown to have worked -- which was a claim about
verifiability doing duty as a claim about priority. The G5 decomposition, twice
revised, now supports a sharper reading:

| Loss mechanism | Absent | Addressed by |
|---|---:|---|
| PROMOTE candidate with no consumer (G12) | ~57 (80%) | **P0** -- one model decision |
| Report post-dates its document (G5d) | 14 (20%) | not a loss; excluded from the baseline |

**This killed the plan's original shape.** Three drafts distributed the loss
across four mechanisms and proposed a fix per mechanism. There is essentially
one mechanism, it is a MODEL question rather than an implementation defect, and
until it was decided the rest of the plan was building instrumentation around an
unresolved contradiction.

The path to that was three consecutive wrong attributions -- `kernel/src` (a
mirrored directory name), a glob that dropped two reports, and a suppression
rule mistaken for omission. Each was caught by measurement or by review, none
by inspection of the plan.

Revised ordering: **P0 first and alone.** P1 and P3 are worth building
regardless as instrumentation, but they are no longer claimed as remedies.
P4-P8 should not be designed until P0 is settled, because P0 changes what a
destination MEANS and therefore what every later item validates against.

**P0 was settled on 2026-08-12 (owner decision): Option C.** The sections
below are the plan as it stands after that decision. Each item states whether
the decision changed it; an item marked reviewed-and-unchanged was re-read
against Option C and found unaffected, which is a different claim from not
having been revisited.

### P0. SETTLED 2026-08-12 -- Option C (fixes G12, subsuming G3/G4/G5b)

**Status: decided. Authority: owner decision, 2026-08-12.** This item is a
decision record. It is no longer an open question and no longer carries a
recommendation.

Coverage emitted upward destinations; the composition model had retired upward
nomination. One of the two had to move, and the choice had to apply to EVERY
directory rather than to a favoured class.

#### What was decided

**Option C -- keep the composition model exactly as it is, and drop hoisting's
repetition trigger. TAKEN.** Concretely:

1. A parent's composition still has exactly TWO inputs: its own direct code
   files, and its children's finished CLAUDE.md documents. There is no third
   input, no routing field, no new disposition, and no child-report edge.
2. A fact still reaches a wider scope only by HOISTING, performed at the parent
   over documents the parent has actually read. Nomination from below still
   NEVER happens.
3. **The single change:** the trigger "the fact appears in more than one child"
   is dropped. A parent MAY hoist a fact it finds in a SINGLE child's finished
   document, provided the WORDING test still passes -- the fact must be worded
   so it is true as stated at the parent's depth, of everything below it.
4. `fact-scoped-to-this-directory` is NOT reversed. Coverage keeps emitting
   local destinations only, and its retired carriage fields stay retired.

#### Why this is a small change rather than a new model

The shipped model already concedes the exact gap Option C closes, in the same
paragraph that states the trigger:
`plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md:135-143`
says the repetition and wording tests "come apart in both directions", and names
the second direction outright -- "a fact true of every child that only one child
noticed never triggers at all" (`:138`). The same concession is stated a second
time at `plugins/skills-kit/skills/md-domain/CLAUDE.md:364-374`.

So the model already holds that WORDING, not repetition, is what licenses a
hoist. Repetition was doing duty as a cheap heuristic for wording, and the
shipped text already records that it is an unsound one. Option C removes the
heuristic and keeps the test.

#### The measurement that decided it

The 13 candidates routing to `godot/` (G3) were the case Option B was proposed
to rescue. Measured against the corpus reports in
`dev/tasks/md-domain-review-enablement/reports-json/`, those 13 candidates are
**12 distinct facts**, and they distribute like this:

| Class | Facts | What Option C does with them |
|---|---:|---|
| Already discovered independently by 2+ sibling reports | 5 | Ordinary repetition-hoisting already fires; no new machinery needed |
| Partially repeated (stated by one report, echoed in part by another) | 2 | Hoistable under either trigger once the parent reads both documents |
| Found by one report, evidence present in several children's own files | 4 | A recall failure at the sibling runs (G11), not a model limit |
| Genuinely single-child | 1 | Reachable only because Option C drops the repetition trigger |

The five independently-repeated facts are the autoload rule, the units-CM rule,
the audio `stop_owner` rule, the headless `user://` gate, and the
no-aggregate-runner rule. Corroboration at file granularity in the reports
directory: `autoload` appears in `godot-assets-audio.json`, `godot-scripts.json`,
`godot-stations.json` and `godot-tests.json`; `stop_owner` in
`godot-scripts.json` and `godot-stations.json`; `user://` in
`godot-scripts.json` and `godot-tests.json`. The 2 partial repeats are the
oracle-divergence fact and the WoodKernel-is-Windows-only fact. The single
genuinely-single-child fact is `godot-tests`' private-surface fact.

**The conclusion that follows.** Under Option C all 12 facts are STRUCTURALLY
reachable, because every one of them is written at the child that assessed it
and the parent reads those documents. What Option B offered over Option C on
this evidence is not reach -- it is a shortcut past a recall failure that G11
already classifies as a quality problem.

**The residue Option C cannot reach**, stated so it is not discovered later as a
surprise: a fact that fails the CHILD's own local value bar and therefore never
enters any document at all. Nothing the parent reads can contain it. On this
sample that is plausibly one fact in twelve. Option C accepts that residue.

#### Options rejected, retained so a later reader can see the trade

**Option A -- coverage stops emitting non-local destinations, model otherwise
untouched. NOT TAKEN, and it is the closest neighbour of what was taken.**
Every candidate targets the directory it was assessed from; a wider fact reaches
its scope only by repetition-triggered hoisting. It was rejected on one cost: a
fact appearing in only ONE child never repeats, so it never triggers a hoist --
it is either written narrowly at the child, where it over-reaches, or lost.
Option C is Option A with exactly that cost removed, which is why Option A is
not a live alternative but also not far from the decision.

**Option B -- composition gains children's ROUTED CANDIDATES as a third input.
NOT TAKEN.** A parent would compose from its own code, its children's documents,
AND its children's coverage candidates whose `destination` names an ancestor.
Upward routing would become real rather than advisory, and the parent would
weigh a nomination it could decline.

Option B was provisionally settled on 2026-08-12 and threaded through this
document in commit `928b907`. Two independent adversarial reviews then returned
NOT-READY on it. The reasons it was set aside:

- **It is a deliberate partial reversal of a fail-severity criterion.**
  `plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md:94-110`
  states that a candidate's destination "is that directory, always". The reason
  the criterion exists (`:104-107`: an assessment that read only this directory
  "cannot see whether the fact holds of code it never opened") remains true
  under Option B; only the consequence was to be downgraded. That is a real
  model reversal, and it obliged amendments across three shipped documents plus
  a mechanical unblocking of `workflow/coverage-detect.js`, whose schema sets
  `additionalProperties: false` (`:98`) and pins `destination` to the assessed
  directory (`:109-113`).
- **The measurement above shows it was not needed for the case it was chosen
  for.** 11 of the 12 `godot/` facts are reachable without it, and the twelfth
  is reachable under Option C.
- **It opened three questions the plan could not close**: how far up a candidate
  may route, what happens to a candidate the parent declines, and whether a
  candidate routed above a run's root is permanently undispositioned. All three
  disappear with Option B.

Nothing in Option C revives `sibling_overlap` or the `PROMOTE` vocabulary, and
nothing in it licenses a child to write outside itself.

#### The scope of the change -- it is not doc-only

The repetition trigger is implemented in the WORKFLOW PROMPT, not only in
prose. `plugins/skills-kit/skills/md-domain/workflow/claude-md-generate.js:322-324`
instructs the composing agent that "A fact appearing in more than one child
moves up to this directory", and `:325-331` states "REPETITION TRIGGERS A HOIST;
WORDING LICENSES IT" with the two-tests framing. An agent dispatched by that
prompt applies the repetition trigger whatever the references say.

So Option C is a documentation change PLUS a workflow prompt change. **The
references and the prompt are inputs to the same dispatched run, so they must
ship in ONE skills-kit version bump.** Shipping the references first would put a
composing agent under a prompt that contradicts the reference it is handed;
shipping the prompt first would put it under a reference that contradicts the
prompt. Either ordering produces a run whose behaviour cannot be attributed.

Two mechanical questions were checked rather than assumed:

- **The `hoists` schema field needs no structural change.**
  `claude-md-generate.js:191-203` declares `hoists` with
  `required: ['fact', 'fromChildren', 'wording']`, and `fromChildren` (`:199`)
  is an array of strings, which already admits a single element. What needs
  amending is WORDING, in two places: the prompt's instruction to report "which
  children stated it" (`:332-334`), which reads as presupposing more than one,
  and the schema comment at `:189-190`.
- **No test pins the repetition trigger.**
  `tests/skills-kit/test_coverage_workflow_contract.py` pins the COVERAGE side
  only -- the two verdicts, the criteria seam, and the `tier`/`anchors`/
  `destination` candidate fields (`:279-306`). Option C changes none of those.
  `tests/skills-kit/test_workflow_js_drift.py` covers the four generated
  REMEDIATE lanes and the detect/classify skeletons, not `claude-md-generate.js`.
  A regression test for the amended trigger therefore does not exist and would
  have to be written; that is a gap to fill, not a blocker discovered late.

### P1. REVISED -- one candidate schema with a stable identity, enforced at emit (fixes G1, G2, partly G5)

**Option C removes the routing field this item carried under Option B, and adds
the identity requirement two reviews found missing.** `destination` goes back to
being pinned to the assessed directory, so the schema does not have to express
what a legal non-local destination is. What it does have to express, and never
did, is WHICH CANDIDATE a downstream record is talking about.

Define a single candidate record and validate it before a report is written:

| Field | Type | Rule |
|---|---|---|
| `id` | string | required; stable within the report, `<report stem>#<index>` |
| `fact` | string | required |
| `destination` | string | required; repo-relative directory, no `CLAUDE.md` suffix, no annotation, no absolute path; the assessed directory, always (CV-3, unchanged) |
| `convertibility` | enum | `finding-convertible` \| `context-only`; required (CV-4) |
| `rationale` | string | required |
| `evidence` | list | required, non-empty; each entry `path:line` inside the assessed directory (CV-7) |
| `severe_deficiency` | bool | optional, default false |

**Stable identity is the addition, and the prior art already exists in the
repo.** `plugins/skills-kit/skills/md-domain/scripts/discover_hierarchy.py:176-183`
states the requirement in its own docstring -- candidates get "a stable `_id`
(`<file stem>#<index>`), which is what the lane's input-accounting check counts
against -- without a stable identity a dropped candidate is indistinguishable
from a merged one" -- and `:217-226` assigns it. That is exactly the property
P3 and the Validation section need and neither previously specified. Adopt the
same scheme rather than inventing a second one, and make it a report field
rather than a loader-applied annotation, so the identity survives being written
to disk.

**Why identity cannot be text matching, under Option C specifically.** A hoist
REWORDS the fact -- that is the wording test, stated at
`generation-lane.md:139-141` and enforced in the prompt at
`claude-md-generate.js:327-330`. A parent may also merge several child
statements into one (`generation-lane.md:174`). So the reconciliation P3
promises -- child claim to parent hoisted claim -- cannot be done by comparing
strings, and no amount of care in the prose makes it mechanical. The parent's
`hoists` entry must name the child claim by ID.

Retire `tier`/`why`/`anchors` as aliases with a one-version compatibility read.
A report that fails validation does not get written -- a malformed candidate is
worse than a missing one because it looks consumable.

**Reconciliation owed against the shipped schema.** `tier`, `why` and `anchors`
are not merely one of the corpus's four observed shapes; they are the REQUIRED
field names in the shipped workflow schema
(`plugins/skills-kit/skills/md-domain/workflow/coverage-detect.js:104`), which
also sets `additionalProperties: false` (`:98`), and they are pinned by
`tests/skills-kit/test_coverage_workflow_contract.py:292-306`. So this item is a
change to a published contract with a test to update, not a tidy-up of corpus
drift. It is independent of P0 and can ship on its own schedule.

### P2. SETTLED -- a code-free directory with children IS a composition subject (fixes G3)

**This item is settled here rather than listed as open, because under Option C
it is load-bearing.** `godot/` holds no direct code files -- only
`project.godot`, a `.tres` and an `.svg` -- so it is not a coverage subject and
no assessment runs there. If no GENERATION run happens at a code-free directory
that has children, the 12 facts of P0's measurement have nowhere to hoist TO,
and Option C delivers nothing at all. Both adversarial reviews identified this
independently, and both noted it is independent of the A/B/C choice.

**The answer: a directory is a COMPOSITION subject when it has at least one
in-scope child CLAUDE.md, whether or not it has direct code files. The COVERAGE
subject rule is untouched -- such a directory is still never assessed, because
there is nothing in it to assess.**

Its generation run is the degenerate case of the existing two inputs: the first
input (own direct code) is empty, the second (children's finished documents) is
not. It is not a new mode.

**The shipped workflow already tolerates this shape, which is what makes the
cost small.** `claude-md-generate.js:120-131` refuses only when EVERY subject
lacks a report and inline candidates; the partial case is explicitly allowed and
merely logged (`:132-138`). A code-free parent is exactly a subject with no
report among subjects that have them.

**The costs, stated rather than implied:**

1. **An enumerator change, caller-side.** The subject set is built from coverage
   discovery, which never produces a code-free directory. Something must add
   directories that hold no code but have at least one in-scope child. Until
   that exists, P2 is a rule with no producer, and Option C's reach at `godot/`
   and `kernel/` is zero.
2. **A false log line.** `claude-md-generate.js:135-137` announces that
   inputless subjects "will be written from code alone". For a composition-only
   subject that is exactly backwards -- it is written from child documents
   alone. The message must distinguish the two, or the one diagnostic that
   surfaces this case actively misdescribes it.
3. **Documents that may be empty, and that is acceptable.** A code-free parent
   whose children share nothing hoistable produces nothing. The null branch is
   already a legal, recorded outcome (`written: false`,
   `claude-md-generate.js:480-483`), so the cost is one wasted dispatch per such
   directory, not a bad document.
4. **A subject with no own report.** P3's accounting and P8's provenance must
   both accept a subject whose own-report entry is absent; see those items.

**The alternative considered and rejected**: letting a child's run write its
parent's document when the parent is code-free. Barred by the one-document-per-
run contract (`generation-lane.md:86-89`) and by the prohibition on a child
writing outside itself, which Option C explicitly does not touch.

### P3. REVISED -- per-candidate accounting as the generation run's OUTPUT CONTRACT (fixes G4, G5, G5b, G10)

**Option C simplifies this item back to one-sided accounting.** Option B's
`routed` disposition and its matching obligation at the destination are
REVERSED: there is no inter-run handoff to account for, because a candidate
never leaves the run that received it.

Today a generation run receives N candidates and returns one document. Nothing
in its contract obliges it to say what became of each candidate, so silently
writing 0 of 14 (`src/kernel`) is a conforming run. The fix is to change what a
run RETURNS, not to add a check after it.

A run must emit, alongside the document, one terminal disposition per admitted
candidate, keyed by the candidate `id` P1 defines:

- `written` -- expressed in this document (cite the section)
- `declined` -- with a reason from a closed set (already ambient; not evidenced
  in this directory; superseded by a broader candidate; below the local value
  bar)
- `deferred` -- the run could not complete the judgment, with the reason

Every disposition is terminal at this run. `declined` is a legitimate and
expected outcome -- the requirement is that it be stated, not that it be rare.
A run that returns a document without a full disposition set is INCOMPLETE and
its output is not accepted.

**The parent side, which Option C makes an addition rather than a handoff.** A
composition already reports `hoists` (`claude-md-generate.js:191-203`). Under
Option C that record gains two obligations:

- **Name the child claim by ID.** `fromChildren` today names directories
  (`:199`). It must name the child's claim identity, because the hoisted wording
  is by construction not the child's wording (P1).
- **Record the NOT-hoisted decision too.** A parent that considered a child's
  fact and left it in the child must say so, with the reason -- almost always
  "no wording is true at this depth short of a list of exceptions", which is the
  escape clause the model already states (`generation-lane.md:141-143`). Without
  this, Option C's failure mode is invisible: a composition that hoists nothing
  and reports no absence looks identical to a composition with nothing to hoist.
  This is the Option C analogue of the flaw the reviews found in Option B's
  scoring, and it is why the Validation section below measures two independent
  quantities.

Consequences for G5b: a null branch becomes expressible only as a document with
every candidate `declined`, each with a reason. "No insight worth capturing at
this scope" stops being a directory-level assertion that silently swallows 11
admitted facts.

Consequence for P2: a composition-only subject has no candidates of its own, so
its disposition set is legitimately empty and its `hoists` set carries the whole
of its output contract. An empty disposition set must therefore be distinguished
from a missing one.

Run-level reconciliation (every enumerated directory has a report or is a
composition-only subject; every report's every candidate has a terminal
disposition; every hoist names a child claim that exists) then becomes a cheap
mechanical check, and the scoring key for the regeneration test below.

### P4. REVISED -- execute Verify AFTER the write, in ONE coordinate system (fixes G6)

Where a Verify is a shell command, run it and compare its output to the claim.

Both reviewers rejected the emit-time formulation, correctly. The corpus's one
measured Verify failure (`src/CLAUDE.md:54`, 15 vs 13) **rotted after the
write**: the grep lacks `--include=*.js`, so it counts CLAUDE.md files, and
generation itself keeps adding those. A check that passed at emit was wrong one
commit later, and it now returns 16.

So: run assertions post-write, over the corpus as it will actually be read, and
either exclude generated documentation from any command's search space or ban
corpus-self-referential commands outright. The command needs a defined cwd,
expected predicate, and read-only classification -- none of which P1's schema
currently specifies.

**One coordinate system, and it is the repository root.** The prior draft of
this item set the cwd to the directory of the document carrying the claim while
also requiring every path in the command to be repository-relative. Those are
incompatible: a repository-relative path does not resolve from a nested
directory, so every emitted command would either fail or silently match nothing.
Pick one and state it once:

- **cwd is the repository root, for every Verify, in every document.**
- **Every path in a Verify command is repository-relative.**

The alternative (cwd at the carrying document, paths relative to it) was
considered and rejected: it makes an otherwise identical claim carry different
command text depending on which document it ends up in, so a hoist would have to
rewrite the command as well as the wording -- adding a second, silent way for a
hoist to be wrong.

Under Option C this is a plain specification gap rather than something the
decision opened. A hoisted claim moves from a child to a parent, so a
document-relative command would have to be rewritten on every hoist; a
root-relative one does not change at all.

### P5. UNCHANGED-REVIEWED -- a verification pass over the emitted document (fixes G7)

**Option C does not change this item.** Its subject is a written document and
the code that document cites. A hoisted claim is re-checked by the parent's pass
exactly like any other claim the parent carries, and whether the hoist was
triggered by one child or several does not alter what re-checking means.

Post-emit, pre-commit: re-check every claim in the written document against the
code it cites, with fresh context and the document as the subject. The
reference places this between emission and consolidation as its own phase with
its own manifest.

One interaction worth naming: under Option C a single-child hoist has exactly
one child document behind it, so the parent's claim rests on a narrower evidence
base than a repetition-triggered hoist did. That raises the value of this pass
rather than changing its design -- the wording test is a judgment, and this is
the only stage that re-tests it against code.

### P6. REVISED -- a corpus pass, as a READ-ONLY planner plus ordinary per-directory runs (fixes G8)

Both reviewers independently proposed the same reconciliation with the
one-document-per-run contract, which the earlier formulation could not satisfy:

1. **Plan (read-only).** After all documents exist, walk every parent/child
   pair and emit a decision ledger -- remove from child, move to parent, retain
   at both, relocate to a narrower child -- with a reason per pair. Reads span
   the corpus; nothing is written.
2. **Apply.** Ordinary per-directory generation runs take that ledger as an
   input. Each run still writes exactly one document.

Reads are corpus-wide, writes stay local, and no fact is placed anywhere its
own directory's run does not write it -- so this is not top-down placement
returning by the back door.

Ordering constraint the reviewers flagged: add-at-parent must precede
remove-from-child, or the child's evidence for the hoist disappears before the
parent is composed and the parent is immediately stale. Retention is a
legitimate outcome; an unrecorded duplicate is not.

**Option C's effect on this item is the opposite of Option B's, and it is worth
stating because the direction reversed.** Option B was expected to SHRINK P6 by
routing facts to the parent before any child copy existed. Option C creates no
such bypass: every fact is still written at its assessing child first, and a
hoist still leaves the child copy behind (`generation-lane.md:145-151`). Option
C also makes hoists MORE frequent by construction, since a single child is now
enough to trigger one. So P6's expected volume goes UP, not down, and the
add-at-parent-before-remove-from-child ordering constraint applies to more
pairs.

That does not make P6 more urgent -- it is still a consolidation pass over a
finished corpus -- but it removes the argument, made under Option B, that the
decision might shrink P6 enough to question whether it is worth building.

### P7. UNCHANGED-REVIEWED -- enforce the authoring form (fixes G9)

**Option C does not change this item.** No H1 title, no directory inventory, at
emit. These are properties of a document's surface form and are indifferent to
where its content came from -- including at a P2 composition-only directory,
whose document is subject to the same form rules as any other.

### P8. REVISED -- provenance that records negative dependencies (fixes G5d)

The chain has no concept of a document being out of date with respect to a
newer assessment, so a documented directory is silently skipped and its report
discarded. A generation run must compare the document's provenance against the
inputs that would compose it, and treat `any input newer than the document` as
REGENERATE rather than SKIP.

This is the item that makes the workflow a REgeneration workflow rather than a
one-shot. It also determines what a second run over an already-documented
corpus is worth, which is the question the validation below actually asks.

**Under Option C the provenance edges are the two composition inputs, and
nothing else.** Option B's third edge -- a child REPORT from which a routed
candidate was consumed -- is REVERSED, because no run consumes another
directory's report. A document's provenance records:

- **its own coverage report**, by identity and content digest -- **nullable**,
  because a P2 composition-only subject has no report and a null entry there
  must be a recorded fact rather than a missing field;
- **every eligible child DOCUMENT**, by path and content digest.

**Digest every eligible child document, including the ones that contributed
nothing.** This is the correction two reviews converged on, and it is the whole
substance of the change. Recording only the inputs a hoist was drawn FROM
detects a changed or removed input; it cannot detect a child document that
previously carried nothing relevant and LATER gains something hoistable. Under
Option C that case is not an edge case at all -- it is the ordinary one, because
a single child is now enough to license a hoist, so any child gaining any fact
can change what the parent should say. A provenance record listing only
contributing children reports a parent as current precisely when the new content
arrived somewhere it was not looking.

So the rule is: the eligible set is every in-scope child document at composition
time, contributing or not, and the parent is stale when any digest in that set
changes, when a child document appears or disappears, or when its own report
changes.

The bottom-up dependency already stated in
`plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md:153-166`
is unchanged in direction and needs no amendment for Option C. Its statement at
`:161-166` that "a stale child document silently corrupts its parent" is exactly
the property this item makes detectable, and Option C strengthens the mechanism
it names -- a stale child now "suppresses hoists whose repetition it no longer
shows" for hoists that need no repetition at all.

### Precondition: the publication gate

**Nothing in this plan may be validated by regenerating the corpus until the
amended skills-kit version is PUBLISHED and the running machine's plugin cache
carries it.** Both adversarial reviews named a variant of this as the highest
risk in the plan, and neither the pre-decision draft nor the Option B draft
contained it.

The failure it prevents: skills-kit is a published plugin, and a session loads
its skill and workflow files from the plugin cache, not from this working copy.
The repository's own rule is that the cache keys on version (`CLAUDE.md:473`),
so an edited-but-unpublished `claude-md-generate.js` is structurally invisible to
a normal session. A corpus-scale regeneration run started before publication
would dispatch every composition under the OLD prompt -- the one that says "A
fact appearing in more than one child moves up to this directory"
(`claude-md-generate.js:322-324`) -- while the plan, the ledger and the scoring
all describe Option C. The run would reproduce the original defect under the new
terminology, and because the amended documentation would be sitting in the tree,
a reviewer reading the plan alongside the results could read the outcome as
partial success.

The gate, in order:

1. The reference amendments and the prompt amendment are committed together on
   `dev` with a skills-kit version bump (P0, "the scope of the change").
2. skills-kit is published through `scripts/publish.py` (`CLAUDE.md:220-222`).
3. The machine that will run the regeneration confirms the installed skills-kit
   version matches the release, by reading the version its session actually
   loaded -- not by reading the working copy.
4. Only then is the regeneration started.

No step here is optional, and step 3 is the one that is easy to skip: the
working copy and the cache disagree silently, which is the condition this gate
exists to catch.

### Validation: REVISED -- regenerate and score TWO independent quantities

The point of P1-P3 is that the fix can be tested rather than asserted.
Regenerate woodworking-sim, after the publication gate above, and score.

**One score is not enough, and this is the correction both reviews forced.**
Under Option B the flaw was that a run which routed every candidate upward and
then declined every one of them at the parent scored as zero unaccounted
candidates and passed. The Option C analogue is exact: a corpus in which every
composition hoists NOTHING, and reports no absence because it was never obliged
to report a not-hoisted decision, also scores as fully accounted. Accounting
completeness and content outcome are different questions and must be reported as
two numbers that cannot substitute for each other.

**Quantity 1 -- accounting completeness.** Mechanical, and the target is exact.

- Every enumerated directory has a report, or is a P2 composition-only subject
  with a recorded null own-report.
- Every admitted candidate, by `id` (P1), has exactly one terminal disposition
  (P3).
- Every `hoists` entry names a child claim ID that exists in that child's
  disposition set.
- Every composition records a not-hoisted decision with a reason for each child
  claim it considered and left in place.
- **Target: zero unaccounted candidates.** Anything else is a failed run,
  independent of content.

**Quantity 2 -- content outcome.** Judgment, and the target is a reviewed
distribution rather than a threshold. Report, as rates over the accounted set:

- `written` at the assessing child;
- `declined` with a reason, broken down by reason;
- considered-but-not-hoisted at the parent, with a reason;
- `hoisted`, split by single-child and multi-child trigger -- the split is the
  direct measure of what Option C bought.

Then compare against a reviewed expected outcome for the known high-value cases,
fixed BEFORE the run so it cannot be fitted afterwards:

- the 5 independently-repeated `godot/` facts hoist to `godot/` (these would
  have hoisted under repetition alone, so a failure here indicts the run, not
  the decision);
- the 2 partial repeats hoist to `godot/` or carry a stated not-hoisted reason;
- the 4 recall-failure facts appear in the assessing child at minimum, and the
  sibling runs that missed them are checked against G11's file-share table;
- the 1 genuinely-single-child fact hoists to `godot/` or carries a stated
  not-hoisted reason -- this is the single case that exists only because Option
  C dropped the repetition trigger, and a silent absence here means the prompt
  amendment did not take;
- `kernel/`'s 6 candidates are dispositioned at their assessing children and
  either hoisted to `kernel/` or refused with a reason.

**Two scoring caveats, both from decisions already taken:**

- The 23% headline is not a valid baseline (G5d). Any report produced after its
  document scores near 100% absent by construction, and 14 of the 71 absences
  are of that class. Exclude them before scoring, per open question 3 below.
- A hoisted fact is scored at the document that CARRIES it, not at the
  directory that assessed it. Scoring at the origin would count a correct hoist
  as a loss and reproduce the exact confusion G4 recorded, where the corpus
  could not distinguish "hoisted at the parent" from "silently lost".

Additional checks:

- `godot/CLAUDE.md` and `kernel/CLAUDE.md` EXIST -- this is the P2 check, and it
  is the precondition for every `godot/` and `kernel/` expectation above.
  If it fails, none of the hoisting results mean anything;
- `godot/extensions/woodkernel/` is decided explicitly: it has one contributing
  child, so under Option C it is a composition subject like any other and its
  document either exists with content or records a null branch with a reason;
- no candidate's `destination` is anything but the assessed directory (CV-3 is
  unchanged by Option C, so a non-local destination in a post-amendment report
  is a regression);
- no report emits `scope`, `sibling_overlap`, or the `PROMOTE` vocabulary;
- `src/kernel` writes or explicitly declines all 14 of its candidates (today:
  0 written, 0 declined, 14 silently absent), subject to the G5d exclusion
  above, which is exactly this directory;
- the `pass2-*` gap closes: no subset of the run loses 55% while the rest
  loses 20%;
- no emitted Verify command disagrees with its claim, and every one runs from
  the repository root with repository-relative paths (P4);
- no fact appears at both a parent and a child without a recorded retain.

The 71 absent facts are themselves the answer key: they are enumerated per
directory in the measurement outputs, so a regeneration can be scored against
a known list rather than re-audited from scratch. The key needs one correction
before use -- the 14 G5d absences are not losses and must be removed from it --
and one relabelling: the 78 PROMOTE-scoped candidates keep their facts but lose
their destinations, and their expected outcome becomes "written at the assessing
child, and hoisted or refused-with-a-reason at the ancestor".

The pre-fix corpus is the control and is preserved in woodworking-sim git
history. Ad-hoc hand edits to the corpus weaken specific checks and are
recorded in `regeneration-ledger.md`.

## Shipped-document and workflow amendments Option C implies

**None of these amendments is made here.** skills-kit is a published plugin;
amending it is a separate, separately-authorized step, with a version bump and
the ordinary publish flow. This section is the change list to approve or reject,
nothing more. See the publication gate above for why the whole list must ship in
one version.

**How this inventory was built, and why the method changed.** The Option B
change list was assembled by grepping the retired placement vocabulary (`scope`,
`sibling_overlap`, `PROMOTE`, "nomination"). That method missed every passage
that states the model WITHOUT those words -- it omitted
`workflow/claude-md-generate.js` entirely, which is where the rule is actually
enforced on a running agent, plus `SKILL.md`, several `coverage-lane.md`
passages, and the keyword list in `md-domain/CLAUDE.md`. This inventory was
built instead by READING every surface that describes parent composition or
hoisting: `SKILL.md`, `CLAUDE.md`, the four files under `references/lanes/`, the
three under `references/standards/`, and `workflow/claude-md-generate.js`, with
grep used only to locate line numbers within files already identified by
reading. The result is smaller than Option B's list -- 10 passages across 6
files, against roughly 13 across 3 plus 4 unread surfaces -- because Option C
changes one rule rather than reversing a criterion.

Line numbers are as of 2026-08-12.

### plugins/skills-kit/skills/md-domain/workflow/claude-md-generate.js -- the enforcing surface

This is the file that must change FIRST in the reviewer's attention, whatever
the commit order, because it is the only one a dispatched agent reads.

1. `:322-324`: "HOISTING is where de-duplication happens, and it happens HERE
   because this is the only place the documents being compared have actually
   been read. A fact appearing in more than one child moves up to this
   directory."
   The final sentence must become the single-child form: a fact appearing in ANY
   child's document is a hoist candidate, and the wording test decides it. The
   first sentence is unchanged and must be kept -- it states why the parent is
   the right place, which Option C does not touch.

2. `:325-331`: "REPETITION TRIGGERS A HOIST; WORDING LICENSES IT. These are two
   tests and they come apart in both directions. A fact stated by 2 of 20
   children, hoisted verbatim, becomes ambient for 18 directories it does not
   govern. ..."
   The headline must become the one-test form -- WORDING licenses a hoist, and
   there is no separate trigger. The 2-of-20 example must be KEPT: it is the
   direction of the failure Option C does not fix, and dropping it would read as
   a licence to hoist anything found anywhere. The escape clause at `:330-331`
   ("When no such wording exists short of a list of exceptions, the fact DOES NOT
   HOIST") is unchanged and becomes the whole of the test.

3. `:332-334`: "Report every hoist you make in the hoists field, naming which
   children stated it and the exact wording you used."
   Must accept a single child without reading as an anomaly, and must gain P3's
   second obligation: report the facts you CONSIDERED and did not hoist, with
   the reason. Without that addition, a composition that hoists nothing is
   indistinguishable from one with nothing to hoist, which is the failure the
   Validation section's second quantity exists to catch.

4. `:189-190`, the schema comment above `hoists`: "Set only by a composition. A
   hoist must be worded so it is true as stated at the parent depth, and it
   obliges the child copies to be removed."
   True as written and worth keeping. It should gain one sentence stating that
   `fromChildren` may name a single child, so a later reader does not restore the
   repetition trigger from the field's plural name. The FIELD (`:199`) needs no
   structural change: it is already an array of strings.

5. `:135-137`, the inputless NOTE: "N of M subject(s) have no coverage input and
   will be written from code alone".
   False for a P2 composition-only subject, which is written from child
   documents alone. Must distinguish a subject with code but no report from a
   subject with no code and children. This is a P2 amendment rather than a
   hoisting one, but it ships in the same version.

### plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md

1. `:124-125`: "A fact appearing in more than one child's document moves to their
   common ancestor."
   Must become the single-child form. The rest of that paragraph (`:125-133`),
   including "It is never nominated from below" and the `shallowest-true-depth`
   reference, is unchanged and must be kept -- Option C does not touch nomination
   or depth.

2. `:135-143`, "Repetition triggers a hoist; WORDING licenses it."
   This paragraph is the decision's own evidence and must be rewritten rather
   than deleted. Its concession at `:138` -- "a fact true of every child that
   only one child noticed never triggers at all" -- is what Option C acts on, and
   the amended paragraph should say so explicitly: the repetition trigger was
   dropped on 2026-08-12 because this document already recorded that wording, not
   repetition, is the licensing test. Keep the 2-of-20 warning at `:136-137`
   verbatim; it is the failure direction that survives.

### plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md

1. `:107-110`, the example under `fact-scoped-to-this-directory`: "A fact that
   genuinely governs a wider area reaches that area by HOISTING, which happens at
   the parent when the parent observes the same fact in more than one child's
   document -- never by nomination from below."
   Only the middle clause changes: the parent observes the fact in a child's
   document. "Never by nomination from below" is unchanged and is the clause that
   must survive intact, because Option C explicitly does not reverse this
   criterion. The statement at `:96-98` and the rule id are UNCHANGED.

2. `:101`, the criterion's keywords: `[scope, this directory only, destination is
   the subject, no nomination, no promotion, no hoisting from below, sibling
   subtree]`.
   Every one of these stays correct under Option C -- no keyword contradicts the
   amended criterion, because the criterion itself is unamended. Named here so
   the reader knows it was checked rather than skipped: this plan argues
   elsewhere that a keyword list contradicting its criterion routes readers to
   the wrong answer, and the same check applied here returns no change.

3. `:195-197`, in "Two RETIRED carriage fields on a candidate": "A fact reaches a
   wider area by HOISTING instead -- the parent observes the same fact in more
   than one child's document and lifts it, rewording it so it is true as stated
   at its new depth."
   Same single-child amendment as item 1. The retired-fields ban at `:185-187`
   and `:201-203` is UNCHANGED and must be kept in full: Option C revives
   neither field and does not license a non-local destination.

### plugins/skills-kit/skills/md-domain/CLAUDE.md

1. `:333`, the summary of `the_subject_is_one_directory_not_a_subtree`: "A parent
   gets its content by reading its children's finished CLAUDE.md files and
   hoisting what repeats -- placement is never nominated from below."
   "hoisting what repeats" becomes "hoisting what a wording test licenses at its
   depth". The supersession clause is UNCHANGED.

2. `:359-360`, under "WHY PLACEMENT MOVED TO THE PARENT": "A fact repeated across
   children moves up; a fact only one child has stays there."
   The second half is exactly what Option C reverses and must be amended. The
   surrounding reasoning at `:355-359` is unchanged and must be kept verbatim --
   it is the justification for hoisting happening at the parent, which Option C
   relies on rather than disputes.

3. `:364-374`, "REPETITION IS THE TRIGGER, WORDING IS THE TEST."
   The heading and the framing must change to wording-is-the-only-test, keeping
   the 2-of-20 example and the mirrored-directory escape clause at `:371-374`,
   which is the observed case where honest wording at the parent would enumerate
   fifteen exclusions. As with `generation-lane.md:135-143`, this paragraph's
   own concession at `:367-368` is the decision's evidence and should be cited
   as such rather than deleted.

4. `:332`, the insight's keywords, which include `hoisting at the parent`. That
   keyword survives; nothing in the list names repetition, so no keyword
   contradicts the amended detail. Checked, no change.

### plugins/skills-kit/skills/md-domain/SKILL.md

1. `:215-217`: "De-duplication happens during parent composition, by HOISTING: a
   fact appearing in more than one child's document moves to their common
   ancestor, reworded so it is true as stated at that depth."
   Single-child amendment. `:217-219` ("It is never proposed from below") is
   unchanged. This is the surface the Option B list missed entirely, and it is
   the one most readers meet first.

### plugins/skills-kit/skills/md-domain/references/lanes/hierarchy-lane.md

1. `:13`: "PARENT COMPOSITION, which reads every child CLAUDE.md directly and
   hoists what repeats".
   Same phrase, same amendment. This lane already ships describing a retired
   model (`plugins/skills-kit/skills/md-domain/CLAUDE.md:314-319`), so the change
   is for internal consistency rather than correctness of the lane, and it is the
   lowest-priority entry in this list.

### Surfaces read and found to need NO amendment

Named because "not on the list" and "not looked at" are different claims, and
the Option B list conflated them.

- `plugins/skills-kit/skills/md-domain/references/lanes/coverage-lane.md`. Its
  statements at `:196-203` ("Every candidate's destination is the assessed
  directory ... reaches that area by HOISTING at the parent during composition
  -- never by nomination from here"), `:264-270`, and the anti-pattern at
  `:416-419` are all still true under Option C, because the coverage side does
  not move. This is the strongest single indicator that Option C is a smaller
  change than Option B: the entire coverage lane is untouched.
- `plugins/skills-kit/skills/md-domain/workflow/coverage-detect.js`. Under
  Option B this file MECHANICALLY blocked the decision (`:98`
  `additionalProperties: false`, `:104` the required list, `:109-113` pinning
  `destination`). Under Option C it needs no change at all for the hoisting
  decision. It changes only if P1 is adopted, which is independent.
- `tests/skills-kit/test_coverage_workflow_contract.py`. Pins the coverage
  contract, not the hoisting trigger; unaffected by Option C, affected by P1
  (`:279-306`).

## Open questions this plan does not settle

1. **Does a single-child hoist need a stronger wording bar than a multi-child
   one?** Option C makes both licensable by the same test. An argument exists
   that one child's evidence is weaker evidence for a claim about all children,
   and that the parent should be told to say so. Left open deliberately: adding a
   second bar re-creates a two-test model under a new name, and the Validation
   section's single-vs-multi split will measure whether it is needed.
2. **Who enumerates composition-only subjects?** P2 settles that they ARE
   subjects; it does not settle which component adds them to the subject set.
   Coverage discovery cannot, because such a directory is not a coverage subject.
   This blocks P2's delivery, not its decision.
3. **A valid baseline.** The 23% figure conflates report-then-document with
   document-then-report. Re-measure with post-document reports excluded before
   scoring any regeneration. The reviewers' estimate is that the real figure is
   materially lower.
4. **Why does a PROMOTE candidate sometimes get written anyway?**
   `pass2-src-api` routes all seven candidates upward and loses none. Some runs
   silently wrote a PROMOTE candidate locally and others dropped it, so the
   destination field was honoured inconsistently. Under Option C the field goes
   back to being local-only, so the inconsistency cannot recur -- but the
   question of what varied between those runs is unanswered, and it is a
   question about run-to-run determinism rather than about the model.
5. **Submit gates** (`claude-md-generation-method.md:400-413`) are a
   deterministic consumer surface that bypasses ancestor-chain reach entirely.
   No document in the corpus carries one. Whether generation should emit them is
   unresolved, and Option C does not bear on it.
6. **Whether P6 (a corpus pass) can be reconciled with the one-document-per-run
   contract**, or requires changing it. Option C does not settle this, and it
   raises P6's expected volume rather than lowering it (see P6).

RESOLVED, with the resolution recorded rather than the question deleted:

- **P0, the promotion question.** Settled 2026-08-12 by owner decision: Option
  C. See P0 for the choice, the measurement behind it, and what Options A and B
  traded. Option B was provisionally settled and threaded through this document
  in commit `928b907`; two adversarial reviews returned NOT-READY and the owner
  chose Option C instead. This was the question that had to be answered before
  work started.
- **Whether a code-free directory is a composition subject.** Settled in P2: it
  is, when it has at least one in-scope child document. Under Option C this is
  not optional -- without it the decision reaches nothing at `godot/` or
  `kernel/`.
- **Three Option B questions retired with the option**: how far up a candidate
  may route, what happens when a parent declines a routed candidate, and whether
  routing above a run's root leaves a candidate permanently undispositioned. All
  three presupposed routing and have no meaning under Option C.
- **The `pass2-*` question.** `reports-json-superseded/` holds first-pass
  reports for exactly those six directories, and the task record documents the
  superseded recursive-unit pass. The 55%-vs-20% gap was also double-counting --
  11 of the 16 pass2 losses ARE the 11 null-branch losses, and all of those are
  PROMOTE.
