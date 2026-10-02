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

Eight shapes have been observed in this repo, of two kinds. Shapes 1 to 3 and
5 to 8 are checks that PASS when they should fail, or that offer evidence which
cannot be trusted; the remedy is to assert the PROPERTY and demonstrate the
failure before believing the check. Shape 4 is a check that FAILS when nothing
is broken; the remedy is to re-point it at the producer and show it still goes
red when the fix is reverted.

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

## Shape 5: the concurrency suite whose evidence is flaky or absent while its assertions pass

A test of behaviour under contention has two jobs: assert the property, and
prove the contention OCCURRED. The second is itself a check, and it can be
vacuous or unreliable while every assertion on the subject passes.

Worked example (content-pipeline-kit spend ledger, 2026-10). Two instances.

- A one-writer version of a status-under-writers test passed.
  With the load-bearing `BEGIN` removed from the reader helper in
  `spend_ledger.py` it STILL passed, because a single writer never raced the
  reader. Three concurrent writers plus a floor on raced commits made it fail
  3 of 3 with the `BEGIN` removed
  (`TestStatusUnderLiveWriters` in
  `tests/content-pipeline-kit/test_llm_spend_ledger_halt.py`, which records
  both the one-writer green run and the 3-of-3 red run).
- A clock-based overlap-floor version of the cross-process cap test proved
  contention by elapsed time. That floor was intermittent in 4 of 10 runs while the ledger
  assertions passed every time, so the EVIDENCE was unreliable, not the
  subject. The replacement is a gate process that holds the write lock until
  every child has announced that its next statement is `reserve`
  (`tests/content-pipeline-kit/test_llm_spend_ledger_concurrency.py`; the gate
  prints `RELEASED <n>` and the test asserts `n` equals the child count).
  That is deterministic and clock-free.

The remedy: give every multi-process test an explicit contention floor, and
build the floor from a rendezvous or a lock held until all parties arrive,
never from elapsed time. Then run the revert-check on the floor itself: remove
the protection and confirm the test goes red, and run the test repeatedly (the
4-of-10 intermittence was invisible in a single run).

## Shape 6: an independent recomputation that agrees with production because both move together

A test can recompute the expected value "independently" and compare it to the
production value. If both are derived from the same rows, deleting the
production behaviour moves both terms and the comparison holds.

Worked example (content-pipeline-kit spend ledger, 2026-10). The partition
suite carries both halves of this shape in one file. `independent_partition`
recomputes the five-state partition with SQL written independently of the
production queries -- an A-vs-B comparison, which on its own cannot distinguish
a correct partition from two wrong ones derived from the same rows. What
actually carries the property is the HARDCODED LITERALS beside it: dropping the
`SUM(reported_cost) FROM leaks` half of the production LEAKED term went red as
`assert 1.0 == 4.0` against a literal the test owns, and only once a leak row
had been injected -- the empty-table half alone could NOT show it red, which is
why the injection sits in that test rather than being left to the unit that
owns the write path
(`tests/content-pipeline-kit/test_llm_spend_ledger.py`). This is Shape 1 in a
test rather than a generator: the check compares A to B, and both can move
together.

The remedy: the literal is load-bearing. Never relax it into a comparison
against a recomputed value, and when reviewing a test, treat a hardcoded
expected number next to a recomputed one as the part that carries the property.

## Shape 7: a predicate whose removal stays green because the language already excludes the case

Removing a guard and watching the test stay green does not show the guard is
unnecessary, and it does not show the test is sound. It can mean the language
or engine already excludes the case, so the revert is not a counterfactual at
all.

Worked example. In `tests/content-pipeline-kit/test_llm_spend_ledger_orphans.py`
(`TestNullLeaseIsNeverSwept`), deleting `AND lease_expires_at IS NOT NULL` from
the sweep's eligibility query changed nothing: SQLite's three-valued logic
already makes `lease_expires_at < ?` NULL, not true, for a NULL lease. The
clause is belt and braces. The real counterfactual is an INSERTION: widening the
predicate to `AND (lease_expires_at IS NULL OR lease_expires_at < ?)` turned
the test red (the unleased row was swept) and its companion red
(`DID NOT RAISE SpendCapExceeded`).

The remedy: when removing a guard leaves the test green, look for the insertion
that breaks the property before concluding the guard is unnecessary. A property
stated as an absence ("never swept") is broken by adding a case, not by
removing a clause. Record the corrected prediction beside the test, as that
test's docstring does.

## Shape 8: a structural property no runtime test can carry

Some design properties have no observable runtime difference, so any runtime
test written for them is vacuous by construction.

Worked examples, both in the spend ledger work.

- `BEGIN IMMEDIATE` versus plain `BEGIN`. Two runtime attempts failed. With no
  transaction-level retry anywhere in the codebase, swapping in a plain `BEGIN`
  yields a fail-closed error, not an over-admission, so there is no wrong
  NUMBER to observe. A hand-built deferred-read-then-insert only asserts
  SQLite's own `SQLITE_BUSY_SNAPSHOT` behaviour, true with or without the
  design. `spend_ledger.py` holds three `BEGIN` sites (two `BEGIN IMMEDIATE`,
  one deliberate plain deferred `BEGIN` for the read-only snapshot).
- Reserve outside the settling `try`. A runtime test asserting the reserve sits
  outside the `try` stayed green when the reserve was moved inside, because the
  variable is initialised above the `try` and the move is unobservable
  (`tests/content-pipeline-kit/test_llm_platform_spend.py`).

Both went to AST SOURCE GUARDS that state their own revert in the docstring
(`test_reserve_is_lexically_outside_the_settling_try` opens "NO RUNTIME TEST CAN
CARRY THIS" and names the revert), and the design says plainly that no runtime
test can carry the property. The honest move is to say so rather than write a
third test that looks like coverage. The guard is still subject to Shapes 1 and
2: show it red against the named revert.

## Why every shape needs a named revert-check

Shapes 1 to 3 and 5 to 8 are checks that PASS when they should fail (or whose
evidence is unreliable). Shape 1 and Shape 2
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

## Where verification effort pays

A seven-unit strand building the cross-process spend ledger produced ten
vacuous or mispredicted checks (Shapes 5 to 8 are four of them). Every one was
caught by its own author applying the counterfactual, and none by a review
lane: all three code-review lanes returned zero findings on all seven units. An
md-domain subject-lens audit of the same strand's documentation found a
documented falsehood and two misplaced facts.

The inference is about where to spend effort: a revert-check inside the unit
finds what review lanes do not, and documentation needs its own audit. It is
not a claim that review lanes are worthless. They are the control that makes a
clean result mean something.
