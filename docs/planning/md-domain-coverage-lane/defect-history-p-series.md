# md-domain defect history -- the P-series

Subject: `skills-kit`, skill `md-domain`, principally its ANALYZE (coverage)
lane.

Status: a durable record of defects found by running the lane at corpus scale,
with the proposal that came out of each. Entries P1-P8 were written 2026-08-20
and 2026-08-21 against `skills-kit` 0.58.0. Entries P9-P13 were written
2026-08-24 and record what a promotion run found and what shipped as
`skills-kit` 0.61.0 and 0.62.0.

This is a maintainer document. It does not ship to consumers, and it is not the
as-built documentation for anything described here -- where a fix shipped, the
shipped criteria, lane procedure, and insight records in
`plugins/skills-kit/skills/md-domain/` are the source of truth. This file is the
history of how those were arrived at.

## Known limit -- read this before quoting any number

**The 2026-08-24 promotion run and recall audit were taken over ONE root**, a
skills and tooling tree, atypical of the corpus it was drawn from. A second
root -- 81 subjects of a large C++ game corpus -- was subsequently taken
through the shipped lane (skills-kit 0.62.0) end to end; see "Second root --
replication evidence" in Part 3. What that run does and does not settle:

- ESTABLISHED: the base rate of "false as written" among proposed candidates
  is stable across two structurally different corpora -- a skills-and-tooling
  tree and a large C++ game corpus. The case for a truth verifier (P9) does
  not rest on one atypical root.
- NOT ESTABLISHED: end-to-end yield across the two roots, deliberately. The
  first root's yield (~41.6 percent, Part 3 below) was measured through a
  two-gate pipeline -- proposal, then a criteria-only promotion gate. The
  second root passed through one gate only and kept 82.7 percent of
  candidates. Those two numbers describe different pipelines and must not be
  presented as a trend.
- NOT ESTABLISHED: generalization beyond this. Two roots are not a corpus, and
  both are from the same organization's tree.

The corpus-scale figures in Part 1 (the 126 proposals, the 305 missed
directories) come from a different and much larger root -- a C++ game corpus --
but they measure an operator working AROUND the lane, not the lane.

## Provenance and sanitization

The 2026-08-20 material was written inside a client project's scratch tree and
cited its evidence by path. This repo is public, so every client path, project
name, depot name, and product-specific identifier has been replaced with a
description of what the thing WAS. Where the original cited a file, this version
says what the file contained and that it lives privately. No substitute paths
have been invented; a description that reads like a path is not one.

## Part 1 -- What happened (corpus run, 2026-08-20)

### Context

The task: full regeneration and audit of markdown across a large C++ game corpus
(a game client on a mainstream engine plus a custom C++ backend). Corpus: 841
authored directories, 4.1M LOC, 186 owned CLAUDE.md files, 58 SKILL.md, about 97
skill reference docs, 57 project docs.

The operator wrote a 7-phase plan, then executed its Phase 0 (scope triage) by
fanning out to four background CLI agents, each classifying a partition of
directories into bands: A (generate a CLAUDE.md), B (analyze later), C (skip).

### Failure 1 -- Paraphrased criteria, 71 percent false-positive rate

The operator authored a criteria brief by hand and gave it to each agent. It
required that a band-A promotion cite `path/to/file:LINE` as evidence.

The four sweeps proposed **126 new CLAUDE.md files**. A cross-check pass (top
model rung, prompted to refute) **downgraded 90 of them -- 71 percent**:

| Downgrade reason | Count |
|---|---|
| the fact is repo-wide, already in the root CLAUDE.md | 41 |
| the citation only showed the thing happening (an include, a macro use) | 27 |
| the citation pointed at a parent or sibling directory | 22 |

70 of the 126 rested on just three boilerplate reason strings -- "uses the
project's custom network transport" (40), "editor-only code must not ship" (20),
"uses the project's assertion-macro convention" (10). All three are
project-wide rules already stated in the repo-root CLAUDE.md.

**All three downgrade rules already existed in `coverage-standards.md`**, as
`fail`-severity criteria:

| The hand-rolled rule | Existing criterion |
|---|---|
| repo-wide | `already-ambient-suppressed` -- *"A rule stated in the repo-root CLAUDE.md is reaching every file beneath it. Proposing it again in a subdirectory is duplication."* |
| wrong-dir | `fact-scoped-to-this-directory` -- *"REJECT a fact whose subject is a file in a subdirectory, a sibling, or a parent."* |
| visible | `absent-fact-earns-ambient-cost` -- *"REJECT a restatement of what a function's name already says."* |

And `coverage-lane.md` forbade exactly what was done:

> Apply `references/standards/coverage-standards.md` verbatim. The caller MUST
> resolve that document to an ABSOLUTE path and pass it as `refs.criteria` to
> `workflow/coverage-detect.js`; **never embed or paraphrase the criteria**.

