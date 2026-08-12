# CLAUDE.md generation: measured deficiencies and a remediation plan

Status: DRAFT, under review. Nothing here is agreed or implemented.

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

### G12 `[OBS]` Coverage emits a retired placement vocabulary that generation cannot consume

**This is the dominant defect.** 78 of 316 candidates carry `scope: PROMOTE`
with a destination above the assessed directory. Under the settled model
promotion is retired -- a fact reaches a wider scope only by HOISTING, which the
PARENT performs after noticing the fact repeated across its children's finished
DOCUMENTS. Nothing is ever nominated from below.

That leaves a PROMOTE candidate with no path to any document:

- the child's own run is forbidden to write it (`fact-scoped-to-this-directory`);
- the parent's run composes from child DOCUMENTS, not child REPORTS, and the
  child was forbidden to write the fact, so the parent never sees it;
- the destination it names is advisory text no stage reads.

The correlation is close to exact in ten reports. It is NOT deterministic,
which is its own finding: `pass2-src-api` has 7 candidates, all 7 PROMOTE, and
loses none -- so some runs write a PROMOTE candidate locally, ignoring the
destination, and others drop it. The field is honoured inconsistently rather
than uniformly ignored.

This subsumes G3 (an unwritable `godot/` destination is one PROMOTE
destination among others), G4, and G5b (all 11 null-branch candidates are
PROMOTE). It is one decision, not four fixes.

**The decision this forces, and it must be made before anything else in Part 2
is worth building:** either coverage stops emitting non-local destinations
entirely, or composition gains child REPORTS as a third input so a parent can
see what its children routed upward. Both reviewers reached this independently.
The plan's P2 chose neither and instead smuggled half the reversal in for
code-free directories only (see P2 below).

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

**This kills the plan's original shape.** Three drafts distributed the loss
across four mechanisms and proposed a fix per mechanism. There is essentially
one mechanism, it is a MODEL question rather than an implementation defect, and
until it is decided the rest of the plan is building instrumentation around an
unresolved contradiction.

The path to that was three consecutive wrong attributions -- `kernel/src` (a
mirrored directory name), a glob that dropped two reports, and a suppression
rule mistaken for omission. Each was caught by measurement or by review, none
by inspection of the plan.

Revised ordering: **P0 first and alone.** P1 and P3 are worth building
regardless as instrumentation, but they are no longer claimed as remedies.
P4-P8 should not be designed until P0 is settled, because P0 changes what a
destination MEANS and therefore what every later item validates against.

### P1. One candidate schema, enforced at emit (fixes G1, G2, partly G5)

Define a single candidate record and validate it before a report is written:

| Field | Type | Rule |
|---|---|---|
| `fact` | string | required |
| `destination` | string | required; repo-relative directory, no `CLAUDE.md` suffix, no annotation, no absolute path |
| `convertibility` | enum | `finding-convertible` \| `context-only`; required (CV-4) |
| `scope` | enum | `local` \| `promote` \| `hoist`; required |
| `rationale` | string | required |
| `evidence` | list | required, non-empty; each entry `path:line` (CV-7) |
| `severe_deficiency` | bool | optional, default false |

Retire `tier`/`why`/`anchors` as aliases with a one-version compatibility read.
A report that fails validation does not get written -- a malformed candidate is
worse than a missing one because it looks routable.

### P0. Settle the promotion question, once, uniformly (fixes G12, subsuming G3/G4/G5b)

Coverage emits upward destinations; the composition model retired upward
nomination. One of the two must move, and the choice must apply to EVERY
directory rather than to a favoured class.

**Option A -- coverage stops emitting non-local destinations.** Every candidate
targets the directory it was assessed from. A fact that governs a wider area
reaches it only when the parent, reading its children's finished documents,
notices the repetition and hoists. Preserves the model exactly. Cost: a fact
that appears in only ONE child never repeats, so it never hoists -- it is
either written narrowly at the child (where it over-reaches) or lost. The 13
`godot/` port facts are mostly of this kind.

**Option B -- composition gains child REPORTS as a third input.** A parent
composes from its own code, its children's documents, AND its children's
routed candidates. Upward routing becomes real rather than advisory. Cost: this
is the retired promotion model returning under another name, and it reopens
what `fact-scoped-to-this-directory` was introduced to close.

**Recommendation: Option B, narrowly.** Option A is internally cleaner but
loses the single-child cross-tree facts, which are exactly the facts the audit
found most valuable (the whole-port rules at `godot/`, the C++ re-port duty).
Option B's cost is a real model reversal and should be taken deliberately,
recorded as such, and bounded: a parent may consume a child's ROUTED candidate,
but a child may still not write outside itself. That is weaker than the retired
promotion, which let a child nominate a destination it then expected someone to
honour.

**P2 as previously written is withdrawn.** It let generation instantiate a
document at a code-free directory "when two or more children route a fact to
it", which (a) requires generation to read child routes -- i.e. Option B --
while claiming not to, (b) applies the rule only to code-free directories, so
the identical routed facts stay lost for `src/`, and (c) does not even fire for
`godot/extensions/woodkernel/`, which has one contributing child. Both
reviewers identified it as smuggling half of Option B in.

