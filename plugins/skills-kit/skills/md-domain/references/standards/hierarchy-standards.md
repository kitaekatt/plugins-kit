# Hierarchy Standards

**READ THIS FIRST -- THESE CRITERIA DESCRIBE A SUPERSEDED MODEL.** They resolve
placement over candidate proposals produced by a SUBTREE-scoped coverage run. A
coverage subject is one directory's own direct code files
(`coverage-standards.md`, Subject), and `fact-scoped-to-this-directory` forbids
a candidate from naming a destination outside the directory it was assessed
from, so the input these criteria assume is no longer produced.

`shallowest-true-depth` remains sound IN SUBSTANCE -- a fact lives at the
shallowest directory where it is true of everything below it -- but its framing
names the retired direction ("promotes a candidate"). Under the current model it
is a COMPOSITION-time judgment over a parent's children's finished documents,
and it is paired with a wording test: a hoisted fact must be worded so it is
true as stated at its new depth, or it stays in the children. See
`references/lanes/generation-lane.md`, parent composition, which cites this
criterion as the depth authority.

Disposition of the lane as a whole: `CLAUDE.md`'s amendment to
`hierarchy_is_the_resolution_phase_over_a_tree`.

What makes a placement resolution over a whole CLAUDE.md tree honest.

Read by the `hierarchy_claude_md_tree` lane
(`references/lanes/hierarchy-lane.md`), which passes this document's path as
`refs.criteria` to `workflow/hierarchy-detect.js`. The workflow applies these
criteria verbatim; it does not paraphrase them and does not supply criteria of
its own.

**Subject.** A `claude_md_tree` composition -- a named directory root, every
CLAUDE.md governing files beneath it, and (optionally) persisted coverage
reports targeting subtrees under it. Not a markdown file, and not a single
subtree. Every criterion here is a RELATION between documents or between a
proposal and a document, which is why none of them can be evaluated on one
file.

**Direction.** Hierarchy decides WHERE each fact in the tree lives -- exactly
one home per fact, at the shallowest depth at which it is true of everything
below. It never judges the fidelity or value of present content (that is
CD-1..CD-6 in `claude-md-standards.md` section 3) and it never discovers new
facts from source (that is coverage). It resolves placement over facts it was
handed.

**Report-only.** The lane binds no remediate workflow. Its output is a plan:
per-destination merged fact sets, a per-source subtraction table, re-judged
leaf dispositions, and declared unplaceable items. Nothing is written by this
lane, whoever calls it and whatever the chain does next.

## What good looks like

A good resolution accounts for every input candidate exactly once, states the
subtraction each merge implies at its source, and refuses an affirmative
verdict whenever its inventory is incomplete. A resolution that proposes no
moves is a normal result: `CHAIN-COHERENT` is not a failure state.

The three `fail` severities (HR-1, HR-4, HR-7) all guard the same error: a plan
that reads as complete over inputs it did not have, or over facts it could not
place. That error is expensive because the plan's reader acts on it -- a
subtraction executed against a destination that was never written deletes a
fact from the only file that carried it.

| Criterion id | Short id | Severity |
|---|---|---|
| `one-home-per-fact` | HR-1 | fail |
| `shallowest-true-depth` | HR-2 | judgment |
| `precedent-outranks-hoisting` | HR-3 | judgment |
| `input-inventory-complete` | HR-4 | fail |
| `disposition-re-judged` | HR-5 | judgment |
| `merge-preserves-precision` | HR-6 | judgment |
| `unplaceable-declared` | HR-7 | fail |