The rule was right, present, and unread. It lives in `coverage-lane.md`, which
the operator never loaded, because the operator never ran the analyze lane.

### Failure 2 -- Hand-rolled file filter, 305 directories missed

To enumerate subjects the operator wrote a directory walker with the extension
set `{.cpp .h .py .cs .fbs}`. md-domain's shared `CODE_DATA_EXT` (in
`scripts/discover_claude_md.py`, consumed by `scripts/discover_coverage.py`) also
includes `.yaml .yml .csv .json .toml .sql .sh .ps1` and more.

Consequence: every config-data directory was silently outside scope. Re-running
with the correct set found **305 additional directories, 24 of which already had
a CLAUDE.md**. `discover_coverage.py` exists and would have produced the right
set. It was not used.

### Failure 3 -- No corpus-scale entry point, so one was invented

`SKILL.md` advertises the tree form in its greeting:

> "give src/ and everything under it CLAUDE.md files" -- the whole-tree form,
> deepest first

and the analyze lane is explicitly per-directory with **no whole-repo default**.
The plugin's honest answer for 841 directories is "run analyze 841 times; most
return `COVERAGE-ASSESSED`."

That is correct and unaffordable. So the operator built a cheap pre-filter
(banding) to decide which directories deserved a full analyze run. **A pre-filter
is by construction a substitute for the analyze lane**, so building one
guaranteed the work happened outside the lane's criteria. Phase 0 -- the phase
that produced both failures above -- is the one phase in the plan that named no
dispatch.

### The structural path

The four steps generalize, and that is the point:

```
corpus-scale request
  -> operator writes a plan (because the unit is one directory and the job is 841)
  -> the plan invents phases that are not lanes (triage, inventory, scoping)
  -> the operator executes the plan and never loads a lane
  -> the lane's criteria, and the rule to pass them verbatim, are never seen
```

Any sufficiently large md-domain job tends to produce this, because the
per-directory unit *forces* a planning layer above it, and that layer is exactly
where the criteria stop being reachable. The guardrails are in the lanes; the
operator is above the lanes.

### An unplanned A/B measurement

After diagnosing failure 1, the same dispatch shape was re-run over the 305
missed directories, changing exactly one thing: the brief stated what the
evidence must *prove* (non-obvious, directory-specific, points inside the
directory) instead of merely that a citation must exist.

| | code tier (loose bar) | data tier (stated bar) |
|---|---|---|
| directories | 536 | 84 (first partition returned) |
| new CLAUDE.md proposed | 126 | **0** |
| survived scrutiny | 36 (29 percent) | n/a -- none proposed |
| junk requiring a cull pass | 90 | 0 |

Same model, same effort, same dispatch shape. Stating the bar eliminated the
false-positive class outright. The cull pass that recovered the first result cost
a full top-rung run over 126 items; it was avoidable.

## Part 2 -- Proposals P1-P8

Ranked by value-to-effort as of 2026-08-21. Status lines are given only where the
record establishes one; absence of a status line means this document does not
record whether the item shipped, not that it did not.

### P1 -- Put the delegation guardrail in SKILL.md (high value, trivial effort)

**Problem:** the verbatim-criteria rule lives in `coverage-lane.md`, reachable
only by an operator who loads that lane. The operator most likely to violate it
is precisely the one who does not.

**Change:** add to `SKILL.md`'s `behavioral_guardrails`:

> When this work is handed to another agent -- a subagent, a background CLI, a
> workflow -- pass the artifact's standards document to it VERBATIM by absolute
> path. A brief that paraphrases the criteria is not the criteria, and the
> agent's output will satisfy the paraphrase rather than the standard. This
> applies to every verb, and it applies whether or not you are inside a lane.

`SKILL.md` is the one document always in context -- including for an agent about
to plan its way out of loading anything else. That is the whole argument for
placing it there rather than deeper.

**Effort:** one guardrail entry.

### P2 -- Surface the discovery script as the enumeration entry point (high value, trivial effort)

**Problem:** `discover_coverage.py` appears only as a `discover_script:` field in
the lane records table -- a data field, not an instruction. Nothing says "use
this instead of writing your own file walk," so a hand-rolled filter looks like
ordinary setup work rather than a divergence.

**Change:** add to `SKILL.md`, near the analyze/generate dispatch guidance:

> To enumerate subjects, run `scripts/discover_coverage.py <directory>` (or
> `--diff`). Do NOT hand-roll a directory walk or a file-extension filter: the
> script owns the shared `CODE_DATA_EXT` set, the VCS-ignore exclusion, and the
> ancestor chain, and a narrower hand-written set silently shrinks scope rather
> than failing.

