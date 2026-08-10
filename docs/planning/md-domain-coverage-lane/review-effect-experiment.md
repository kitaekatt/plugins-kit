# The documentation-on/off review experiment

Measures **link 4 (DEMONSTRATE)** of the md-domain review-enablement goal chain:
does a CLAUDE.md that md-domain produced change what a code review finds?

This is NOT the held-out coverage run. That experiment measures candidate
precision/recall and was run 2026-08-09. This one measures DOWNSTREAM EFFECT.
Neither substitutes for the other.

**Pre-registered 2026-08-10, before any reviewer was dispatched.** The scoring
rule and the effect threshold below were fixed in advance precisely so the
result could come out negative. An honest "no measurable effect" is a valid and
valuable outcome: it would say the goal's premise is wrong.

## Subject

`woodworking-sim` at `e654891`, subtree `src/kernel` (a 10-file pure geometry
kernel). The document under test is `src/kernel/CLAUDE.md` -- 93 lines, six
sections, generated 2026-08-10 by the md-domain coverage -> generation chain
(skills-kit 0.44.1). It was NOT hand-written for this experiment.

The repo is held out by construction: md-domain's criteria were not derived
against it.

## Design

Two arms, identical in every respect except one file's presence on disk:

| Arm | `src/kernel/CLAUDE.md` | Reviewable diff |
|---|---|---|
| `arm-on` | present | `src/kernel/meshOptimize.js` only |
| `arm-off` | absent | `src/kernel/meshOptimize.js` only |

Both are detached `git worktree`s off `e654891`, so the shared working tree is
never touched. In `arm-off` the document's removal is COMMITTED first, so the
reviewable diff is byte-identical between arms and does not itself reveal which
arm a reviewer is in. Verified by `diff` on the two `meshOptimize.js` files.

Three independent reviewers per arm, same model (sonnet), same prompt, blind:
they are told to review a pending change and are told nothing about CLAUDE.md,
about the hypothesis, or about anything having been planted.

## The planted change

One plausible CL against `src/kernel/meshOptimize.js`. **Every plant is SILENT:
the full `npm test` suite passes (exit 0) with the change applied.** That is the
precondition -- a hazard a test already catches cannot discriminate between arms.

| id | Plant | Documented in the CLAUDE.md? |
|---|---|---|
| P1 | `flushDenormals(srcPos)` mutates the caller's `positions` array IN PLACE | YES -- the zero-copy-views section |
| P2 | `edgeToTris` changed from `Map` to a plain object | YES -- the iteration-order section |
| P3 | `WELD_EPSILON` raised 1e-4 -> 2e-3, crossing above the 1e-3 connectivity weld | YES -- the tolerance-ladder section |
| P4 | behavior changed in `src/kernel/*.js` with no matching `kernel/src/*.cpp` edit and no fixture re-export | YES -- the C++-mirror section |
| X1 | the degenerate-normal guard `if (lengthSq3(normal) < 1e-10) continue;` deleted | **NO** -- a real hazard the document does not cover |
| C1 | `triCount` -> `triangleCount` rename plus a doc line | benign control; nothing is wrong with it |

P1-P4 are the treatment. X1 is a tunnel-vision check: if the document helps by
FOCUSING attention, it may also narrow it, and X1 is how that would show up.
C1 is a false-positive check.

## Scoring rule

Per reviewer, per plant, one of:

- **RAISED** -- flags the change AND states the correct consequence.
- **NOTED** -- mentions the line but not the consequence. Does not count as
  RAISED. (A reviewer saying "`Map` changed to an object" without saying order
  is observable has not protected anything.)
- **MISSED** -- neither.

C1 scores **FALSE POSITIVE** if raised as a problem.

Scoring is done against the reviewer's verbatim output by an assessor that is
NOT one of the reviewers.

## Effect threshold (fixed in advance)

Primary: mean RAISED count over P1-P4 (max 4), `arm-on` vs `arm-off`.

An effect is declared only if BOTH hold:

1. `arm-on` mean exceeds `arm-off` mean by **>= 1.5 of 4**, and
2. at least one plant is RAISED by **every** `arm-on` reviewer and by **no**
   `arm-off` reviewer.

Clause 2 is what stops a diffuse one-finding-here-one-there difference, which
at n=3 is noise, from being read as an effect.

Secondary, reported regardless:

- **Tunnel vision** -- if X1's RAISED count drops by >= 2 reviewers in `arm-on`,
  that is a finding AGAINST the document and is reported as prominently as any
  positive result.
- **False positives** on C1, both arms.

## Known limitations of this design

- **n=3 per arm.** Enough to see a large effect, not enough to size a small one.
- **Ceiling/floor risk from model choice.** One model tier is used across both
  arms, so internal validity holds, but a tier that catches everything or
  nothing in both arms would produce a null for reasons unrelated to the
  document.
- **The plants violate rules the document states.** This measures whether the
  document is USABLE by a reviewer -- not whether its facts are true, which was
  established separately against source. It cannot tell us whether a document
  written about DIFFERENT facts would help.
- **One subtree, one CL, one repo.** A sample, not an inventory.

## Answer-key hazard (why the key is constructed, not harvested)

The 2026-08-09 coverage run drew its answer key from `14bebbc`, a BUG-FIX
commit, so "the right remedy was a code fix" restated how the sample was drawn
rather than measuring anything. Every conclusion about remedies was void.

Here the key is CONSTRUCTED: the plants were written for this experiment, and
the invariants they violate were verified true against source independently of
the document. No plant is drawn from a commit whose remedy was already decided.

## Result

See `review-effect-results.md` in this directory.