```yaml
standards_set:
  _schema_version: "1"
  identity: >-
    Placement-resolution criteria deciding where each fact in a CLAUDE.md tree
    lives, applied to a named directory root together with the tree's existing
    CLAUDE.md files and any persisted coverage candidate sets targeting it.
  applies_to: claude_md_tree
  criteria:
    - id: one-home-per-fact
      statement: >-
        A fact appears exactly once across the resolved chain -- one
        destination, with every other reporter of it collapsed into that entry
        and subtracted at its source.
      severity: fail
      enforcement: judgment
      keywords: [one home, duplicate collapse, sibling blindness, same fact two reporters, subtraction, chain duplication]
      example: >-
        Two sibling subtrees each report the same registration requirement,
        correctly, because neither one's ambient chain carries the other's
        CLAUDE.md. The resolution emits ONE entry at the common ancestor and a
        subtraction row for each reporting leaf. Emitting the fact at both
        leaves, or at the ancestor AND a leaf, is the duplication the placement
        spine forbids.

    - id: shallowest-true-depth
      statement: >-
        A fact lives at the shallowest directory where it is true of everything
        below it, and no shallower.
      severity: judgment
      enforcement: judgment
      keywords: [depth, promote, leaf only, hoisting, common ancestor, true of everything below, CCP CRP ADP]
      example: >-
        This is CCP change cadence, CRP reader set, and ADP load order applied
        across a tree; defer to references/cohesion-principles.md rather than
        re-deriving them. A fact whose violators all work inside one leaf does
        not earn a parent placement, and a fact true of every leaf does not stay
        in one of them. The test runs in BOTH directions -- it promotes a
        candidate and it lifts a written fact out of a document that is too deep
        or too shallow for it.

    - id: precedent-outranks-hoisting
      statement: >-
        Where the tree already places a class of fact by an observable
        convention, follow that convention; absent one, a mutual-sync fact
        ("these two files change together") whose subject is a single mirrored
        directory goes to the mirrored directory, not to the common ancestor.
      severity: judgment
      enforcement: judgment
      keywords: [precedent, house convention, observable, mutual sync, mirrored directory, common ancestor, detection not preference]
      example: >-
        DETECT the convention before applying the default: if two existing
        documents already place sync rules beside the mirrored directory, that
        is the tree's answer and the default never fires. The default exists only
        for a tree with no precedent to read. A project holding the opposite
        position disables this criterion by id rather than restating it in prose.

    - id: input-inventory-complete
      statement: >-
        An affirmative verdict may be emitted only when every enumerated leaf
        maps to a candidate report, an explicit assessed-null, or a written
        document -- never over a silent absence.
      severity: fail
      enforcement: mechanical
      keywords: [inventory, missing report, absence of evidence, INPUTS-INCOMPLETE, enumerated leaf, silent absence, fake pass]
      example: >-
        A resolution handed 10 of 18 leaf reports must not treat the other 8 as
        empty candidate sets. Absence of a report is absence of evidence, not
        evidence of absence. The lane enumerates the leaves itself precisely so
        this inventory does not pass through the caller's hands, and the verdict
        is COMPUTED from the inventory table: any MISSING row makes both
        affirmative verdicts unemittable.

    - id: disposition-re-judged
      statement: >-
        Every leaf whose candidate set was reduced by subtraction has its
        disposition re-judged from the post-subtraction count, and a flip runs
        WARRANTED to NOT-WARRANTED only.
      severity: judgment
      enforcement: judgment
      keywords: [disposition, re-judge, post subtraction, downward only, warranted, thin verdict, holds by one fact]
      example: >-
        A leaf warranting its own CLAUDE.md on four candidates, three of which
        promote to a parent, is re-judged on the remaining one -- and the report
        says the verdict holds by one fact. An upward flip is arithmetically
        impossible here: subtraction only REMOVES content from a leaf, so a leaf
        that did not warrant a document before cannot warrant one after.

    - id: merge-preserves-precision
      statement: >-
        When duplicate reporters are collapsed, the narrower verified statement
        wins, and any precision constraint a reporter recorded survives the
        merge.
      severity: judgment
      enforcement: judgment
      keywords: [merge, restatement, over broadening, narrower wins, precision constraint, do not restate as, true as cited]
      example: >-
        Two reporters state the same rule at different widths. The merged entry
        takes the narrower one and carries both reporters' anchors; a reporter's
        recorded constraint ("do not restate this as a general rule -- it holds
        only for the loader") is carried into the merged entry verbatim rather
        than dropped as commentary. Merging two phrasings is exactly where a
        statement true as cited becomes false as restated.

    - id: unplaceable-declared
      statement: >-
        A fact for which the placement constraint admits no destination is
        reported UNPLACEABLE with the reason, never forced to the root and never
        silently dropped.
      severity: fail
      enforcement: mechanical
      keywords: [unplaceable, no admissible destination, sibling trigger, forced to root, silently dropped, declared not resolved]
      example: >-
        A fact whose trigger is an edit in a SIBLING subtree has no destination
        that is an ancestor of every file it governs and of no file it does not.
        Hoisting it to the root reaches every file it should not; dropping it
        loses it. Declare it UNPLACEABLE with the reason and stop -- this
        criterion reports the condition, it does not resolve it.
```