Note the failure direction: a hand-rolled filter does not error, it just omits.
The one in Part 1 omitted 305 directories including 24 that already had a
CLAUDE.md.

**Effort:** one paragraph.

### P3 -- A corpus-scale triage capability (high value, real design work)

**Problem:** the tree form is advertised but its only execution strategy is
"analyze every directory." At corpus scale that forces the operator to invent a
pre-filter, and the pre-filter lands outside the criteria.

**Change:** give the plugin the triage step, so the criteria stay attached to it.
Sketch, not a specification:

- A `triage` verb (or a documented pre-phase of `analyze`) taking a root and
  returning a ranked/banded directory list, report-only.
- It already has everything it needs: the extension set, the VCS-ignore logic,
  the ancestor chain, and `has_claude_md`.
- Bands should be defined in terms the existing criteria already use, so a triage
  result is a *cheap prediction of* the analyze verdict rather than a separate
  opinion. Suggested framing: a directory whose facts would all be suppressed by
  `already-ambient-suppressed` is exactly a band-C directory.
- Must stay report-only, like `analyze`, with the same
  `GAPS-FOUND`/`COVERAGE-ASSESSED` discipline and an explicit human gate.

**Open question for the maintainer:** is triage genuinely cheaper than analyze,
or does an honest triage converge on doing the analysis anyway? The banding in
Part 1 used file counts, LOC, changelist churn, and a shallow read -- and its A
band still needed a 71 percent cull. That may be evidence that cheap triage is
not reliable, and that the right answer is instead to make `analyze` cheap enough
to run 841 times. There is no data here to settle it.

**Effort:** design conversation plus implementation.

### P4 -- Name the planning layer as a risk (medium value, trivial effort)

**Problem:** md-domain has a `plan-checkpoint` concept only via `awesome-kit`'s
orchestrate policy, and nothing in md-domain warns that a corpus-scale plan is
the specific artifact that routes work away from the lanes. The Part 1 plan went
through orchestrate's checkpoint, was judged "not load-bearing, cheap to unwind,
skip review," and that was the last gate that would have caught this.

**Change:** add a short note to `SKILL.md` -- when a job is large enough to need
a plan, every phase in that plan should name the dispatch it runs, and a phase
that names none is the signal that the work has left the plugin. A phase without
a dispatch is not necessarily wrong, but it inherits no criteria and must say
where its criteria come from.

**Effort:** one paragraph.

### P5 -- Prefer a positive vendoring MARKER over a directory-name list (added 2026-08-21)

**Problem:** `discover_coverage.py` decides vendoring from `VENDOR_DIR_NAMES`, a
list of directory basenames. A name list fails in both directions at once, and
the corpus demonstrated both on the same day:

- MISS -- the list held `third_party` and `thirdparty` but the corpus convention
  was a CamelCase spelling of the same name, so roughly 300 vendored directories
  under a single shared-library root arrived as subjects. (Fixed: the match is
  now case-insensitive.)
- FALSE POSITIVE -- a path-segment name rule cannot tell a vendored library from
  first-party build glue parked beside it. Making the match case-insensitive
  correctly pruned the vendored payload AND incorrectly pruned about 10
  first-party directories whose only direct file is a team-authored build script.
  The non-recursive subject makes this sharper, not softer: such a directory's
  own direct file is first-party, while the upstream library is a versioned
  sibling subtree that the same rule excludes correctly.

Separately, a hand-rolled exclusion regex in the run's own scratch tooling --
built because the plugin's list did not cover the corpus -- wrongly excluded 32
of 899 directories, including a heavily-churned schema directory that already had
a CLAUDE.md, via a case-insensitive substring collision with a library token.
Evidence: an exclusion-miss audit in the run's scratch tree (private).

**Change:** treat an upstream provenance MARKER as the primary vendoring signal
and demote the name list to a fallback. In that corpus the marker is a
`source.txt` and/or `LICENSE` in the directory or an ancestor; measured against
the 899, that single check classifies **611 correctly with no hand-written
tokens**, and it is what surfaced most of the 32 misses.

The generalization needs maintainer judgment and is the reason this is a proposal
rather than a patch: a bare `LICENSE` cannot mean "vendored" unconditionally, or
a repo-root LICENSE would prune the entire repo. Plausible framings, unranked --
a marker only counts BELOW the project root; a marker counts only when it names a
party other than the project; `source.txt` (an explicit upstream-provenance file)
counts on its own while `LICENSE` counts only alongside another signal.

**Effort:** design conversation plus implementation. Lower risk than P3 and it
subsumes part of it -- a marker-based rule shrinks the corpus honestly, which is
the affordability problem P3 was reaching for.

### P6 -- The skill-existence check cannot see plugin-provided skills (added 2026-08-21)

