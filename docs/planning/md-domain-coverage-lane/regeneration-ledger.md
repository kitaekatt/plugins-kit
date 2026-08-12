# Regeneration ledger

Two ledgers for findings from the 2026-08-12 audit of the generated
woodworking-sim corpus (45 CLAUDE.md files), split by whether the finding
indicts the md-domain generation workflow.

The split exists to protect a measurement. Bucket A items are fixed in the
workflow and then RE-TESTED by regenerating the corpus and checking whether the
run now finds what it missed. Bucket B items are real but are not workflow
defects, so acting on them changes the corpus without telling us anything about
the workflow -- and can mask a bucket A regression by hand-supplying content the
run was supposed to produce.

**Bucket B is HELD until generation/regeneration is signed off as high enough
quality.** Recording an item here is not a decision to build it.

Provenance matters: an item is `PROVEN` when an artifact shows the failure
(a coverage report that carries a fact the emitted document does not), and
`INFERRED` when it is a reading of the audit findings only. Only PROVEN items
are regression cases.

## Bucket A -- workflow flaws (fix, regenerate, verify)

| id | Flaw | Evidence | Provenance | Regeneration check |
|---|---|---|---|---|
| A1 | Generation drops admitted candidates | `kernel/src` coverage report proposed 4 facts (aliasing pointers in `computeOccludedSet`; `guideX` profile space vs recentered mesh; packParts hardcoding a snapshot of a live gameplay table; the hash-container rule). The emitted document carries only the last. | PROVEN | Regenerate `kernel/src`. All 4 land, or each omission carries a stated reason. |
| A2 | Emitted `Verify` commands are never executed | S4 (`src/CLAUDE.md:54` grep lacking `--include=*.js` counts CLAUDE.md files as code: claims 15, truth 13, now returns 16); S5 (`test/fixtures/CLAUDE.md:17-18` states a check that contradicts its own correct conclusion); S8 miscounts. | INFERRED | Every emitted shell-command Verify runs and its output matches the claim. |
| A3 | No verification pass over the emitted document | S6 (`godot/tests/CLAUDE.md:120-121` asserts three files carry no rationale; all three do); S7 (root retains >=8 stale claims). | INFERRED | Regenerated documents carry no claim refuted by the code they cite. |
| A4 | Hoisting cannot instantiate a document at a code-free parent | S1: `godot/scripts/CLAUDE.md:3-5` defers four whole-port facts to a `godot/CLAUDE.md`. `godot/` has 0 direct code files, so it is not a subject and the target cannot exist. Same for `kernel/` (4 documents cite `kernel/PORTING.md` as governing authority) and `godot/art/`. | PROVEN | A fact found in ANY ONE of `godot/scenes`, `godot/scripts`, `godot/stations` produces a `godot/CLAUDE.md`. Under the P0 Option C decision (2026-08-12) the check must NOT require repetition: the genuinely single-child case (`godot/tests`' private-surface fact) is exactly what a repetition-worded check would miss. The instantiation itself is settled by P2 (a directory with at least one in-scope child document is a composition subject), not by hoisting. |
| A5 | No corpus pass, so a parent cannot remove child copies | Six duplication clusters, ~250-350 lines, per the audit's Severity-3 table. Already produced a factual error (A2's 15-vs-13) one commit after the hoist. | PROVEN | Post-run, no fact appears at both a parent and a child without a recorded retain decision. |
| A6 | Authoring form rules not applied | 45 of 45 documents open with an H1 title; the root carries a stale 75-line directory tree (`:158-232`) that six children write corrective text against. The method forbids both. | PROVEN | No emitted document opens with a title header or carries a directory inventory. |

**Unsorted, and the biggest open item.** The audit reported ~20 cross-tree
reach failures (S3) as one undifferentiated problem. They are two different
things and only the first is a bucket A flaw:

- the signal IS in the directory's own files and the run missed it -- e.g. all
  ten `kernel/src/*.cpp` open with `// Port of src/kernel/<name>.js ...
  PORTING.md rules apply`, which is the subject the model reads;
- there is NO local signal, so the directory could not have discovered its own
  obligation -- the corpus itself measures this (root:135-137: 43 `.gd` files
  name a `src/` path, against 5 of 102 `.js` files).

Sorting the ~20 instances between these is owed before either bucket is
actionable.

## Bucket B -- not workflow flaws (record, hold)

Real observations that would not be fixed by a better run. Do not act on these
until generation/regeneration is signed off.

| id | Observation | Why it is not a workflow flaw |
|---|---|---|
| B1 | A directory with no local signal cannot discover an obligation owed to another tree | Owner's call, 2026-08-12: if a directory has no way to discover its obligation, that is not a problem this workflow needs to solve. |
| B2 | Submit gates are an unused reach mechanism | `claude-md-generation-method.md:400-413`: parsed deterministically, rendered verbatim, bypassing reviewer judgment and the ancestor-chain reach test. Not one of the 45 documents was checked for one. A capability question, not a defect. |
| B3 | No structural-unit enumeration | The reference adds parents with no direct source to the unit set. Our direct-code subject is a deliberate model decision. A4 was recorded as the smaller fix covering the observed case; the P0 Option C decision (2026-08-12) settled it the other way, via P2: composition subjects and coverage subjects are separate sets, and a directory with at least one in-scope child document is composed whether or not it has direct code. The coverage-subject rule is untouched. |
| B4 | Placement is judged from directory layout, not derived from build/include edges | `claude-md-generation-method.md:192-198`. A model change requiring project-specific parsers, not a run quality issue. |
| B5 | No per-document `finding-convertible` / `context-only` split is reported | CV-4 requires the classification; the workflow candidate schema has no field for it (`md-domain-coverage-gaps.md:289`). A known contract-to-schema seam. |

## Ad-hoc corpus edits made outside a run

Recorded because each one weakens the corresponding regeneration check by
hand-supplying content the run was supposed to produce. The pre-edit state is
recoverable from woodworking-sim git history.

| Date | File | Edit | Affects |
|---|---|---|---|
| 2026-08-12 | `kernel/src/CLAUDE.md` | Added the port obligation (every `.cpp` names its JS original; JS is canonical; one-to-one filenames; what shape to raise) | A1 (that fact was a dropped candidate), and the S3 sort above |
