# The hierarchy lane

The hierarchy procedure. Like `coverage`, this one is NOT parameterized by
artifact -- it has exactly one subject shape, and its subject is a TREE: a named
directory root, the CLAUDE.md files governing it, and the persisted coverage
reports targeting subtrees under it.

**What kind of operation this is.** Hierarchy is not a peer of `audit` and not a
second coverage. It renders no compliance verdict, so it is not an audit; it
creates no document, so it is not generation either. It is the RESOLUTION step
between them: coverage discovers facts per subtree, hierarchy decides where each
one lives across the whole tree, and generation writes the destinations. The
chain runs

```
coverage (per leaf, N runs)  ->  hierarchy (tree, 1 run)  ->  generate (per destination, M runs)
```

with the user's decision between each arrow -- a CHAIN, not a composite verb,
exactly as `coverage -> generate` already is. REPORT-ONLY IS A PROPERTY OF THIS
ENTRY POINT: the lane binds no remediate workflow and never writes, whoever
calls it and whatever the chain does next.

**Scope of this lane as shipped: the resolution over persisted reports.** It
reads the tree's existing CLAUDE.md files for suppression, lift-out, and
precedent, and it resolves the candidates those reports carry. A pure
chain-audit over a tree with no reports at all -- extracting facts from written
prose and de-duplicating them -- is the harder judgment problem and is NOT part
of this lane's contract; a run with no reports and no documents reports
`INPUTS-INCOMPLETE` rather than improvising one.

## Why this cannot live in an existing lane

- **Not `coverage`.** Coverage's `present-content-not-re-audited` is
  fail-severity: it judges absent facts only and never evaluates content already
  present. Hierarchy judges the PLACEMENT of present facts. Folding it into
  coverage breaks coverage's own hardest boundary.
- **Not an audit lane with a bigger selector.** The audit lanes' criteria are
  per-document. Every criterion here is a RELATION between documents, or between
  a proposal and a document, and cannot be evaluated on any single file. An
  audit also has no artifact to render a verdict on when the input is a set of
  proposals.

Two structural findings force the tree unit specifically:

- **Sibling blindness is correct behaviour, not a bug.** A sibling's CLAUDE.md
  is not ambient for a subtree, so per-subtree coverage rightly re-reports a
  shared fact once per sibling. Only a pass whose subject is the tree can see
  both reporters at once and collapse them.
- **Depth is invisible per leaf.** Whether a fact belongs at the leaf or at a
  parent cannot be judged from inside the leaf's own subject. The judgment
  belongs to a lane whose subject contains the parent -- this one.

## Subject and unit

**Unit: `(tree root, the CLAUDE.md files governing it, the candidate reports
targeting it)`.**

Registered as the `claude_md_tree` composition in
`references/audit-framework.yaml`. Its `candidate_reports` constituent is the
one thing the framework could not express before: a lane whose input includes
another lane's PERSISTED OUTPUT. It is declared as an optional constituent of
the composition rather than as a new lane-record field, so the registry stays
the single place a composition is described.

## Parameters

| Parameter | Value |
|---|---|
| lane id | `hierarchy_claude_md_tree` |
| discovery | `scripts/discover_hierarchy.py` |
| detect workflow | `workflow/hierarchy-detect.js` |
| remediate workflow | NONE -- report-only, deliberately |
| verdicts | `CHAIN-COHERENT` / `RESOLUTION-PROPOSED` (and `INPUTS-INCOMPLETE`, which is not a verdict) |
| standards | `references/standards/hierarchy-standards.md` |
| supported flags | `--reports <dir>`, `--json` |

## Model pinning (not negotiable)

The detect workflow pins `opus` + `high` effort, per
`plugins/skills-kit/CLAUDE.md`.

**The workflow is entered regardless of subject count**, for the same reason
coverage does it and more sharply: this lane has exactly ONE subject by
construction, so an inline single-subject shortcut would put every run off-pin,
not just the rare one. Always go through the workflow.

There is no remediate lane, so the `sonnet` + `low` remediation pin and
`scripts/gen_workflow_js.py` do not apply here at all.

## The pipeline

```
Step 1  Resolve + INTENT GATE
Step 2  Discover (mechanical, inventory-building)
Step 3  Resolve (hierarchy-standards.md)
Step 4  Report and stop
```

There is no Q&A gate and no remediation phase. Nothing is ever applied.

### Step 1 -- Resolve and gate on intent

1. This verb NEVER runs as part of an `audit`, `generate`, or `coverage`
   invocation, and never as a side effect of any other lane. As shipped it is
   opt-in only.