**Problem:** `audit_skill` reported, at SERIOUS severity, that a project command's
`[FAIL]`-gating C++ verification step was FICTIONAL because the verification
skill it calls does not exist. The finding was wrong. That skill ships in a
plugin from a second, private marketplace, and the plugin is enabled for the
project in its `.claude/bootstrap.json`.

The mechanism is worth stating precisely, because it defeats every obvious check
at once. The skill declares `disable-model-invocation: true`, which removes it
from the session's model-invocable skill roster while leaving it installed and
user-invocable. So all four of these come back empty for a skill that plainly
exists:

- no directory under `.claude/skills/`
- no `.claude/commands/` entry
- a grep for the skill's `name:` under `.claude/` finds nothing
- the name is absent from the session skill roster

The auditor enumerated the roster in its own finding text as positive evidence of
absence. That is the defect: a roster listing model-invocable skills is not an
inventory of installed skills, and treating it as one converts a hidden-by-design
skill into a fabricated SERIOUS finding against a safety gate.

The severity asymmetry matters. A missed real defect is a gap; a fabricated
SERIOUS finding against a protective mechanism invites someone to DELETE a
working gate. The proposed remediation in this instance was to author the missing
skill or else strip the verification step, its skip flag, its checklist item and
its sample report line -- that is, to remove a functioning C++ verification gate.

**Change:** resolve skill references against the installed plugin set, not only
the consuming repo. The inputs already exist -- `.claude/bootstrap.json` names the
marketplaces and the enabled plugins, and the plugin cache is on disk. Minimum
viable fix: before emitting any dangling-skill-reference finding, check
`plugins/*/skills/<name>/SKILL.md` across the enabled plugins, and never cite the
model-invocable roster as evidence of absence.

Note the check must stay honest in the other direction: in the same audit, three
other referenced skills were verified absent from BOTH plugin repos and are
genuinely broken references. The fix is to resolve the plugin set, not to stop
reporting dangling skill references.

**Effort:** one resolution step plus a rule against roster-as-inventory.

### P7 -- Every workflow lane marshals its args inline (added 2026-08-21)

**Problem:** originally logged against `coverage-detect.js`, but it is not
specific to that lane. `skill-remediate.js` takes
`args = {perFile:[{path, remediations:[...]}]}` and, for a 32-file remediation
pass, that payload is 48 KB. The Workflow tool accepts args only inline, so an
orchestrator must reproduce 48 KB of JSON verbatim in a tool call -- where a
single escaping error silently ALTERS a remediation instruction rather than
failing loudly. In that run the dispatch was abandoned for this reason and the
agents were pointed at the decided-remediation file on disk instead.

**Change:** give the file-taking lanes the same treatment `coverage-detect.js` is
getting -- an argument naming an ABSOLUTE path the agents read themselves, with
the inline form retained for small runs. The lanes' own agents have filesystem
access; only the sandboxed script body does not.

**Effort:** mirrors the `subjectsFile` work, once per lane.

### P8 -- Sniff for binary instead of maintaining an extension list (added 2026-08-21)

**Problem:** `discover_coverage.py` deliberately KEEPS a directory whose files
carry an unrecognized extension, because silently dropping one made "a whole
subtree of an unrecognized language read as an EMPTY, well-formed subject -- the
strongest verdict, over a subtree that was never read at all." That reasoning is
correct and must be preserved.

But the safeguard fires on any extension absent from both `CODE_DATA_EXT` and
`ASSET_BINARY_EXT`, and a BINARY container in that gap manufactures phantom
subjects at the scale of the consuming project. Measured on the C++ game corpus
before the fix: **1,510 of 3,071 enumerated subjects (49 percent) had zero direct
code files**, 1,318 of them game-engine content directories, driven by the
engine's binary asset extension (30,593 files) and its map extension (594). At
the measured ~40K tokens per advanced-depth subject, running the corpus in that
state would have spent roughly 53 million tokens on directories holding no
readable source -- and it would have looked like progress the whole way.

The constant's own comment predicted the direction and understated the magnitude:
"A MISSING entry here only causes NOISE ... never a MISSED SUBJECT -- that is the
safe direction to err in." The direction is safe. The cost is not cosmetic.

**Change:** before counting a file as an unknown extension, sniff it -- a NUL byte
in the first 8 KB means binary, so absorb it as an asset instead of raising a
discovery failure. The evidence that this is the right discriminator rather than
a guess came from opening four extensions with a byte dump: two were binary
containers (one of them opening with a FlatBuffers vtable header) and would be
absorbed; two LOOK binary from their names and are plain text, and would
correctly stay visible. A name-based list gets all four wrong without someone
opening the files.

This does not delete the safeguard -- an unrecognized TEXT extension still
surfaces, which is the case it exists for.

