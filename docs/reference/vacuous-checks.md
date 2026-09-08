# Vacuous checks: tests and guards that pass without checking

This reference supports the test-workflow rules in the root `CLAUDE.md`. It
covers a class of defect that no test run reports, because the symptom IS a
passing check: a test or guard that is green for a reason unrelated to the
property it was written to protect.

The cost is worse than having no check. An absent check is visible in a
coverage gap and in a reviewer's question. A vacuous one answers that question
with a green result, so nobody looks again -- and it keeps answering after the
behaviour it guarded is gone.

Two shapes have been observed in this repo. They differ in what the check
compares, but the remedy is the same in both: assert the PROPERTY, and
demonstrate the failure before believing the check.

## Shape 1: the check compares A to B, and both can move together

A drift guard that compares a generated artifact to its generator is green
whenever the two agree. It cannot distinguish "the artifact is correct" from
"the artifact and the generator are both wrong in the same way", because
regeneration is what makes them agree.

Worked example. `tests/bootstrap/code_review/test_skill_drift.py` asserts the
ten rendered code-review skill files are byte-identical to what
`scripts/gen_code_review_skills.py` renders. That guard stops the two kits
drifting apart, and it stops a hand-edit to a rendered file. It does NOT
protect the machine-emitted banner: dropping `BANNER` from the template and
regenerating leaves both sides matching, the byte-identity check green, and
the guard silently no longer guarding the thing that made the files safe to
exclude from review.

What the banner actually buys is a property of the CONTENT -- a code review
classifies these files as machine-emitted, and the pre-commit guard's exemption
has something to exempt. So the remedy was to assert that property directly
against the real detectors, which the same file now does in
`TestRenderedFilesAreDetectableAsMachineEmitted`:

- `detect_machine_emitted` fires on every rendered path.
- `detect_signature_bytes` fires on the rendered BYTES, because the pre-commit
  guard reads blobs as bytes and a banner only the text detector finds would
  exempt a path with nothing to exempt.

The general test: when a check compares A to B, ask what happens when A and B
move together. If the answer is "it stays green", the check is a consistency
check and something else has to carry the property.

## Shape 2: the test passes with the fix reverted

A test written alongside a fix tends to be written in the fix's own terms, and
it can end up asserting something that was already true. Three landed in one
task (`bootstrap-display-rule5`, 2026-09-08) before the pattern was named:

- Two called `_append_detail` directly with hand-written strings. That asserts
  only that `_append_detail` honours an explicit `display=` label, which is
  pinned elsewhere -- it says nothing about the production call sites the fix
  changed.
- One precedence test used a fixture identifier that the claim glob `**/*.md`
  never matches, so the claim it was meant to outrank was never made and the
  precedence was never exercised.

The same task produced the live demonstration of why a green runtime assertion
is not enough. Reverting a production display site to a bare append left the
runtime test GREEN -- `derive_short` happened to cut the log line at a
separator sitting before the path, so the rendered text was clean for a reason
unrelated to the fix. Only the AST guard over the source went red.

The general test, and it is cheap: revert the fix, run the named test, and
watch it FAIL. A test that stays green with the fix removed is not testing the
fix. Do this before committing, not after a reviewer asks.

## Why both shapes need a named revert-check

Neither shape is detectable by reading the test. Both read as reasonable
assertions about real behaviour, and both pass. The only reliable signal is
the counterfactual: remove the thing the check protects and confirm the check
notices.

That is why this repo's task-level communication protocol asks, when a fix is
reported, which test would FAIL if the fix were reverted. Naming it forces the
counterfactual to be run rather than assumed.
