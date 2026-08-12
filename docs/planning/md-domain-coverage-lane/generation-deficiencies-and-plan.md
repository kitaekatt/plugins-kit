# CLAUDE.md generation: measured deficiencies and a remediation plan

Status: Part 1 (measured deficiencies) is settled measurement with its own
retractions. Part 2 is the plan after its one blocking decision was made --
P0 was answered on 2026-08-12 by owner decision (Option B, bounded) and every
other item has been reviewed against it. Nothing in Part 2 is implemented, and
the shipped-document amendments Part 2 lists have NOT been made.

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

**P0 was settled on 2026-08-12 (owner decision): Option B, bounded.** The
sections below are the plan as it stands after that decision. Each item states
whether the decision changed it; an item marked reviewed-and-unchanged was
re-read against Option B and found unaffected, which is a different claim from
not having been revisited.

### P0. SETTLED 2026-08-12 -- Option B, bounded (fixes G12, subsuming G3/G4/G5b)

**Status: decided. Authority: owner decision, 2026-08-12.** This item is no
longer an open question and no longer carries a recommendation.

Coverage emitted upward destinations; the composition model had retired upward
nomination. One of the two had to move, and the choice had to apply to EVERY
directory rather than to a favoured class. Two options were on the table.

**Option A -- coverage stops emitting non-local destinations. NOT TAKEN.**
Every candidate would target the directory it was assessed from. A fact
governing a wider area would reach it only when the parent, reading its
children's finished documents, noticed the repetition and hoisted. This
preserves the settled model exactly and needs no reversal of anything. It was
rejected on one measured cost: a fact that appears in only ONE child never
repeats, so it never triggers a hoist -- it is either written narrowly at the
child, where it over-reaches, or lost. The 13 `godot/` port facts (G3) and the
whole-port and re-port duties the audit rated most valuable are mostly of that
kind. Option A trades the corpus's most valuable facts for model purity. This
paragraph is retained so a later reader can see what was traded away; it is not
a live alternative.

**Option B -- composition gains children's ROUTED CANDIDATES as a third input.
TAKEN, BOUNDED.** Concretely:

1. A parent directory's CLAUDE.md composition has THREE inputs, not two: its
   own direct code files; its children's finished CLAUDE.md documents; and its
   children's ROUTED coverage candidates, meaning candidates whose
   `destination` names a directory above the one that was assessed.
2. Upward routing is therefore REAL rather than advisory. A routed candidate
   reaches a consumer -- the run that composes the named destination -- which
   is exactly what G12 measured as missing.
3. **The bound.** A parent MAY CONSUME a child's routed candidate. A child may
   still NEVER write outside itself, and a routed destination is an input the
   parent WEIGHS, not an obligation the child IMPOSES. The parent may decline
   it, reword it, or route it further up, and its decision is final.