**Why it was not in that change:** it alters the behaviour of the one mechanism in
the module whose misfires are expensive in BOTH directions -- a missed binary
costs phantom subjects, a wrongly-absorbed text file costs a silently missing
subject, which is the unsafe direction this whole document is about. It deserves
its own adversarial review rather than riding along with a constant edit.

**Interim:** the verified engine binaries were added to `ASSET_BINARY_EXT` by
hand. An inventory of likely-wrong entries for OTHER consumer classes -- a second
mainstream game engine (`.unity`, `.prefab`, `.asset`, `.meta`), a third
(`.res`, `.scn`, `.ctex`), DCC formats (`.fbx`, `.blend`, `.dds`, `.exr`), and
package formats (`.sqlite`, `.jar`, `.whl`, `.apk`) -- is recorded beside the
constant as REPORTED BUT UNVERIFIED, deliberately not shipped: nobody here can
check them against a real project of that type, and a wrong entry is a missed
subject.

## Part 3 -- The promotion run (2026-08-24), P9-P13

One root was taken end to end through promotion: 125 candidates over 36 subjects
-> 89 accepted by a first filter -> 8 survived an adversarial re-verify. A design
critique was written from that trail, and then a **recall audit** re-judged all
89 promoted candidates blind against `coverage-standards.md` verbatim -- 81
prior kills plus the 8 prior survivors as blind controls, in four independent
lanes, ids reshuffled by content hash and prior verdicts withheld.

The audit overturned the critique's central numbers and inverted its defect
ranking. Both are recorded below in the entries they bear on.

### The retired figure

**The 6.4 percent end-to-end yield is RETIRED. Do not reason from it.** It
measured the lane composed with an improvised promotion gate that enforced a rule
no shipped standards document contains, so it was never the lane's precision.

A criteria-only gate admits 47 of the 81 kills (58.0 percent). Implied yield on
the 89 promoted candidates rises from 8/89 (9.0 percent) to 52/89 (58.4 percent);
end-to-end on the 125 proposed, **from 6.4 percent to ~41.6 percent (52/125)**.

Nothing in P1-P8 or in Part 1 reasons from 6.4 percent -- that figure post-dates
them and belongs to a different, much smaller root -- so no earlier text in this
document needed correcting. The Part 1 figures (71 percent junk, 29 percent
survival) come from the pre-lane corpus run and are unaffected.

The audit also measured, per prior kill reason, how often a criteria-only gate
overturns:

| Prior kill reason | Overturn rate |
|---|---|
| the gate's invented check C | 19/25 = 76 percent |
| the gate's check D (over-applied CV-1) | 12/18 = 67 percent |
| universal-quantifier falsity (A) | 7/21 = 33 percent |
| ordering falsity (B) | 2/4 = 50 percent |
| `absent-fact-earns-ambient-cost` | 5/9 |
| `hazard-durability` | 2/4 |

The invented and over-applied classes collapse; the truth-checking classes hold.
The blind controls behaved as expected: 5 of 8 prior survivors upheld, 3
overturned, consistent with known flip-flop variance -- the gate is noisy in both
directions, which is itself an argument for P9.

**Method note, the reusable part.** The load-bearing design choice in the audit
was requiring every verdict to carry a VERBATIM QUOTE from the criteria document
for the rule it applied. That one field is what made shipped criteria
mechanically separable from invented ones after the fact, and it is why the
invented check was measurable rather than merely suspected.

**Interpretation caveat, stated rather than absorbed.** A harness defect stripped
the candidates' `anchors` and `why` fields from the file the lanes were briefed
with, so all four lanes judged against source firsthand rather than against the
record. This run therefore measures FACT admissibility, not RECORD admissibility.
That is the more informative measurement for the recall-audit question, but it
was not what the brief specified.

### Second root -- replication evidence (2026-08-24)

A second root was taken end to end under the shipped lane (skills-kit 0.62.0):
81 subjects of a large C++ game corpus, chosen as the first 81 in the corpus's
own enumeration order to scale-match the first root's 81 subjects. Zero agent
errors across the run.

- 289 candidates proposed over 81 subjects.
- The refutation stage (P9) returned 287 verdict rows: 238 STANDS, 49
  FALSIFIED.
- All 49 kills carry a `file:line` counterexample. Zero of 287 rows carry a
  criteria quote, which is CORRECT and not a defect -- the schema requires a
  quote only for a value judgment, and a pure falsification needs none. That
  zero is positive evidence that the truth-only scoping (see P9's "Its scope
  is TRUTH ONLY") held across all 57 fresh-context refutation agents this run
  dispatched.
- An independent re-check classified 48 of the 49 kills as SOUND (the
  counterexample really does contradict the fact as written) and 1 as
  UNSOUND.
- Kill shapes: 33 universal-quantifier claims, 7 exclusivity claims, 1
  ordering claim, 8 other.

**Headline comparison.** Truth-failure rate as a share of candidates
proposed: first root 17.6 percent, second root 16.6 percent -- essentially
identical across a skills-and-tooling tree and a C++ game corpus, roughly one
candidate in six false as written. Truth-scoped refutation also held up under
re-checking at 98 percent here (48/49) against the 67 percent the first
root's upheld kills managed (Part 3 above, the criteria-only-gate overturn
table).

