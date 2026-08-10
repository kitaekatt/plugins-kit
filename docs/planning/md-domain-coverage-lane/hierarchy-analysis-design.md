# Hierarchy analysis: the right shape for tree-scale CLAUDE.md work

Status: design proposal, 2026-08-10. Nothing here is implemented.
Evidence base: the woodworking-sim coverage campaign
(`dev/tasks/md-domain-review-enablement/reports/`, gitignored -- LEDGER.md,
MERGE-src.md, BRIEF.md), read directly, not summarized.

## The recommendation

**The unit changes, not the verb -- and the two candidate analyses are ONE
analysis.** Add a single report-only lane, `hierarchy_claude_md_tree`, whose
subject is a composition this design registers: `claude_md_tree` -- a named
directory root plus every CLAUDE.md governing files beneath it, plus any
persisted coverage candidate sets targeting it. Its input is the union of
WRITTEN facts (extracted from the tree's existing CLAUDE.md files) and
PROPOSED facts (candidates from per-subtree coverage reports). Its output is a
placement resolution: per-destination merged fact sets, a per-source
subtraction/removal list, and re-judged per-leaf dispositions -- the shape
MERGE-src.md has by hand today. It is invoked standalone (the "opted into"
half of the owner's sentence) and is the IMPLIED first phase whenever
`generate claude-md` is dispatched at tree scale (the "implied when all
CLAUDE.md files are regenerated" half) -- the same phase relationship coverage
already holds to single-document generation. It is not a fourth peer verb; it
is the resolution phase of generation at the tree unit, exactly as coverage is
the discovery phase of generation at the subtree unit.

## Why (a) and (b) are one analysis, not two

The task framing offered two readings of the owner's sentence:

- (a) MERGE/PROMOTION RESOLUTION -- input is N per-subtree candidate sets;
  output is per-destination merged facts plus per-leaf subtractions. Runs
  before generation.
- (b) CHAIN DE-DUPLICATION AUDIT -- input is the existing CLAUDE.md files in
  a tree; output is facts duplicated across ancestor/descendant or sitting at
  the wrong depth. Runs after generation, or standalone.

The decisive evidence is that the campaign's hand-built artifact is neither
pure form. MERGE-src.md sections 1-2 are analysis (a) over 38 promoted
candidates -- but section 3 is analysis (b) over an EXISTING document: it
lifts two whole sections out of `src/kernel/CLAUDE.md` (the THREE-facing
wrappers rule and the lathe `guideX` rule) and rewrites a third, using exactly
the same depth test it applies to candidates ("a fact whose violators work
outside src/kernel cannot justify staying"). The merge plan could not have
been built without both inputs, because a real tree mid-campaign is MIXED:
some documents exist (`.`, `src/kernel`), most destinations do not. A pure
(a) that ignores written documents re-creates duplication against
`src/kernel/CLAUDE.md`; a pure (b) cannot run before anything is written,
which is when the campaign needs it most.

The unification is clean because a written fact IS a candidate whose current
location is its proposed destination. The analysis over both is identical:
given a set of (fact, location) pairs across one tree, apply the placement
spine (`references/cohesion-principles.md` -- CCP change cadence, CRP reader
set, ADP load order) to select exactly one home per fact, and report every
move, merge, subtraction, and rejection that selection implies. Candidates
whose home is confirmed feed generation; written facts whose home changes
feed regeneration of their source and destination documents.

Two campaign findings confirm the analysis cannot live inside any per-subject
lane:

- **Sibling blindness is structural, not a bug** (finding 1). A sibling's
  CLAUDE.md is not ambient for a subtree, so per-subtree coverage CORRECTLY
  re-reports a shared fact once per sibling -- 38 raw promotions to `src`
  collapsed to 32 entries over 6 duplicate pairs, and two genuine placement
  disagreements (M17, M23) arose from two assessors each reasoning correctly
  from what each could see. Only a pass whose subject is the tree can see
  both reporters at once.
- **Depth is invisible per-leaf** (finding 2). The lane emits no
  leaf-only-vs-promote judgment; the campaign added a caller-side `scope:`
  line judged by reading a parent or sibling -- a read outside the lane's
  subject. The judgment belongs to a lane whose subject contains the parent.

## Where it lives in the dispatch table

In `references/audit-framework.md` vocabulary:

- **Subject**: a composition. Register `claude_md_tree` in
  `audit-framework.yaml::compositions`: marker = a caller-named directory
  root (no whole-repo default, same posture as coverage); contains =
  `claude_md` primitives discovered beneath the root, plus (optionally)
  persisted coverage reports supplied by the caller. The framework already
  says "a subject is either a primitive or a composition", so a
  composition-unit lane needs no framework-level change for the subject
  itself.
- **What the framework CANNOT express today, honestly**: a lane whose input
  includes another lane's PERSISTED OUTPUT. Coverage reports are neither a
  registered primitive nor part of any composition. The minimal extension is
  one of: (i) the `claude_md_tree` composition entry declares an optional
  `candidate_reports` constituent, or (ii) lane records gain an `inputs:`
  field naming consumable report kinds. Option (i) is smaller and keeps the
  registry the single place compositions are described; this design proposes
  (i). This is a registry entry plus a lane-record field, not a framework
  refactor -- the framework's "open under addition" principle covers it.

Dispatch table addition (one row):

| Verb x subject | Lane id | Procedure | Standards doc |
|---|---|---|---|
| hierarchy (claude_md_tree) | `hierarchy_claude_md_tree` | `references/lanes/hierarchy-lane.md` | `references/standards/hierarchy-standards.md` |

On "is this a fourth verb": the dispatch table gains a row, but SKILL.md
already states "the three are dispatch entries, not three things of the same
kind" -- coverage is listed as a verb while being defined as the discovery
phase OF generation. Hierarchy is the same kind of thing one step later: the
RESOLUTION phase of generation, invocable standalone. The verb-shaped token
`hierarchy` exists for dispatch ergonomics; the design content is that
`generate x claude-md` gains a tree unit, served as a CHAIN:

    coverage (per leaf, N runs)  ->  hierarchy (tree, 1 run)  ->  generate (per destination, M runs)

with the user's decision between each arrow, mirroring the existing
"coverage then generation is a CHAIN, not a composite verb" rule verbatim. A
request to "regenerate all the CLAUDE.md files under src" routes to this
chain, announcing each phase per the naming-and-scope rule; the hierarchy
phase is implied there and opted into everywhere else. The generation lane's
single-invocation contract ("generating N files is N runs") is untouched --
tree scale lives in dispatch routing, not in the generation procedure.

Two existing-lane boundaries this placement respects:

- **Not `coverage`.** Coverage's CV-8 is fail-severity: "coverage judges
  absent facts only; it never evaluates content already present." Hierarchy's
  (b) face judges present facts' PLACEMENT. Folding it into coverage breaks
  coverage's own hardest boundary. (Hierarchy judges placement only, never
  fidelity or value of present content -- see refusals below.)
- **Not `audit claude-md` with a bigger selector.** The audit lanes'
  criteria are per-document; hierarchy's criteria are RELATIONS between
  documents and cannot be evaluated on any single file. And an audit does not
  consume proposals -- the (a) face has no artifact to render a verdict on.
  The mixed-input case the evidence demands fits neither audit machinery
  (DETECT -> Q&A -> REMEDIATE over an existing subject) nor its verdict
  vocabulary.

New standards doc, `hierarchy-standards.md`, criteria sketch (ids final at
authoring time; each id rides the existing `configuring-standards.md`
disable/tune mechanism automatically):

- HR-1 one-home-per-fact: a fact appears exactly once across the chain
  (fail).
- HR-2 shallowest-true-depth: a fact lives at the shallowest directory where
  it is true of everything below it, and no shallower (judgment). This is
  CCP/CRP/ADP applied across a tree, deferring to cohesion-principles; it is
  not a re-derivation.
- HR-3 precedent-outranks-hoisting: where the tree already places a
  fact-class by an observable convention, follow it; for mutual-sync
  ("these two files change together") facts with a single mirrored
  directory, the default is the mirrored directory, not the common ancestor
  (judgment). See the razor section for why this is detection, not config.
- HR-4 input-inventory-complete: the affirmative verdict may be emitted only
  when every enumerated leaf maps to a candidate report, an explicit
  assessed-null, or a written document -- never over a silent absence (fail).
- HR-5 disposition-re-judged: every leaf whose candidate set was reduced by
  subtraction gets its disposition re-judged from the post-subtraction
  count; flips run WARRANTED -> NOT only, never the reverse (judgment).
- HR-6 merge-preserves-precision: when duplicate reporters are collapsed,
  the narrower, verified statement wins, and precision constraints recorded
  by a reporter ("do not restate as X") survive the merge (judgment).
- HR-7 unplaceable-declared: a fact CV-3 admits no destination for (its
  trigger lives in a sibling subtree) is reported UNPLACEABLE with the
  reason, never forced to the root or silently dropped (fail).

Verdicts: `CHAIN-COHERENT` (inventory complete, no moves/merges/subtractions
proposed) / `RESOLUTION-PROPOSED` (inventory complete, plan non-empty).
When HR-4 is unsatisfied the lane emits NO verdict -- it reports
`INPUTS-INCOMPLETE` with the per-leaf inventory table and stops. Neither
verdict is COMPLIANT/NON-COMPLIANT, and neither alters any document verdict,
same posture as coverage.

## Input contract

Three inputs, all caller-named, no defaults that widen scope:

1. **The tree root** (required). The lane enumerates code directories and
   CLAUDE.md files beneath it itself, reusing the coverage discovery walk --
   the enumeration is the lane's job precisely so HR-4 has an authoritative
   leaf list that did not pass through the caller's hands. (The campaign's
   root CLAUDE.md omitted 7 of 37 directories from its own structure map;
   caller-supplied inventories are exactly that unreliable.)
2. **Candidate reports** (optional; their absence selects the pure chain-audit
   face). One file per assessed subtree, the coverage lane's `--json` report
   persisted to a caller-chosen directory and passed as a path.
3. **The existing CLAUDE.md files** in the tree (discovered, not passed).
   Read for three purposes only: suppression (a candidate already carried by
   a resolving written fact), lift-out (a written fact whose depth test
   fails, MERGE-src section 3), and precedent detection (HR-3).

**On persistence -- this design answers a KNOWN OPEN QUESTION and says so.**
"Which representation persists a reported candidate" is open in
`dev/tasks/md-domain-review-enablement/plan.md` (Working notes). The answer
proposed here: the persistence unit is the coverage report itself, in its
existing `--json` form, one file per subtree, location caller-chosen (a task
folder, a `reports/` directory -- the lane takes a path and imposes no
layout). No new artifact kind, no registry, no state directory: the report
the lane already emits IS the durable representation, which also means the
campaign's existing per-leaf reports are (after the schema addition below) the
lane's input format. If the project later settles the question differently,
this lane consumes whatever that settlement names; the coupling is one
loader.

**One coverage schema change rides along**: the candidate record gains the two
caller-added fields the campaign proved load-bearing (BRIEF.md): `scope`
(`LEAF-ONLY` | `PROMOTE -> <dir>`, judged by reading the parent or a sibling)
and `sibling_overlap` (present only where a sibling document states the
fact, naming the section and whether it reaches this subtree's author).
Without `scope` the resolution has no depth input -- the campaign could not
have built MERGE-src.md without it, and the hand-rolled brief is the wrong
place for a field a lane depends on. This is a coverage-lane and
coverage-standards change, separately shippable, and it needs a version bump
and publish like any manifest-visible change.

## Report-only, and why

**Report-only holds here as a hard property of the entry point, same as
coverage.** The lane binds no remediate workflow and never writes, whoever
calls it and whatever the chain does next. Three reasons, each of which
breaks concretely if the property is dropped:

1. **The plan spans lanes with an ordering constraint.** Executing it means M
   generation runs plus subtractions from existing documents, and the safe
   order is write-destination-before-subtract-source (the same
   no-partial-rename discipline the task CLAUDE.md records: a fact deleted
   from `src/kernel/CLAUDE.md` before `src/CLAUDE.md` exists is a fact that
   exists nowhere, and nothing greps for an absence). A lane that remediates
   inline either half-executes the plan or grows multi-document orchestration
   that generation's single-invocation contract deliberately excludes.
2. **The plan is a decision point, not a formality.** The dispatch table's
   own rationale for refusing a `coverage+generate` composite applies
   verbatim: a verb that resolved and wrote in one motion makes the report a
   formality. MERGE-src.md contains five rejections and one demotion -- real
   editorial judgments a user must be able to overrule per item, not per
   run.
3. **Disposition re-judgment is a judgment call the user owns.** "A whole
   CLAUDE.md for one fact is a judgment call, not an automatic yes"
   (LEDGER.md). Auto-remediation would automate past exactly that call, on
   the least stable quantity the campaign measured.

## What it must refuse to do (the fake-gate rule applied)

The false pass here has a precise shape, because the campaign hit its
sibling: `discover_coverage.py` returned `codeFiles: []` for GDScript
subtrees and the lane would have emitted `COVERAGE-ASSESSED` -- "verified
absent" -- over files it never read. Seven of 37 directories would have
satisfied the done-condition without being read. The hierarchy lane's
equivalents, and what structurally prevents each:

- **"Resolution complete" over a missing leaf.** A tree resolution handed 10
  of 18 leaf reports must not treat the other 8 as empty candidate sets --
  absence of a report is absence of evidence, not evidence of absence.
  Prevention: HR-4 is fail-severity and the verdict line is COMPUTED from
  the per-leaf inventory table (every enumerated leaf -> report | explicit
  assessed-null | written-doc | MISSING); any MISSING row makes the
  affirmative verdicts unemittable, the same way NOT-AUDITED is counted
  apart from DIFF-CLEAN in review mode.
- **"Chain clean" over a document it could not extract facts from.** In the
  pure (b) face, a CLAUDE.md the lane failed to parse or chose not to read
  must appear in the report as UNEXTRACTED, and bars `CHAIN-COHERENT`.
  Prevention: the report carries a per-document extraction inventory; the
  verdict is computed from it.
- **An affirmative verdict over zero input.** A tree with no CLAUDE.mds and
  no reports has nothing to resolve; the lane says so and stops, mirroring
  the fix the discovery defect demands (refuse the strong verdict over a
  zero-file read) rather than repeating the bug one level up.
- **Re-auditing present content.** Hierarchy judges the PLACEMENT of a
  written fact, never its fidelity or value -- those are CD-1..CD-6
  (`claude-md-standards.md` section 3). The campaign's worked instance:
  MERGE-src rejected a candidate as "CD-lane work misfiled as coverage"
  (a correction of a stale root claim routed to the root rewrite). The lane
  must make that same routing, not absorb the work.
- **Reading code as discovery.** Facts enter from reports and documents
  only. The lane opens source at most to check a MERGED restatement against
  its carried anchors -- the six-instances finding (a candidate true as
  cited, false as restated) shows merging two reporters' phrasings is
  exactly where over-broadening happens. Even that check is bounded to
  carried `file:line` anchors and is deferred out of the first slice (see
  below); unrestricted code reading would re-run coverage inside a lane that
  is not coverage.
- **Inventing a criterion for sibling reach.** HR-7 declares UNPLACEABLE; it
  does not resolve it. See limits.

## The plugin-opinion razor, applied per opinion

The lane's structural advantage: it is report-only, so every judgment it
hardcodes surfaces as a per-item proposal the user can decline -- the
remedial action against a disagreeable default is "don't take that item",
not uninstallation. Additionally, every HR criterion carries a stable id and
rides the EXISTING `configuring-standards.md` disable/tune mechanism -- a
seam that already ships, at zero cost to this design. Opinions examined:

1. **Mirrored-directory over common-ancestor for mutual-sync facts (HR-3).**
   The test arguably passes -- a team could prefer ancestor-hoisting of sync
   rules. But the razor's own sharpening (awesome-kit 0.26.0) applies:
   prefer DETECTING an observable fact over a config key, and the repo's
   precedent IS observable in its existing documents (two campaign assessors
   cited `src/kernel/CLAUDE.md`'s placement independently as the house
   convention). So HR-3 is written as precedent-detection with
   mirrored-directory as the no-precedent default, the default is
   disable-able by id through the existing mechanism, and no config key is
   added.
2. **Write-destination-before-subtract-source ordering.** Razor test FAILS:
   no scenario was found in which a power user wants a fact deleted from its
   only home before its replacement exists. Hardcoded, no seam.
3. **Downward-only disposition flips (HR-5).** Razor test FAILS: subtraction
   only removes content from a leaf, so an upward flip would assert facts
   the analysis never added. This is arithmetic, not preference. Hardcoded.
4. **The implied hierarchy phase on tree-scale regeneration.** A user asking
   to regenerate N files for a reason with no hierarchy stake (a format
   migration) is real. The seam is interactional, not configuration: the
   chain announces every phase by name before running (SKILL.md's
   naming-and-scope rule already requires this), and the user declines the
   phase in conversation. No config key.
5. **Report persistence location.** Caller-chosen path; no opinion held.
6. **No whole-repo default; named root required.** Inherited from coverage's
   registered posture, not a fresh opinion.

Net: no new config keys; one opinion (HR-3) shaped as detection with a
disable-able default; two hardcoded on failed tests; one interactional seam.

## Honest limits -- what this design does NOT solve

- **Finding 3 (CV-3 and sibling reach) is not solved, and this design does
  not touch it.** A fact whose trigger is an edit in a sibling subtree still
  has no admissible destination; the hierarchy lane inherits CV-3 and merely
  refuses to paper over it (HR-7 reports UNPLACEABLE rather than forcing the
  root). The corpus-wide wrong-direction documentation pattern therefore
  remains unexpressible. The deferred items `cv3-cannot-reach-a-sibling-
  subtree` and `cv-reachability-not-ambience` own that question and say to
  settle it together or not at all; this design deliberately does not
  preempt them.
- **Finding 4 (disposition instability) is mitigated in sequencing only.**
  HR-5 re-judges disposition at the correct moment (post-subtraction) and
  the report states how thin a verdict is ("holds by one fact",
  MERGE-src's `src/ui` note). But the leaf-only tail's run-to-run
  instability is a property of coverage assessment (one subtree went 3 -> 0
  across identical runs); a disposition resting on one pass is still
  one-pass evidence, and this lane adds no second pass.
- **The plan is a sample of samples.** Coverage reports are non-exhaustive
  and non-idempotent by declared contract; a resolution over them inherits
  both. The lane must not imply the merged fact set is the tree's fact
  inventory.
- **Fact extraction from written prose is judgment**, as is same-fact
  identification across two reporters' restatements. No idempotency is
  claimed, matching coverage's posture.
- **The discovery-extension defect (`CODE_DATA_EXT`) is a separate,
  already-filed item** (`coverage-discovery-misses-extensions`). This lane
  depends on its fix only insofar as garbage-in reports remain garbage; it
  does not fix it and must not be reported as doing so.
- **Merged-restatement verification is deferred**, and intersects the
  deferred `generation-carries-unverified-rules` finding (true-as-cited vs
  true-as-restated). Coverage's advanced verify pass is the existing prior
  art (six measured catches); wiring the same check into merge output should
  be designed together with that item, not smuggled in here.
- **Cost at tree scale is real.** The pure (b) face over a large written
  tree reads every CLAUDE.md completely; the (a) face reads reports plus
  documents. Neither reads source. There is no whole-repo default, and the
  intent gate confirms scope, same as coverage.

## First implementable slice

The smallest thing that lets the woodworking-sim campaign stop hand-rolling
merge plans:

**Slice 1: the (a)-face resolution over persisted reports.**

- Input: a tree root, plus a directory of per-subtree coverage reports
  (JSON, with the `scope` and `sibling_overlap` candidate fields -- the
  campaign's existing reports carry these as caller additions and are
  convertible).
- The lane enumerates leaves under the root, builds the HR-4 inventory
  table, reads the tree's existing CLAUDE.mds for suppression / lift-out /
  precedent, and emits: per-destination merged fact sets (duplicates
  collapsed per HR-6, rejections and demotions stated with reasons), the
  per-leaf subtraction table, lift-out/rewrite items for existing documents,
  re-judged dispositions per HR-5, UNPLACEABLE items per HR-7, and the
  verdict (or `INPUTS-INCOMPLETE`).
- Ships with: the `hierarchy-standards.md` criteria, the lane doc, the
  `claude_md_tree` composition registry entry, and the coverage candidate
  schema addition (`scope`, `sibling_overlap`). All are skills-kit changes
  needing a version bump and a publish -- the owner's call, and until
  published the campaign continues hand-rolling (per
  never_hand_make_a_plugins_output, the plan artifact for the remaining
  destinations is not evidence the lane works).

What slice 1 does NOT yet cover:

- The pure (b) face (fact extraction from written prose with no reports) --
  the hardest judgment component, and the one with no campaign-scale
  worked example yet beyond MERGE-src section 3.
- The implied-phase wiring of `generate claude-md` at tree scale (the chain
  routing in SKILL.md); slice 1 is opt-in only.
- Merged-restatement verification against anchors (deferred, see limits).
- Any change to CV-3, sibling reach, or reachability (deferred items own
  those).
- Model/effort pinning decisions for the lane's workflow entry (to be
  settled at implementation against the coverage lane's "always enter the
  workflow" precedent).