**This is a deliberate partial reversal of `fact-scoped-to-this-directory`
(`plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md:94-110`),
and it is recorded here as one rather than presented as a clarification.** What
is reversed is precisely the destination clause -- ":97-98 `Its destination is
that directory, always; an assessment never proposes a fact for anywhere
else.`" What is NOT reversed is the criterion's evidence clause: a candidate
must still be a fact about the assessed directory's own direct code, cited to a
file and line in that directory. The reversal moves where a candidate may POINT,
not what it may be about.

**The cost, stated plainly.** The reason the criterion existed does not go
away. `coverage-standards.md:104-107` states it: "An assessment that read only
this directory has no basis to place anything anywhere else: it cannot see
whether the fact holds of code it never opened." That remains true under Option
B. What changes is the CONSEQUENCE, not the premise: because a routed candidate
is now a nomination the parent may ignore rather than a placement anyone must
honour, an unjustifiable nomination costs the parent a rejection instead of
producing a wrong document. The bound is what makes the reversal survivable, and
it is strictly weaker than the retired promotion model, in which a child
nominated a destination it then expected someone to honour.

Three consequences follow immediately and are handled by the items below:

- The bottom-up dependency now spans child REPORTS as well as child DOCUMENTS
  (P8).
- A destination naming a directory that is not itself a coverage subject -- the
  20 candidates of G3 -- still needs a run to exist at that directory (P2).
- Every routed candidate needs a terminal disposition at its destination, not
  only at its origin (P3).

Nothing in Option B licenses a child to write, and nothing in it revives
`sibling_overlap` or the `PROMOTE` vocabulary; see the shipped-document change
list below for the exact wording that must move.

### P1. REVISED -- one candidate schema, enforced at emit (fixes G1, G2, partly G5)

**Option B changes this item.** `destination` is no longer pinned to the
assessed directory, so the schema must express what a LEGAL routed destination
is -- otherwise the reversal reintroduces exactly the free-text drift G2
measured, with a consumer now actually reading it.

Define a single candidate record and validate it before a report is written:

| Field | Type | Rule |
|---|---|---|
| `fact` | string | required |
| `destination` | string | required; repo-relative directory, no `CLAUDE.md` suffix, no annotation, no absolute path; MUST be either the assessed directory or a strict ANCESTOR of it -- never a sibling, never a descendant |
| `routing` | enum | `local` \| `routed`; required; `routed` iff `destination` is an ancestor |
| `convertibility` | enum | `finding-convertible` \| `context-only`; required (CV-4) |
| `rationale` | string | required |
| `evidence` | list | required, non-empty; each entry `path:line` inside the ASSESSED directory (CV-7) |
| `severe_deficiency` | bool | optional, default false |

Four rules in that table are Option B's doing and each is load-bearing:

- **Ancestor-or-self, enforced.** Option B licenses routing UPWARD only. A
  sibling destination is still unjustifiable for the reason
  `coverage-standards.md:104-107` gives, and it has no consumer either -- a
  sibling's run reads neither this directory's documents nor its reports.
- **`routing`, not `scope`.** The retired vocabulary is `scope` with values
  `LEAF-ONLY` / `PROMOTE -> <dir>`
  (`plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md:183`).
  Reusing either name would make a report written under Option B
  indistinguishable from one written under the model G12 measured, and the
  corpus already contains 78 of the latter. A distinct field name is what makes
  the two eras mechanically separable.
- **`routing` is derived, not asserted.** It is a function of `destination` and
  the assessed directory, so validation computes it rather than trusting it.
  An emitter that could set them inconsistently is a second source of truth.
- **Evidence stays local.** This is the half of `fact-scoped-to-this-directory`
  that P0 did NOT reverse, and putting it in the schema is what stops the
  reversal widening past its bound.

Retire `tier`/`why`/`anchors` as aliases with a one-version compatibility read.
A report that fails validation does not get written -- a malformed candidate is
worse than a missing one because it looks routable.

**Reconciliation owed against the shipped schema.** `tier`, `why` and `anchors`
are not merely one of the corpus's four observed shapes; they are the REQUIRED
field names in the shipped workflow schema
(`plugins/skills-kit/skills/md-domain/workflow/coverage-detect.js:104`), which
also sets `additionalProperties: false` (`:98`). So this item is a change to a
published contract, not a tidy-up of corpus drift, and the rename decision
belongs with the amendments listed under "Shipped-document amendments Option B
implies" below rather than being made here.

### P2. REVISED -- composition subject vs coverage subject (fixes G3)

**The previously withdrawn P2 is reinstated in a narrower form.** The old
version let generation instantiate a document at a code-free directory "when
two or more children route a fact to it". All three objections to it were about
the smuggling, not about the underlying gap, and Option B removes all three:

- (a) it required generation to read child routes while claiming not to --
  generation now reads them openly, as P0's third input;
- (b) it applied only to code-free directories, so identical routed facts
  stayed lost for `src/` -- routing is now uniform across every directory, and
  this item no longer carries the routing rule at all;
- (c) it did not fire for `godot/extensions/woodkernel/`, which has one
  contributing child -- the two-or-more threshold was a borrowed hoisting
  trigger and is dropped. One routed candidate is enough for the parent to have
  something to weigh.

What remains is a real and separate question that Option B does NOT answer:
**a directory with no direct code files is not a COVERAGE subject, and nothing
yet says whether it is a COMPOSITION subject.** G3's 20 candidates route to
`godot/` (13), `kernel/` (6) and `godot/extensions/woodkernel/` (1), all with
zero direct code files. Under Option B those candidates now have a consumer in
principle, and still no run to consume them.

The item: **a directory is a composition subject when it has at least one child
CLAUDE.md or at least one inbound routed candidate, whether or not it has
direct code files.** Its run has an empty first input and non-empty second and
third inputs, which is a degenerate case of P0's three, not a new mode. The
subject rule for COVERAGE is untouched -- such a directory is still never
assessed, because there is nothing in it to assess.

This is the one place the plan changes what gets WRITTEN rather than what gets
read, so it should be built after P0's read side and validated by the G3 check
in the Validation section (`godot/CLAUDE.md` and `kernel/CLAUDE.md` exist and
carry the facts routed to them).

### P3. REVISED -- per-candidate accounting as the generation run's OUTPUT CONTRACT (fixes G4, G5, G5b, G10)

**Option B changes this item**, in the one way that matters: a routed candidate
is no longer terminal at its origin. The earlier draft called this the
load-bearing item because it targeted the dominant loss mechanism; under P0 the
model decision is the remedy and this is the instrumentation that proves it
landed, which is the ordering premise above applied to this item.

Today a generation run receives N candidates and returns one document. Nothing
in its contract obliges it to say what became of each candidate, so silently
writing 0 of 14 (`src/kernel`) is a conforming run. The fix is to change what a
run RETURNS, not to add a check after it:

A run must emit, alongside the document, one terminal disposition per admitted
candidate:

- `written` -- expressed in this document (cite the section)
- `hoisted` -- deferred to a named ancestor that the run also confirms exists
  or will be composed
- `routed` -- Option B's disposition: handed to the named ancestor's run as an
  input. NOT terminal at this run, and this is the whole point of naming it
  separately from `hoisted` and `deferred`
- `declined` -- with a reason from a closed set (already ambient; not evidenced
  in this directory; superseded by a broader candidate)
- `deferred` -- destination not yet composed, with the destination named

A run that returns a document without a full disposition set is INCOMPLETE and
its output is not accepted. `declined` is a legitimate and expected outcome --
the requirement is that it be stated, not that it be rare.

**Option B makes accounting two-sided, and that is the substantive change here.**
A `routed` disposition discharges the CHILD's obligation and creates the
PARENT's: the run composing the destination must return a terminal disposition
for every routed candidate it received, exactly as it does for its own. Three
outcomes are legal there -- `written`, `declined` with a reason, or `routed`
again to a higher ancestor -- and a candidate is not accounted for until some
run returns one of the first two. Without this, Option B would replace a fact
that vanished at the child with a fact that vanishes at the parent, and the G12
measurement could not tell the two apart.

One consequence worth stating because it is the bound made mechanical: the
parent's disposition set is where "the parent may decline" stops being a
sentence in P0 and becomes an artifact. A declined routed candidate is recorded
with a reason at the destination, so a child's nomination that nobody honoured
is visible rather than silent -- which is precisely the property G12 found
missing.

Consequences for G5b: a null branch becomes expressible only as a document with
every candidate `declined` or `routed`, each with a reason or a destination.
"No insight worth capturing" stops being a directory-level assertion that
silently swallows 11 admitted facts. Note this is exactly the case Option B
converts rather than fixes: all 11 null-branch candidates are PROMOTE-scoped
(G12), so under Option B they become routed candidates their parents must
account for.

Run-level reconciliation (every enumerated directory has a report, every report
a document or a full decline set, every routed candidate a terminal disposition
at an ancestor) then becomes a cheap mechanical check over those dispositions,
and the scoring key for the regeneration test below.

### P4. REVISED -- execute Verify AFTER the write, not at emit (fixes G6)

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

**Option B's one effect on this item: the cwd becomes ambiguous for a routed
candidate.** Its evidence is `path:line` inside the assessed directory (P1) but
the claim it becomes is written at an ancestor, so "run the command from the
subject's directory" now names two different directories. Pin it explicitly:
a Verify runs from the directory of the document that CARRIES the claim, and
any path in the command is repo-relative so the two readings cannot diverge
silently. This is a specification gap Option B opens, not a defect it causes --
under the pre-decision model every claim was written where its evidence was.

### P5. UNCHANGED-REVIEWED -- a verification pass over the emitted document (fixes G7)

**Option B does not change this item.** Its subject is a written document and
the code that document cites; where a claim was routed from does not alter what
re-checking it means, and a claim hoisted or routed to a parent is re-checked by
the parent's pass exactly like any other claim it carries.

Post-emit, pre-commit: re-check every claim in the written document against the
code it cites, with fresh context and the document as the subject. The
reference places this between emission and consolidation as its own phase with
its own manifest.

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

**Option B narrows this item's expected volume without removing it, and the
distinction matters for whether it is worth building.** Duplication has two
sources: a fact independently written by several children (P6's subject), and a
fact that should have sat at the parent all along. Option B addresses only the
second, and addresses it BEFORE the duplication exists rather than after -- the
parent receives the routed candidate and writes it once, so no child copy is
created to remove. What P6 is left with is the first source, which routing
cannot reach because no child proposed anything upward. The six measured
clusters (G8) are not yet sorted between the two, and that sort is what would
tell us how much of P6 survives; it is not attempted here because it would be a
re-measurement.

The ordering constraint above is unaffected in direction but gains a second
instance: under Option B a fact can reach a parent by routing as well as by
hoisting, and add-at-parent must precede remove-from-child in both cases.

### P7. UNCHANGED-REVIEWED -- enforce the authoring form (fixes G9)

**Option B does not change this item.** No H1 title, no directory inventory, at
emit. These are properties of a document's surface form and are indifferent to
where its content came from -- including at a P2 composition-only directory,
whose document is subject to the same form rules as any other.

### P8. REVISED -- a document must be stale-checked against its report (fixes G5d)

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

**Option B widens what a document's provenance must record, and this is the
item Option B changes most.** A parent's document is now composed from three
inputs, so "the report it was composed from" is no longer sufficient
provenance: a parent is stale when its OWN report is newer, when any child's
DOCUMENT is newer, or when any child's REPORT contributed a routed candidate
and has since changed. The third is the one Option B adds, and it is the one
that is invisible without being recorded, because a child's report can be
re-run without its document changing at all -- so nothing about the parent or
the child looks different.

Minimum viable form, revised: record the identity of the directory's own
report, of each child document read, AND of each child report from which a
routed candidate was consumed. The bottom-up dependency already stated in
`plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md:153-166`
is unchanged in direction; Option B adds edges to it, and
`:161-166` ("a stale child document silently corrupts its parent") now holds of
a stale child REPORT for the same reason and by the same mechanism.

### Validation: REVISED -- regenerate and score

The point of P1-P3 is that the fix can be tested rather than asserted.
Regenerate woodworking-sim and score against the ledger.

**Two scoring caveats, both from decisions already taken and both required
before a number from this run means anything:**

- The 23% headline is not a valid baseline (G5d). Any report produced after its
  document scores near 100% absent by construction, and 14 of the 71 absences
  are of that class. Exclude them before scoring, per open question 2 below.
- Under Option B a routed candidate is scored at its DESTINATION, not at its
  origin. Scoring it at the origin would count a correctly-routed fact as a
  loss and reproduce the exact confusion G4 recorded, where the corpus could not
  distinguish "hoisted at the parent" from "silently lost".

Checks:

- every admitted candidate has a terminal disposition (P3), and every `routed`
  disposition is matched by a terminal disposition at its destination;
- **the absent rate falls from the corrected baseline toward zero, where
  "absent" means no disposition OR a `written` claim the document does not
  support**;
- no candidate's `destination` is a sibling or a descendant of the assessed
  directory (P1's ancestor-or-self rule);
- no report emits `scope`, `sibling_overlap`, or the `PROMOTE` vocabulary --
  the reversal is bounded, so the retired fields must not return under Option B
  cover;
- `src/kernel` writes or explicitly declines all 14 of its candidates (today:
  0 written, 0 declined, 14 silently absent), subject to the G5d exclusion
  above, which is exactly this directory;
- `godot/scripts` writes or routes its 11 whole-port facts, and each routed one
  is dispositioned at `godot/` (today: all 11 lost to a `godot/CLAUDE.md` that
  was never created);
- `godot/CLAUDE.md` and `kernel/CLAUDE.md` exist and carry the facts routed to
  them -- this is the P2 check, and it is the single sharpest test of Option B
  end to end, since it exercises routing, the composition-only subject, and
  two-sided accounting at once;
- `godot/extensions/woodkernel/` is decided explicitly: its one routed
  candidate is either written there or declined with a reason. A single
  contributing child is the case the withdrawn P2 could not express, so
  silence here means P2 was rebuilt with the same threshold defect;
- the `pass2-*` gap closes: no subset of the run loses 55% while the rest
  loses 20%;
- no emitted Verify command disagrees with its claim;
- no fact appears at both a parent and a child without a recorded retain.

The 71 absent facts are themselves the answer key: they are enumerated per
directory in the measurement outputs, so a regeneration can be scored against
a known list rather than re-audited from scratch. The key needs one correction
before use -- the 14 G5d absences are not losses and must be removed from it --
and one relabelling: the 78 PROMOTE-scoped candidates become the routed set,
and their expected outcome changes from "written at the child" to
"dispositioned at the destination".

The pre-fix corpus is the control and is preserved in woodworking-sim git
history. Ad-hoc hand edits to the corpus weaken specific checks and are
recorded in `regeneration-ledger.md`.

## Shipped-document amendments Option B implies

The model Option B partially reverses is recorded in PUBLISHED skills-kit
content. **None of these amendments is made here.** skills-kit is a published
plugin; amending it is a separate, separately-authorized step, with a version
bump and the ordinary publish flow. This section is the change list to approve
or reject, nothing more.

Each entry quotes the passage that becomes false and states what it must say
instead. Line numbers are as of 2026-08-12.

### plugins/skills-kit/skills/md-domain/CLAUDE.md -- the authority

This file holds the decision Option B amends, so it is the one that must change
FIRST; the other two describe what it decides.

1. `:333`, the summary of `the_subject_is_one_directory_not_a_subtree`:
   "A parent gets its content by reading its children's finished CLAUDE.md
   files and hoisting what repeats -- placement is never nominated from below.
   This SUPERSEDES the promotion machinery (`scope`, `sibling_overlap`, an
   assessment naming an ancestor destination)."
   Must say instead: a parent composes from its own direct code, its children's
   finished CLAUDE.md files, and its children's ROUTED candidates; a child may
   nominate an ancestor destination but may never write outside itself, and the
   parent's decision on a nomination is final. The supersession of
   `sibling_overlap` stands; the supersession of an ancestor destination is
   partially reversed as of 2026-08-12.

2. `:355-362`, "WHY PLACEMENT MOVED TO THE PARENT", specifically `:356-359`:
   "The retired criterion invited an assessment to name a destination above
   itself, which it cannot justify: it read only its own directory, so it
   cannot know whether the fact holds of code it never opened."
   The REASONING must be kept verbatim -- Option B does not dispute it -- and
   its CONCLUSION downgraded: because the assessment cannot justify the
   placement, its nomination BINDS nobody. The parent, which has read the
   documents, decides. This is the load-bearing edit in the whole list: the
   premise survives and only the consequence moves, and an amendment that
   deletes the premise would lose the reason the bound exists.

3. `:386-390`, COMPATIBILITY: "`destination` is pinned to the subject directory
   -- degenerate, kept so reports written before this model stay loadable.
   `scope` and `sibling_overlap` are read-only for the same reason and must not
   be emitted or reintroduced under another name."
   Must say instead: `destination` is the subject directory or a strict ancestor
   of it, and is no longer degenerate; `sibling_overlap` stays retired and
   read-only; `scope` stays retired under that NAME and with those VALUES, and
   the routing distinction is carried by a differently-named field (P1's
   `routing`) so pre-decision and post-decision reports remain mechanically
   distinguishable.

4. `:303-307`, inside the amendment to `hierarchy_is_the_resolution_phase_over_a_tree`:
   "The `scope` / `sibling_overlap` fields are RETIRED (read-only compatibility
   surface); the criterion that licensed a candidate to name an ancestor
   destination is rewritten as `fact-scoped-to-this-directory`, which forbids
   it. Nothing may reintroduce nomination from below."
   The final sentence becomes false and must be amended to the bounded form:
   nomination from below is permitted as a non-binding input to the parent;
   PLACEMENT from below remains forbidden. This entry is outside the insight the
   decision brief named, and it is included because the same claim is stated
   twice in one file -- amending only the first would leave the file
   self-contradicting.

### plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md

1. `:24-28`, "Why not a subtree": "A parent gets its content instead by reading
   its children's finished CLAUDE.md files -- see
   `references/lanes/generation-lane.md`, parent composition. That input is what
   makes the non-recursive subject lossless rather than merely narrower."
   Must name the third input alongside the second, since under Option B it is
   the routed candidates, not the child documents alone, that carry the
   single-child facts. The losslessness claim is what Option B was chosen to
   make true; as written it credits the wrong input.

2. `:94-110`, the `fact-scoped-to-this-directory` criterion. The statement
   `:97-98` -- "Its destination is that directory, always; an assessment never
   proposes a fact for anywhere else." -- is the sentence P0 reverses. It must
   become: the destination is that directory or a strict ancestor of it, and a
   non-local destination is a nomination the ancestor's composition may accept
   or decline. The FIRST clause of the statement (`:96-97`, "A candidate must be
   a fact about the assessed directory's own direct code") is unchanged and must
   be kept, because it is the bound.
   The keywords at `:101` include `no nomination, no promotion, no hoisting from
   below`, which must be replaced rather than extended -- a keyword that
   contradicts the criterion routes readers to the wrong answer.
   The example at `:107-110` -- "A fact that genuinely governs a wider area
   reaches that area by HOISTING, which happens at the parent when the parent
   observes the same fact in more than one child's document -- never by
   nomination from below." -- must state both routes: hoisting on repetition,
   and routing on nomination, with the parent deciding in both cases.
   **The rule id must NOT be renamed**, however poorly `fact-scoped-to-this-directory`
   now describes the criterion. `plugins/skills-kit/skills/md-domain/CLAUDE.md:104-118`
   preserves all rule ids verbatim through the fold and makes the golden corpus
   the gate on them; a rename is its own decision with its own re-record. Record
   the id/name mismatch as a follow-up instead.

3. `:181-203`, "Two RETIRED carriage fields on a candidate". `:201-203` --
   "Do not emit either field, and do not reintroduce an equivalent. A
   `destination` pointing anywhere but the subject directory is a criterion
   violation, not a hint." -- has two claims and they now diverge. The field ban
   stands for both `scope` and `sibling_overlap`. The destination claim is
   reversed for ancestors and stands for everything else. The section must be
   split so the reader cannot read the surviving half as licence for the
   reversed one.

### plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md

1. `:99-100`: "A `claude-md` run whose directory contains child directories is a
   COMPOSITION, and it has TWO inputs" plus the numbered list at `:102-109`.
   Must become THREE inputs, the third being the children's routed candidates,
   with the bound stated in the same place: the parent may decline any of them.

2. `:91-95`: "The second input is the children's own CLAUDE.md files, and
   reading them is part of this procedure, not of the report". Must name both
   the second and third inputs; the "part of this procedure, not of the report"
   framing is correct and should be kept for the third as well.

3. `:111-113`: "The second input is what makes the non-recursive subject
   lossless rather than merely narrower ... Neither input substitutes for the
   other". Same correction as coverage-standards item 1, and the
   neither-substitutes claim must extend to three.

4. `:124-133`, "Hoisting is where de-duplication happens", specifically
   `:127-130`: "It is never nominated from below: an assessment that read only
   its own directory cannot know whether the fact holds of code it never opened,
   and `fact-scoped-to-this-directory` forbids it proposing a destination
   anywhere else (`../standards/coverage-standards.md:94-110`)."
   Must distinguish the two routes: HOISTING is never nominated from below and
   that is unchanged; ROUTING is nomination from below and is now permitted,
   non-binding. The cross-reference line range at `:130` will move when
   coverage-standards is amended and must be re-resolved rather than carried
   over.

5. `:153-166`, "Order is strictly BOTTOM-UP". Direction is unchanged and the
   two stated consequences hold. It must gain the third input's edge: a child's
   REPORT is now an input to its parent, so a re-assessed child invalidates its
   parent even when the child's document does not change. `:161-166`, "A stale
   child document silently corrupts its parent", holds identically of a stale
   child report and should say so -- this is P8's requirement expressed at the
   place the dependency is defined.

6. `:364-366`, the anti-pattern "Composing a parent without reading its
   children's documents ... The parent's own direct code is only half its
   input." The arithmetic is now wrong as well as the list: direct code is one
   of three inputs, and a composition that reads the child documents but ignores
   the routed candidates is the same anti-pattern in its Option B form.

### Surfaces this list does not cover, found but not read

Named so the owner can scope the amendment, not analyzed here. Each was located
by grep for the retired vocabulary and is quoted only by line:

- `plugins/skills-kit/skills/md-domain/workflow/coverage-detect.js:127-134` --
  a comment stating `scope` and `sibling_overlap` "were REMOVED from this schema
  deliberately, and the removal is the enforcement", and `:109-113` -- a comment
  pinning `destination` to "ALWAYS the assessed directory". With
  `additionalProperties: false` at `:98` and the required list at `:104`, this
  file MECHANICALLY blocks Option B: an assessment cannot emit a routing field
  at all. It is the binding constraint, not documentation of one.
- `plugins/skills-kit/skills/md-domain/references/lanes/coverage-lane.md:299-316`
  -- a second copy of the retired-fields section, whose `:315-316` repeats the
  do-not-reintroduce ban.
- `plugins/skills-kit/skills/md-domain/references/lanes/hierarchy-lane.md:22`
  and `plugins/skills-kit/skills/md-domain/workflow/hierarchy-detect.js:111,584`
  -- the hierarchy lane consumes `sibling_overlap` and reasons about
  `PROMOTE`-scoped candidates. That lane already ships describing a retired
  model (`plugins/skills-kit/skills/md-domain/CLAUDE.md:315-319`), so Option B
  changes what it is wrong ABOUT rather than making it newly wrong.

## Open questions this plan does not settle

1. **How far up may a candidate route?** Option B permits an ancestor
   destination and does not say whether that means the immediate parent only or
   any ancestor. Any-ancestor is what the corpus needs -- `godot/scripts` routes
   to `godot/`, its immediate parent, but 13 `godot/` candidates arrive from
   more than one depth -- and it is also the weaker bound, since a fact can
   travel arbitrarily far from the evidence that justified it without any
   intermediate run seeing it. Parent-only forces each hop through a run that
   has read the documents, at the cost of a fact needing several hops to arrive.
2. **What happens to a routed candidate the parent declines?** The bound makes
   the parent's decision final, which is correct, and leaves the fact with
   nowhere to go. Two readings: the decline is terminal and the fact is dropped
   with a recorded reason, or the child retains a narrow local fallback. The
   first is cleaner; the second is what would have prevented the G3 losses if
   the parent had refused. This must be settled before P3's disposition set is
   frozen, because the two produce different closed sets.
3. **Does routing above the run's root leave a candidate permanently
   undispositioned?** A run scoped to a subtree can receive a candidate routed
   above its own root, where no run will ever compose the destination. P3 would
   record it as `routed` forever, which is a silent loss wearing a disposition.
4. **Why does a PROMOTE candidate sometimes get written anyway?**
   `pass2-src-api` routes all seven candidates upward and loses none. If some
   runs silently write a routed candidate locally, the destination field is
   being honoured inconsistently. Option B does not remove this question, it
   sharpens it: under the settled model that behaviour becomes a fact written
   in two places, since the destination's run will also receive it.
5. **A valid baseline.** The 23% figure conflates report-then-document with
   document-then-report. Re-measure with post-document reports excluded before
   scoring any regeneration. The reviewers' estimate is that the real figure is
   materially lower.
6. **Submit gates** (`claude-md-generation-method.md:400-413`) are a
   deterministic consumer surface that bypasses ancestor-chain reach entirely.
   No document in the corpus carries one. Whether generation should emit them is
   unresolved, and Option B does not bear on it.
7. **Whether P6 (a corpus pass) can be reconciled with the one-document-per-run
   contract**, or requires changing it. Option B does not settle this; it only
   reduces how much duplication P6 is expected to find (see P6).

RESOLVED, with the resolution recorded rather than the question deleted:

- **P0, Option A or Option B.** Settled 2026-08-12 by owner decision: Option B,
  bounded. See P0 above for the choice, the bound, the reversal and its cost.
  This was the question that had to be answered before work started.
- **The `pass2-*` question.** `reports-json-superseded/` holds first-pass
  reports for exactly those six directories, and the task record documents the
  superseded recursive-unit pass. The 55%-vs-20% gap was also double-counting --
  11 of the 16 pass2 losses ARE the 11 null-branch losses, and all of those are
  PROMOTE.