**What this run corrects.** The refutation brief's own framing names
ordering/sequencing claims as one of its three primary falsification shapes.
This run produced exactly ONE ordering kill out of 49. Over-quantification --
universal-quantifier plus exclusivity claims, 40 of 49, 82 percent -- is
overwhelmingly the real shape on this evidence. This is a correction to the
brief's EMPHASIS, not to its correctness: ordering claims are still a valid
falsification shape, they are simply rare next to over-quantification.

**What this run does not settle.** It replicates the truth-failure base rate
and the refutation stage's soundness on a second, structurally different
root. It does not compare end-to-end yield (see the Known Limit section
above -- the two roots passed through different numbers of gates) and it does
not touch P10's drift finding, which is specific to the improvised gate that
predates skills-kit 0.62.0 and has no counterpart to measure on a run that
only ever exercised the shipped gate.

### P9 -- A refutation stage: the lane had a generator and no verifier (SHIPPED, skills-kit 0.62.0)

**Problem:** every MECHANICAL property was enforced by `workflow/coverage-detect.js`
after the agent returned, while every SEMANTIC criterion in
`coverage-standards.md` was enforced only by the proposing agent judging
candidates it had just written, in the same context. The "verification pass" at
advanced depth was three lines of the same batch prompt the proposer runs, and
that clause itself asserted "At this depth COVERAGE-ASSESSED means verified
absent". The standards document stamped that output "verified absent".

**Measured scope, narrower than the critique claimed.** The critique attributed
43 of the 81 kills to this. That number was contaminated by the improvised gate's
own invented rules. The clean number is **22**: of the 33 kills a criteria-only
gate upholds, 22 (67 percent) carry a `file:line` counterexample -- the fact is
FALSE as written and falsifiable from the subject's own direct files.

The audit also re-scoped the fix in KIND, not only in size. Refutation applied to
ADMISSION judgment is precisely what over-rejected (the 76 percent and 67 percent
classes); applied to TRUTH it held (33 percent and 50 percent).

**Shipped:** a refutation stage in `coverage-detect.js`. One fresh-context
dispatch per subject holding candidates, after reconciliation, carrying that
subject's exhaustive direct-file list plus the criteria path, pinned to the top
model rung. **Its scope is TRUTH ONLY**, which is the recall audit's finding
turned into code. Two refusals are pinned by tests: a FALSIFIED verdict with no
counterexample is discarded and the candidate kept; a subject whose verification
never returned keeps its candidates with a note to treat them as depth basic. The
verdict is re-derived with the reducer's own expression so GAPS-FOUND-iff-
candidates survives a deletion. New totals: `verifyRan`, `verified`, `falsified`,
`verifyUnreturned`, `verifyUnsupported`, `verifyPartialReads`, and the summary
line says explicitly when verification did NOT run.

**Verified by a live dispatch, not by re-reading the diff:** one Workflow
dispatch of `coverage-detect.js` at advanced depth over a real 3-file directory
returned `verifyRan: true, verified: 4, falsified: 0, verifyUnreturned: 0,
verifyUnsupported: 0`.

**Defect found in the first commit of this very fix**, by the agent writing the
lane doc: the verify brief demanded a verbatim quote from a document it never
named -- a rule a verifier could satisfy only by invention, which is precisely
what the field exists to detect. Fixed in a second commit and pinned by a test.
Keep it as the example of why the doc writer reads the code rather than the
brief.

**Open falsification test:** A/B the refutation stage against the old in-prompt
pass over the same new subjects. If it does not catch universal-quantifier and
ordering falsities at materially higher rates, P9 was wrong and the failure is
model capability rather than architecture -- in which case the stage should be
reconsidered rather than tuned. This has not been run; the second-root
replication above is a different test (base-rate consistency, not an A/B
against the old in-prompt pass) and leaves this one open.

**Partial answer to the single-root caveat:** the "Second root -- replication
evidence" subsection above found the same ~17 percent truth-failure rate on a
structurally different corpus, which is evidence that P9's problem statement
-- candidates that are false as written, not merely ambient or hazard calls --
is not an artifact of the first root. See the Known Limit section for what
that replication does and does not establish.

### P10 -- Ship the promotion gate: an un-shipped filter drifts, invisibly (SHIPPED, skills-kit 0.62.0)

This is the most reusable finding in this document.

