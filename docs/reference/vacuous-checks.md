# Vacuous checks: tests and guards that do not check what they appear to

This reference supports the test-workflow rules in the root `CLAUDE.md`. It
covers a class of defect in which a test or guard does not do what it looks
like it does: its result (green or red) is decided by something other than the
property it was written to protect. The class has two faces. A check can
approve silently, staying green for a reason unrelated to the property. Or it
can object noisily, going red on a change that broke nothing.

The silent kind is worse than having no check. An absent check is visible in a
coverage gap and in a reviewer's question. A vacuous one answers that question
with a green result, so nobody looks again -- and it keeps answering after the
behaviour it guarded is gone. No test run reports this kind, because the
symptom IS a passing check.

The noisy kind fails differently. A check that cries wolf gets edited to shut
it up, and the edit can quietly remove the protection along with the noise: the
literal is loosened, the assertion deleted, the allowlist widened. Its red
result is at least visible, but the habit of dismissing it is what the next
real regression meets.

Four shapes have been observed in this repo, of two kinds. Shapes 1 to 3 are
checks that PASS when they should fail; the remedy is to assert the PROPERTY
and demonstrate the failure before believing the check. Shape 4 is a check
that FAILS when nothing is broken; the remedy is to re-point it at the
producer and show it still goes red when the fix is reverted.

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
against the real detectors, which the same file does in
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

### The fixture that carried neither thing it tested

A parametrized case can be vacuous while its siblings are sound, and the
parametrization hides it. In the bootstrap profiles work (2026-09-15), one test
asserted that the `profiles` and `profile` keys are stripped from the effective
manifest in every resolution status. Removing the strip turned five of its six
cases red and left the sixth green: that case's fixture declared neither key, so
there was nothing to strip and the assertion held either way.

Two things generalize. A revert-check must be read per CASE, not per test -- "the
test went red" is satisfied by one case and says nothing about the others. And a
fixture that omits the subject of the assertion is the specific shape to look
for, because it reads as coverage of one more status while exercising nothing.
The fix was to give that fixture the key, after which all six went red.

## Shape 3: the test pins its own mock

A hand-rolled fake standing in for a real subprocess or API can carry a
catch-all branch that absorbs any input it was not written for. When a later
change adds a new command or call the fake was not updated for, the catch-all
swallows it silently, and an assertion written against the fake's output ends
up describing what the fake DID rather than what the contract under test
says.

Worked example. Two tests named for a CLEAN scan result had been updated,
alongside a fake command runner, to assert an incomplete result: the expected
value named a subsystem the test was not about, because a catch-all branch in
the fake absorbed a command the scan added later and returned nothing for it.
The tests stayed green -- they were asserting the fake's silence, not the
scanner's completeness. The tell is exactly that mismatch: an expected value
that names something outside the test's own subject is a sign the fake, not
the contract, produced it.

The remedy is not to delete the catch-all outright. Converting it surfaces the
fake's hidden commands ONE AT A TIME -- each newly-unhandled command fails the
first test that exercises it, naming the gap instead of masking it. A blanket
conversion (making every unmatched command an error at once) is wrong for the
same reason a blanket rule usually is here: some tests legitimately depend on
an unknown command failing, and turning every catch-all into an error changes
their meaning along with the ones that needed the fix.

## Shape 4: the check pins a spelling and goes red on a change that broke nothing

The inverse hazard: not a check that cannot fail, but one that fails for a reason
that is not a defect. A check that asserts a hardcoded spelling of the thing it
guards must be edited by every change to that spelling, and the red result names
no regression.

Worked example (2026-10-01). A migration of about 95 command sites from
`${CLAUDE_PLUGIN_ROOT}/scripts/x.py` to `"${<PLUGIN>_ROOT:?...}/scripts/x.py"`
turned four tests red on spelling alone: `test_skill_drift.py` (four assertions
re-typing the launcher literal), `test_skills_kit_tool.py` (an expected literal
and its docstring), `test_scene_layers_launch.py` (an expected substring and its
docstring), and `test_python_invocation_standard.py` (two anchored allowlist
entries whose anchor text the change moved).

The remedy is a hybrid, not a rule to never re-type. `test_skill_drift.py` reads
the launcher string from the generator's own constants, so the interpreter
expression and the `:?` hint are not a second source of truth. It re-types the
two per-kit variable names (`GIT_KIT_ROOT`, `P4_KIT_ROOT`) on purpose: a
regression inside the generator's `_launcher` helper moves the constant and the
rendered output together, so a pure `constant in render` check stays green. That
is Shape 1 again. Derive what is incidental from the producer; re-type only the
property the producer cannot vouch for.

An anchored allowlist entry carries the same hazard. Its anchor is text in the
guarded file, so a change to that file can stale it. The stale anchor firing is
correct -- it caught a real edit -- but choose an anchor for being distinctive
and stable, not merely present.

## Why every shape needs a named revert-check

Shapes 1 to 3 are checks that PASS when they should fail. Shape 1 and Shape 2
are not detectable by reading the test, and shape 3 often is not either -- the
fake looks complete until a new command exposes what it was never taught to
answer. All three read as reasonable assertions about real behaviour, and all
three stay green. The only reliable signal is the counterfactual: remove the
thing the check protects and confirm the check notices.

Shape 4 is the opposite kind: a check that FAILS when nothing is broken. It is
visible the moment a change runs, so it needs no revert-check to find. It needs
one to fix: after re-pointing the check at the producer, revert the fix and
confirm it still goes red, so the repair did not trade a noisy check for a
vacuous one.

That is why this repo's task-level communication protocol asks, when a fix is
reported, which test would FAIL if the fix were reverted. Naming it forces the
counterfactual to be run rather than assumed.