2. Resolve the target: a named directory root. **There is no whole-repo
   default** -- inherited from coverage's registered posture. If no root is
   given, say so and stop; do not pick one.
3. Resolve the reports directory. Its absence is not an error, but it selects a
   run with no candidates, which for a tree that also has no documents is
   reported `INPUTS-INCOMPLETE` rather than as a clean chain.
4. Announce the run by name and scope before Step 2, per SKILL.md's "Naming and
   scope announcement": the analysis name (`Hierarchy resolution`), the tree
   root, the leaf count, the document count, and the number of reports loaded.

### Step 2 -- Discover (mechanical)

Run `scripts/discover_hierarchy.py <root> [--reports <dir>] --json` and consume
the subject it returns: `leaves`, `claudeMdPaths`, `ambientAbove`, `reports`,
`inventory`, `unmatchedReports`, `candidateTotal`, `skipped`, `noisePruned`,
`notes`.

**The leaf enumeration is the lane's job, deliberately.** It does not pass
through the caller's hands, because the whole force of `input-inventory-complete`
comes from the leaf list being independent of the report set. A tree's own root
document routinely omits directories from its structure map, and a resolution
built on a caller-supplied list cannot notice what that list already forgot.

`inventory` carries one row per leaf with status `report` / `assessed-null` /
`written-doc` / `MISSING`. Surface it in the report in full; it is not a
diagnostic aside, it is the evidence the verdict is computed from.

`unmatchedReports` -- a report naming a root that matches no enumerated leaf --
has the same standing as a `MISSING` row: the reports and the tree disagree
about what exists, so the inventory cannot be trusted to be complete.

**The persisted report format.** One JSON file per assessed subtree, each
carrying a subtree `root` and a `candidates` list. A bare subject object, a list
of them, or an object holding them under `perSubject` / `subjects` are all
accepted. A subject whose `candidates` list is empty is an EXPLICIT
assessed-null -- materially different from no report at all, which is why the
two get different inventory statuses. The location is caller-chosen; this lane
takes a path and imposes no layout. The persistence unit is the coverage report
itself, in its existing `--json` form: no new artifact kind, no registry, no
state directory.

### Step 3 -- Resolve

Apply `references/standards/hierarchy-standards.md` verbatim. The caller MUST
resolve that document to an ABSOLUTE path and pass it as `refs.criteria` to
`workflow/hierarchy-detect.js`; never embed or paraphrase the criteria in the
JavaScript workflow. `hierarchy-detect.js` refuses to run while `refs.criteria`
names no document, so this seam cannot be crossed by accident.

The resolution reads the tree's existing CLAUDE.md files for exactly three
purposes: **suppression** (a candidate already carried by a written fact that
resolves is a rejection, not a placement), **lift-out** (a written fact whose
depth test now fails moves, and the source document is named), and **precedent
detection** (an observable house convention outranks the criteria's
no-precedent default).

Facts enter from reports and documents ONLY. Reading code as discovery would
re-run coverage inside a lane that is not coverage.

### Step 4 -- Report and stop

```
## Hierarchy resolution -- <tree root>

Leaves enumerated: <N>
Documents in tree: <N>   Above the root: <N>
Reports loaded: <N>  (<M> candidates)

### Input inventory
- [report | assessed-null | written-doc | MISSING] <leaf>  <- <sources>
(every enumerated leaf, no exceptions)

### Document extraction inventory
- [EXTRACTED | UNEXTRACTED] <document>  <reason when UNEXTRACTED>

### Resolution -- per destination
<destination CLAUDE.md>
  - <fact>
    sources: <candidate ids>   constraints: <carried precision constraints>
    why: <why this depth>

### Subtractions -- per source
- <source leaf>: <fact>  (<from> -> <to>)
  order: write-destination-before-subtract-source

### Lift-outs from existing documents
- <document> <section>: <fact>  ->  <destination>   <reason>

### Rejections
- <candidate id>: <reason>  [routed to: <lane>]

### UNPLACEABLE
- <candidate id>: <fact>   <reason>

### Dispositions (re-judged post-subtraction)
- <leaf>: <before> -> <after>   (<candidatesBefore> -> <candidatesAfter>) <note>

### Hierarchy verdict
CHAIN-COHERENT | RESOLUTION-PROPOSED | INPUTS-INCOMPLETE (no verdict)
```

Then STOP. No Q&A, no edits, no follow-up pass.

The inventories are not decoration. `input-inventory-complete` and
`unplaceable-declared` are both fail-severity, and both are satisfiable in the
assessment and droppable at the only point a reader sees. Rendering the plan
without them produces exactly the artifact this lane exists to replace: a merge
plan that looks complete.