**Problem:** the plugin shipped no promotion gate, so every corpus-scale consumer
had to improvise one per run -- and operators had improvised one twice. An
improvised gate is not merely undocumented; it is **unauditable**, and this one
had DRIFTED STRICTER THAN ITS OWN CRITERIA with nobody able to see it.

The measurement is mechanical, taken from the raw verdict files with no model
calls. The `criterion` field on the 81 kills recorded 25 as `C`, 21 `A`, 18 `D`,
4 `B` -- letters from the improvised brief -- against only 13 naming a shipped
criterion. **68 of 81 kills (84 percent) were decided under the gate's own
vocabulary rather than the plugin's criteria.**

The worst of them was an invented check: "fails the evidence floor EVEN WHEN IT
IS TRUE". That string appears verbatim in the improvised gate's brief and in no
shipped standards document. It caused **25 kills, of which 19 (76 percent) a
criteria-only gate overturns**. A second check over-applied CV-1's single-site
rule for another 12.

**One un-shipped filter caused more wrong kills than any property of the lane.**
That inverted the defect ranking: this is the LARGEST measured defect, not the
smallest.

**The lesson, and it is the third instance of it.** P1 was criteria paraphrased
into a brief. P2 was a file filter hand-rolled beside a shipped discovery script.
This is the same shape a third time: **a guardrail that lives outside the
software does not hold.** It is not merely that the improvised artifact is worse
than the shipped one -- it is that its divergence is invisible, because there is
nothing to diff it against. Whatever the mechanism is, if consumers must build it
per run, they are building a different one each time and no one can measure how
far it has drifted.

**Shipped:** `coverage-lane.md` now names the four-step promotion gate every
corpus-scale consumer runs, plus a step documenting the refutation stage, plus
the verbatim-quote guardrail as the thing that makes drift DETECTABLE -- the
audit's own load-bearing method, turned into a shipped requirement.

**Single-root caveat, unresolved for this entry specifically.** This finding
is a measurement of an improvised, un-shipped gate that predates skills-kit
0.62.0; the second-root run (see "Second root -- replication evidence" in
Part 3) only ever exercised the shipped gate, so it has no drift to measure
and does not extend P10's evidence to a second root. See the Known Limit
section above.

### P11 -- The honesty pass: nine sites claimed verifier-grade semantics (SHIPPED, skills-kit 0.62.0)

**Problem:** the pipeline did not deliver verifier-grade output (P9), yet
documentation asserted that it did. The critique named two such sites. An
exhaustive sweep found **nine**.

**Shipped:** all nine corrected. `coverage-standards.md`'s depth section now
states that "verified absent" names the refutation stage, that the stage can be
ABSENT, and that it deletes only for falsity on a named counterexample. `SKILL.md`'s
`--advanced` description was corrected. md-domain's own `CLAUDE.md` received an
AMENDMENT rather than a silent edit, because its claim was true of the DESIGN and
false of the CODE, and the gap between those is the thing worth recording.

The general rule this discharges: a document may not stamp an output with a
property the pipeline does not enforce. When a claim is true of the design and
false of the code, amend rather than overwrite -- the discrepancy is the finding.

### P12 -- CV-1's closed escape list misses the invoker and the output reader (SHIPPED, skills-kit 0.62.0)

**Problem:** CV-1's single-site clause carried a CLOSED four-item escape list. It
does not cover a fact whose harmed reader is a tool INVOKER or an OUTPUT READER
rather than a code reader -- someone who runs the code or consumes what it prints
and never opens the site where a comment would live.

This surfaced UNPROMPTED and INDEPENDENTLY in three of the four audit lanes. All
three named it as their largest judgment lever, and the run's only UNSETTLED
verdict sits on it. Three independent lanes converging on the same unlisted case
is the strongest signal in the run that the list, not the judges, was wrong.

**Shipped:** the closed list is restated as the GOVERNING QUESTION -- "does a
comment here reach the reader who would make the mistake?" -- with its known
cases attached, including the new one. A judge must name which case holds.

**Confirmed live:** on the first real dispatch after the change, one candidate
was admitted specifically by the invoker/output-reader clause, and the old closed
list would have rejected it.

### P13 -- A typed counterpart channel: REFUTED ON EVIDENCE, deliberately not built

This entry records a proposal that was designed, measured, and abandoned. It is
not pending work.

**The proposal was:** let a candidate cite evidence OUTSIDE its subject in a
typed field beside the anchors, on the argument that anchor membership conflates
proof-of-isolation with epistemic support, making CV-1's flagship fact class
uncitable. The critique attributed 25 of the 81 kills to this.

**The measurement refuted it.**

- Of the 6 invented-check kills a criteria-only gate still upholds, **none was
  upheld because the evidence lies outside the subject** -- four are plain
  falsity, two are ambient-cost calls. **Zero of the upheld kills failed for the
  reason this channel exists to fix.**
