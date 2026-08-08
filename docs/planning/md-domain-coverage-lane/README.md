# md-domain coverage lane -- planning

Design for an UNBUILT addition to `skills-kit:md-domain`: an opt-in lane that
audits whether a code subtree's hazards are covered by the CLAUDE.md files that
actually load for it.

Nothing here ships to consumers. These are planning documents for work that has
not started; the shipped summary lives in
`plugins/skills-kit/skills/md-domain/references/coverage-gap.md`.

## Why this exists

A full, clean md-domain audit of a project coexisted with many undocumented,
code-review-relevant hazards. The audit was not wrong -- it answers whether the
DOCUMENT is internally coherent and locally accurate, which is orthogonal to
whether a reviewer working from it would catch a defective change. Of the six
mechanisms behind that gap, four are closed or scoped; the one that would
actually deliver the goal -- nothing checks whether a fact is AMBIENT for the
code it describes -- is what this verb addresses.

**Scope, stated up front because it was got wrong once.** md-domain is not a
code-review tool. This verb reads code only as a SOURCE OF INSIGHT for the
CLAUDE.md that will be ambient for it. It does not hunt for defects; a defect
noticed in passing is reported only if severe, and only ever as CLAUDE.md
content. Finding defects is the job of a code review conducted AGAINST the
CLAUDE.md this verb helps produce. See the spec's opening section.

## Contents

- `coverage-lane-spec.md` -- the design: unit `(code subtree, ambient CLAUDE.md
  chain)`, the report-only third-verb shape, intent gating, exclusions, and the
  distinct `GAPS-FOUND` / `COVERAGE-ASSESSED` verdict vocabulary.
- `regression-test-design.md` -- how to measure it: the pre-registered answer
  key, per-mechanism recall, the negative controls, and the circularity
  limitation that makes recall alone meaningless.

## Read this before building it

Two independent adversarial reviews rejected the obvious version of this
feature -- a "hazard sweep" that reads code and reports what the docs omit. The
reasons are recorded in `coverage-gap.md` under "Why the obvious fix is wrong",
and they are not stylistic:

- the hazard predicate is not enumerable (non-determinism lives in the
  candidate set, not the label set);
- ambient budget is PER FILE, so trading lines across a corpus does not net out;
- documenting a hazard can FOSSILIZE a bug whose right remedy was a code fix or
  a loud failure.

The design here survives those objections by reusing the authoring direction's
existing observation kinds rather than inventing a hazard taxonomy, and by
never touching document compliance. Do not rediscover the rejected version.

## Status

Split, as of 2026-08-08.

- **PIPELINE -- settled and buildable.** Shape (report-only third verb),
  discovery, ambient-chain resolution, exclusions, intent gating, verdicts,
  report shape, and their tests. None of it depends on the criteria below.
- **ANALYSIS CRITERIA -- BLOCKED, awaiting two documents from the owner.** What
  makes a code-derived fact worth a place in a CLAUDE.md, and the severity bar
  for the severe-deficiency carve-out. Do not invent these; build the pipeline
  with the criteria seam left open.

The language-family validation gate that once blocked the whole design was
retired 2026-08-08 as an accepted limitation (see the provenance entry in
md-domain's `references/provenance/standards-decisions.md`).

The gate that remains: run the four negative controls in `coverage-gap.md`,
especially the documented-loud-and-test-enforced near miss, and measure
precision on whatever corpus is at hand that the design was not built against.
If none is, ship and record the limitation rather than block.
