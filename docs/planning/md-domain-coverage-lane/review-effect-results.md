# Documentation-on/off review experiment -- results

Run 2026-08-10. Design and scoring rule pre-registered in
[review-effect-experiment.md](review-effect-experiment.md), committed as
`f4e3f20` before any reviewer was dispatched.

## Result: NO EFFECT DETECTED

The two arms tied exactly.

| Arm | Reviewer scores (RAISED of 4) | Mean |
|---|---|---|
| `arm-on` (document present) | 3, 1, 2 | **2.00** |
| `arm-off` (document absent) | 3, 2, 1 | **2.00** |

Both pre-registered clauses fail, and not narrowly:

1. `arm-on` exceeds `arm-off` by **0.00**, against a required **>= 1.5**.
2. No plant was RAISED by every `arm-on` reviewer and no `arm-off` reviewer.
   The two plants everyone caught (P1, X1) were caught 3/3 in BOTH arms.

**The generated CLAUDE.md did not change what this review found.**

## Per-plant breakdown

| id | Plant | Documented? | on | off |
|---|---|---|---|---|
| P1 | in-place mutation of the caller's `positions` | YES | 3/3 | 3/3 |
| P2 | `edgeToTris` `Map` -> plain object | YES | 0/3 | 0/3 |
| P3 | `WELD_EPSILON` 1e-4 -> 2e-3 | YES | 2/3 | 2/3 |
| P4 | `.cpp` mirror + fixtures not updated | YES | 1/3 | 1/3 |
| X1 | degenerate-normal guard deleted | **NO** | 3/3 | 3/3 |
| C1 | benign rename (control) | -- | 0 FP | 0 FP |

Secondary results, reported as pre-registered:

- **No tunnel vision.** X1 -- the one real hazard the document does NOT
  mention -- was raised 3/3 in BOTH arms. Having the document did not narrow
  attention. This was the main risk the design was built to catch, and it did
  not materialize.
- **No false positives on C1** in either arm. One `arm-on` reviewer did note
  that the rename left a stale `triCount` reference in a docstring -- a real
  defect the control introduced, so the control was imperfect, but the finding
  is correct and is not scored as a false positive.

## Why the null -- the finding that matters

Every fact the document carries was **independently reachable within one or two
hops**, and the reviewers took those hops unprompted:

- **P1** -- both arms quoted `src/kernelAdapter.js`'s module comment
  ("kernel functions never mutate inputs unless documented 'in place'")
  verbatim. The document exists to carry that fact into the subtree; reviewers
  simply opened the adapter.
- **P3 / P4** -- `meshOptimize.js`'s own header already says "behavior changes
  here invalidate the kernel regression suite, don't make any." Reviewers
  followed it outward. One `arm-off` reviewer went to `kernel/PORTING.md`
  unaided, opened the C++ mirror, and enumerated the exact divergences
  (`kMeshOptWeldEpsilon` still 1e-4, both guards still present, no
  `flushDenormals`). That is a better P4 finding than any `arm-on` reviewer
  produced.

So the document CONSOLIDATED facts that were already discoverable. For an LLM
reviewer, one or two hops is cheap, and consolidation of cheap-to-reach facts
buys little.

**This is a real limitation in the coverage lane's value model.** CV admission
turns on whether a fact is AMBIENT -- present in the CLAUDE.md chain. A reviewer
is not confined to the ambient chain; it reads whatever it wants. So the
document's marginal value is over what a reviewer would NOT otherwise find, which
is a strictly smaller set than "not ambient". A fact sitting in a sibling file's
module comment is *not ambient* and *highly discoverable* at the same time, and
coverage currently cannot tell those apart.

The gap is real but narrower than "coverage is not useful": it is that
`not ambient` is being used as a proxy for `the reviewer will miss it`, and these
came apart on every plant here.

## P2 -- the document is wrong, and six reviewers were right

P2 was NOTED by all six reviewers and RAISED by none. Every one independently
concluded the `Map` -> plain-object swap was functionally harmless.

**They are correct.** `edgeKey` produces `"<int>_<int>"` strings, which are not
integer-like, so V8 preserves insertion order for them. The swap does not change
behavior.

The document's section says "Do not swap a `Map` for a plain object" and calls
this "the highest-silence failure in the directory". That is **stricter than
reality**. It is a conservative overstatement inherited from
`kernel/PORTING.md` point 5, which is about what the C++ port must reproduce --
a real constraint that does not imply the JS-side swap is unsafe.

Consequence for md-domain, and it is the sharper of the two findings here: the
generation lane faithfully carried a source document's rule into the new
CLAUDE.md **without testing whether the rule was true as restated**. Nothing in
the chain checks a carried-forward claim against the code it constrains. This is
distinct from a fabrication -- the fact had a real source -- which is exactly why
neither the coverage assessment's verification pass nor the generation lane
caught it.

P2 therefore functioned as an accidental SECOND control, and both arms passed it.

## Validity limits

- **Limited dynamic range.** P1 and X1 sat at ceiling (6/6) and P4 near floor
  (2/6). Only P3 discriminated in the middle, so the design had roughly one
  informative plant. A larger effect could have been detected; a small one could
  not.
- **n=3 per arm**, one model tier, one subtree, one CL, one repo.
- **The reviewers were strong.** A weaker reviewer -- or a human under time
  pressure, who will not open `PORTING.md` on a hunch -- might show the effect
  this run did not. The null is about THIS reviewer population.
- The result says nothing about a document written over facts that are genuinely
  unreachable from the code. It says the facts this document carried were not
  of that kind.

## What this does and does not license

- It does **not** support "coverage prevents defects". That claim remains
  unsupported, now with a direct measurement against it.
- It does **not** show the document is harmful: no tunnel vision, no false
  positives.
- It **does** say the goal statement "md-domain can effectively create CLAUDE.md
  files that inform code review" is, on this evidence, **not demonstrated at the
  level of review OUTCOMES** -- while remaining true at the level of producing an
  accurate, anchored, correctly-placed document, which was link 3.