### P3. Per-candidate accounting as the generation run's OUTPUT CONTRACT (fixes G4, G5, G5b, G10)

This is the load-bearing item, because it targets the 61% mechanism.

Today a generation run receives N candidates and returns one document. Nothing
in its contract obliges it to say what became of each candidate, so silently
writing 0 of 14 (`src/kernel`) is a conforming run. The fix is to change what a
run RETURNS, not to add a check after it:

A run must emit, alongside the document, one terminal disposition per admitted
candidate:

- `written` -- expressed in this document (cite the section)
- `hoisted` -- deferred to a named ancestor that the run also confirms exists
  or will be composed
- `declined` -- with a reason from a closed set (already ambient; not
  scoped to this directory; superseded by a broader candidate)
- `deferred` -- destination not yet composed, with the destination named

A run that returns a document without a full disposition set is INCOMPLETE and
its output is not accepted. `declined` is a legitimate and expected outcome --
the requirement is that it be stated, not that it be rare.

Consequences for G5b: a null branch becomes expressible only as a document with
every candidate `declined`, each with a reason. "No insight worth capturing"
stops being a directory-level assertion that silently swallows 11 admitted
facts.

Run-level reconciliation (every enumerated directory has a report, every report
a document or a full decline set) then becomes a cheap mechanical check over
those dispositions, and the scoring key for the regeneration test below.

### P4. Execute Verify -- AFTER the write, not at emit (fixes G6)

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

### P5. A verification pass over the emitted document (fixes G7)

Post-emit, pre-commit: re-check every claim in the written document against the
code it cites, with fresh context and the document as the subject. The
reference places this between emission and consolidation as its own phase with
its own manifest.

### P6. A corpus pass, as a READ-ONLY planner plus ordinary per-directory runs (fixes G8)

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

### P7. Enforce the authoring form (fixes G9)

No H1 title, no directory inventory, at emit.

### P8. A document must be stale-checked against its report (fixes G5d)

The chain has no concept of a document being out of date with respect to a
newer assessment, so a documented directory is silently skipped and its report
discarded. A generation run must compare the document's provenance against the
report that would compose it, and treat `document older than report` as
REGENERATE rather than SKIP.

This is the item that makes the workflow a REgeneration workflow rather than a
one-shot. It also determines what a second run over an already-documented
corpus is worth, which is the question the validation below actually asks.

Minimum viable form: record, in or beside each document, the identity of the
report it was composed from. A run whose report differs from the recorded one
regenerates; a run whose report matches skips legitimately.

### Validation: regenerate and score

The point of P1-P3 is that the fix can be tested rather than asserted.
Regenerate woodworking-sim and score against the ledger:

- every admitted candidate has a terminal disposition (P3);
- **the absent rate falls from 23% (71 of 310) toward zero, where "absent"
  means no disposition OR a `written` claim the document does not support**;
- `src/kernel` writes or explicitly declines all 14 of its candidates (today:
  0 written, 0 declined, 14 silently absent);
- `godot/scripts` writes or hoists its 11 whole-port facts (today: all 11 lost
  to a `godot/CLAUDE.md` that was never created);
- `godot/CLAUDE.md` and `kernel/CLAUDE.md` exist and carry the facts routed to
  them;
- the `pass2-*` gap closes: no subset of the run loses 55% while the rest
  loses 20%;
- no emitted Verify command disagrees with its claim;
- no fact appears at both a parent and a child without a recorded retain.

The 71 absent facts are themselves the answer key: they are enumerated per
directory in the measurement outputs, so a regeneration can be scored against
a known list rather than re-audited from scratch.

The pre-fix corpus is the control and is preserved in woodworking-sim git
history. Ad-hoc hand edits to the corpus weaken specific checks and are
recorded in `regeneration-ledger.md`.

## Open questions this plan does not settle

1. **P0 Option A or Option B.** This is the only question that has to be
   answered before work starts, and it is a model decision rather than a
   technical one.
2. **Why does a PROMOTE candidate sometimes get written anyway?**
   `pass2-src-api` routes all seven candidates upward and loses none. If some
   runs silently write a PROMOTE candidate locally, the destination field is
   being honoured inconsistently, and Option A would change the behaviour of
   those runs too.
3. **A valid baseline.** The 23% figure conflates report-then-document with
   document-then-report. Re-measure with post-document reports excluded before
   scoring any regeneration. The reviewers' estimate is that the real figure is
   materially lower.

RESOLVED since the draft: the `pass2-*` question. `reports-json-superseded/`
holds first-pass reports for exactly those six directories, and the task record
documents the superseded recursive-unit pass. The 55%-vs-20% gap was also
double-counting -- 11 of the 16 pass2 losses ARE the 11 null-branch losses, and
all of those are PROMOTE.
2. Submit gates (`claude-md-generation-method.md:400-413`) are a deterministic
   consumer surface that bypasses ancestor-chain reach entirely. No document in
   the corpus carries one. Whether generation should emit them is unresolved.
3. Whether P6 (a corpus pass) can be reconciled with the one-document-per-run
   contract, or requires changing it.