- Mechanically, **all 81 kills carried anchors entirely INSIDE the subject**;
  zero had an outside anchor. The membership check drops out-of-list anchors
  before they reach the record, so the class was never "candidates citing
  elsewhere".
- The criteria-only gate admitted outside-counterpart facts on in-list anchors
  without difficulty.
- The 25-kill class the critique attributed to this was, on inspection, 19
  wrong-by-invented-rule (see P10) plus 6 killed for unrelated reasons.

**What remains true:** CV-1 does admit a fact whose counterpart lives elsewhere
while anchors must be in-list, so the tension is real IN PRINCIPLE. What is now
false is that it costs anything measurable. Its measured cost on this corpus is
zero.

**Recommendation: do not build it.** Rebuilding needs NEW evidence, not the
original argument. If revisited, note that this is a criteria change to a
published contract and is gated on the owner regardless; the cheap alternative is
a wording clarification in CV-7 stating how such a fact should be anchored, with
no schema change and no new field.

Recording a refuted proposal here rather than deleting it is deliberate. The
argument is good enough to be re-derived by the next reader, and the only thing
that stops that is the measurement.

## Part 4 -- What is NOT proposed

- **No change to the ADMISSION criteria's content.** `coverage-standards.md`'s
  admission criteria were correct on every one of the 90 downgrades in Part 1,
  and the recall audit found the criteria-only gate to be the more accurate
  judge in every collapsing class. P12 changed how ONE clause is expressed, not
  what it requires.
- **No change to the per-directory subject.** The non-recursive subject is
  well-argued in `coverage-standards.md` ("Why not a subtree") and nothing here
  contradicts it. Nothing in the kill list is caused by the unit. P3 is about
  affordability at scale, not about changing the unit.
- **No relaxation of the report-only boundary** on analyze.
- **No loosening of anchor MEMBERSHIP.** It is the isolation spine and it worked
  (zero isolation violations across the run). P13 would have added a channel
  BESIDE it; it is not being built, and membership is not being weakened either
  way.
- **No tightening of the generator to raise precision.** The generous generator
  posture LOOKS like the defect and is load-bearing: several kills were TRUE
  facts with defective citations, and recall requires generosity. The fix is a
  verifier (P9), not a shyer proposer.
- **No undoing of the ambient chain (CV-2).** The strongest empirical win
  available: the repo-wide kill class went from 41-of-90 dominant in the Part 1
  pre-lane run to near-zero in the promotion run.

## Part 5 -- Evidence index

None of the underlying artifacts are in this repo, and none should be: they were
produced inside client project scratch trees and are unsanitized. This index
describes WHAT each artifact is, so a future reader knows what would have to be
reproduced.

From the 2026-08-20 corpus run (a C++ game corpus; artifacts in that run's
scratch tree, excluded from its version control and regenerable):

- The banding table -- 536 code-tier directories with band, reason, evidence, and
  changelist churn.
- The cross-check verdict table -- the 126 promotions with the downgrade rule
  applied to each.
- Two criteria briefs -- the loose paraphrased one (failure 1) and the tightened
  one that stated what the evidence must prove (the A/B change).
- The data-tier result table -- 84 directories, 0 promotions.
- The extension-set metrics script and its output -- the narrow hand-rolled set
  against the correct shared one.
- The Phase 0 writeup, including the filter-leak list.
- The exclusion-miss audit behind P5 -- 32 of 899 directories wrongly excluded by
  a hand-rolled regex.

From the 2026-08-24 promotion run and recall audit (a skills and tooling root;
artifacts in that run's scratch tree beside the original run):

- The 125 proposed candidates, the 89 accepted, and the 8 promotable.
- Four per-lane verdict files carrying the 81 verify-stage kills, plus the
  first-pass rejection table.
- The recall-audit harness -- build, score, classify, and anchor scripts; four
  lane briefs; four result files; and the blind key.
- The improvised gate's brief, which is where the invented check appears verbatim
  and is the primary evidence for P10.

The design critique written from the 2026-08-20 and 2026-08-24 trails was also
unsanitized, and it has been RETIRED rather than relocated: being unsanitized, it
could not move into this public repo, and a triage of all 297 lines found nothing
in it that is both unique and still true. Its headline figures were superseded by
Part 3; its defect arguments are carried by P9-P13; its "what I would NOT change"
list is carried by Part 4 and by the as-built documents those items now live in --
`coverage-standards.md` (non-recursive subject, the ambient chain, the generous
generator posture), `coverage-lane.md` (the promotion gate, batch reconciliation),
and the anchor-membership mechanism in `coverage-detect.js`. Do not cite the
critique as the argument of record; it no longer exists.
