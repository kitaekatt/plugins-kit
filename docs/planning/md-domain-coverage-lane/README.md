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
code it describes -- is what this lane addresses.

## Contents

- `coverage-lane-spec.md` -- the lane design: unit `(code subtree, ambient
  CLAUDE.md chain)`, mandatory exclusions, the distinct `GAPS-FOUND` /
  `COVERAGE-ASSESSED` verdict vocabulary, and the remediation routing that puts
  code fixes and loud failures AHEAD of documentation.
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

Not started. Unblocked as of 2026-08-08 -- the language-family validation gate
that previously blocked it was retired as an accepted limitation (see the
provenance entry in md-domain's `references/provenance/standards-decisions.md`).

The gate that remains: run the four negative controls in `coverage-gap.md`,
especially the documented-loud-and-test-enforced near miss, and measure
precision on whatever corpus is at hand that the design was not built against.
If none is, ship and record the limitation rather than block.