## The input inventory, and why the verdict is computed from it

The lane enumerates the leaves under the root ITSELF and builds one row per
leaf:

| Leaf | Status |
|---|---|
| `<dir>` | `report` -- a candidate report targets it |
| `<dir>` | `assessed-null` -- a report targets it and states zero candidates |
| `<dir>` | `written-doc` -- no report, but the leaf carries its own CLAUDE.md |
| `<dir>` | `MISSING` -- none of the above |

A caller-supplied leaf list is exactly the input this criterion distrusts: a
tree's own root document routinely omits directories from its structure map,
and a resolution built on a list that already forgot a leaf cannot notice the
omission. Enumerating independently is what makes HR-4 checkable at all.

Two further inventories bar an affirmative verdict on the same principle:

- **Document extraction.** A CLAUDE.md the lane failed to parse, or chose not
  to read, is reported `UNEXTRACTED`. A chain cannot be declared coherent
  against a document nobody read.
- **Input accounting.** Every candidate carried by every loaded report is
  accounted for exactly once -- folded into a merged entry, rejected with a
  reason, or declared unplaceable. A candidate that appears in none of the
  three was silently dropped, and a plan that silently drops inputs is the
  failure this criteria set exists to prevent.

## Verdicts

- `CHAIN-COHERENT` -- inventory complete, and the resolution proposes no move,
  merge, or subtraction.
- `RESOLUTION-PROPOSED` -- inventory complete, and the plan is non-empty.
- `INPUTS-INCOMPLETE` -- **not a verdict.** It is what the lane reports INSTEAD
  of a verdict when the inventory is incomplete, when a document is
  `UNEXTRACTED`, when an input candidate went unaccounted for, or when there is
  no input at all. It carries the inventory table and stops.

Neither affirmative verdict is `COMPLIANT` or `NON-COMPLIANT`, and neither
alters any document's verdict -- same posture as coverage. A CLAUDE.md can be
COMPLIANT while the tree it sits in is `RESOLUTION-PROPOSED`.

**Idempotency is NOT claimed.** Extracting a fact from written prose is
judgment, and so is deciding that two reporters' restatements are the same
fact. Re-runs may differ; the report says so.

**The plan is a sample of samples.** Coverage reports are non-exhaustive and
non-idempotent by declared contract, and a resolution over them inherits both.
The merged fact set is never the tree's fact inventory, and must not be
presented as one.

## What is not in this doc

- **Whether a fact is worth writing at all.** That is coverage
  (`coverage-standards.md`), applied before a candidate reaches this lane.
- **Whether present content is accurate or valuable.** CD-1..CD-6 in
  `claude-md-standards.md` section 3. A candidate that is really a correction
  of a stale claim is ROUTED there, not absorbed here.
- **The placement algorithm itself.** `references/cohesion-principles.md` is the
  spine; HR-2 and HR-3 constrain the ANSWER and defer the derivation.
- **How a destination document gets written.** Generation is a separate lane and
  a separate run per destination.
- **How to resolve an UNPLACEABLE fact.** HR-7 declares the condition. Nothing
  here invents a criterion for a fact whose trigger lives in a sibling subtree.

## Configuring these criteria

Every criterion above carries a stable id and is disable-able or tunable
through the standard mechanism in `references/configuring-standards.md`. A
project that holds a different position on an opinion here changes it there
rather than forking this document.

**Named deviation: `applies_to: claude_md_tree` is not a file-type primitive.**
`references/authoring-standards.md` says a standards file governs exactly one
file-type primitive. This subject is a directory root plus the documents and
proposals attached to it -- a composition, not a file type -- so it is
registered under `audit-framework.yaml::compositions`, leaving the primitive
roster unchanged. The same deviation `coverage-standards.md` records, for the
same reason.

The one opinion worth naming explicitly, because a competent team genuinely
disagrees with it, is `precedent-outranks-hoisting`: absent an observable
convention it sends a mutual-sync fact to the mirrored directory rather than to
the common ancestor. That is a deliberate default rather than an oversight --
hoisting a sync rule puts it in the load path of every file that will never
touch either side of the sync -- and a project preferring ancestor-hoisting
disables the criterion by id.