## Handing the report to generation

Stopping is where THIS lane ends, not where the work has to. A caller holding a
resolution may invoke the generation lane per destination. That is a separate
run of a separate lane and takes nothing away from report-only.

Three things the caller has to do, because the lane does none of them:

1. **Write destinations before subtracting sources.** A fact deleted from its
   only home before its replacement exists is a fact that exists nowhere, and
   nothing greps for an absence. The subtraction rows carry the order with them.
2. **Group by destination.** Generation is single-invocation -- one run writes
   one document -- so a resolution spanning M destinations is M generation runs,
   each taking that destination's merged facts together.
3. **Decide per item.** Rejections and demotions are editorial judgments; a
   resolution is not pre-approved by having been produced.

## Decision rules (verdict)

- `CHAIN-COHERENT` -- inventory complete, no move, merge, subtraction, lift-out
  or unplaceable item proposed.
- `RESOLUTION-PROPOSED` -- inventory complete, plan non-empty.
- `INPUTS-INCOMPLETE` -- **not a verdict.** Reported INSTEAD of one, with the
  inventory table, and the run stops.

**The verdict is COMPUTED, never returned by the assessment.** The affirmative
verdicts are unemittable while any of the following holds, and the first four
are decided BEFORE any assessment is dispatched:

- any enumerated leaf is `MISSING`;
- any loaded report names a root matching no enumerated leaf;
- the tree carries no document and no report was supplied (an affirmative
  verdict over zero input);
- no leaf was enumerated at all;
- any document in the tree is `UNEXTRACTED` or missing from the extraction
  inventory;
- any input candidate appears in no destination, no rejection and no unplaceable
  declaration, or appears in more than one;
- any unplaceable declaration carries no reason.

Each of those is the same failure wearing a different hat: a clean-looking
result over inputs the run did not actually have. The counterpart in the
coverage lane is the discovery-failure refusal, and the counterpart in review
mode is `NOT-AUDITED` being counted apart from `DIFF-CLEAN`.

**Neither affirmative verdict is ever `COMPLIANT` or `NON-COMPLIANT`, and
neither alters a document verdict.** A CLAUDE.md can be COMPLIANT while the tree
it sits in is `RESOLUTION-PROPOSED`.

**Idempotency is NOT claimed.** Same-fact identification across two reporters'
restatements is judgment, as is fact extraction from prose. Say so in the report
rather than implying a stable result.

**A resolution is a SAMPLE OF SAMPLES.** Coverage reports are non-exhaustive and
non-idempotent by their own declared contract, and a resolution over them
inherits both. The merged fact set must never be presented as the tree's fact
inventory.

## Gotchas

- Do not accept a caller-supplied leaf list. The independence of the enumeration
  is the entire basis of the input-inventory criterion.
- Do not treat a missing report as an empty candidate set. That is the fake pass.
- Do not force an unplaceable fact to the root. The root reaches every file the
  fact does not govern, which is the failure the placement constraint names.
- Do not let the lane read source to find facts. Facts enter from reports and
  documents only.
- Do not enter an inline single-subject path. See "Model pinning".

## Anti-patterns

- **Executing the plan inside the lane.** The plan spans lanes with an ordering
  constraint, contains editorial judgments a user must be able to overrule per
  item, and re-judges dispositions on the least stable quantity in the chain. A
  lane that resolved and wrote in one motion would make the report a formality
  -- the same reason there is no `coverage+generate` verb.
- **Absorbing content work.** A candidate that is really a correction of a stale
  claim is CD-lane work misfiled as a placement question. Reject it and name the
  lane; do not fix it here.
- **Verifying a merged restatement against source.** Merging two reporters'
  phrasings is where a statement true as cited becomes false as restated, so
  such a check has real value -- but it is not part of this lane, and a bounded
  anchor re-check needs designing alongside the existing verification pass
  rather than being smuggled in.
- **Claiming to solve sibling reach.** A fact whose trigger is an edit in a
  sibling subtree still has no admissible destination. This lane declares
  UNPLACEABLE and stops; it does not invent a criterion for the case.

## Cross-references

- What makes a resolution honest -- `references/standards/hierarchy-standards.md`.
- Where a fact belongs (the placement spine) -- `references/cohesion-principles.md`.
- What earns a candidate in the first place -- `references/standards/coverage-standards.md`.
- The discovery phase that feeds this one -- `coverage-lane.md`.
- Writing a destination document -- `generation-lane.md`.
- Fidelity and value of present content (never judged here) -- `references/standards/claude-md-standards.md`.
