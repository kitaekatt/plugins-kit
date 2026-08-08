# Spec: the `coverage` lane (opt-in)

Status: SPEC. Replaces the failed "Part 5 hazard sweep". Derived from sol's
cheaper-alternative recommendation, which both reviews' objections point at.

## The insight this rests on

md-domain ALREADY knows how to find undocumented hazards -- in the AUTHORING
direction. `claude-md-standards.md:352-359` and `:448-450` define the
observation kinds an author is supposed to write up: silent-failure modes,
blast radius, build-flag-dependent behaviour, deliberately-wrong-looking code.
The audit direction is what refuses to look (`:416`, "not a gotcha crawler").

So the gap does not need a new hazard taxonomy. It needs the existing authoring
observation kinds run in a REPORTING mode over a subtree, without mutating
anything. That is the whole design.

## Subject and unit

**Unit: `(code subtree, its ambient CLAUDE.md chain)`.** Not a markdown file --
this is the first lane whose subject is code. That is deliberate and is why it
must be a separate lane rather than a criterion inside `audit_claude_md`: the
per-file lanes enumerate CLAUDE.md files, and no criterion can have a subject
its lane cannot enumerate.

Discovery: the user names a directory, or the lane takes the current diff.
There is NO whole-repo default -- an unbounded default is how this becomes
expensive and non-idempotent.

## Exclusions (mandatory, checked before any read)

Skip, and say in the report that they were skipped:

- vendored / third-party trees (`node_modules/`, `vendor/`, `third_party/`,
  `Pods/`, `target/`, `dist/`, `build/`)
- generated trees and files (an existing modality already exists for generated
  content, `claude-md-standards.md:413`)
- symlinked trees that resolve outside the subtree
- nested repositories and submodules (the ancestor walk stops at `.git`, so a
  nested repo's ambient chain is NOT the outer repo's)
- anything already covered by an ambient CLAUDE.md claim that RESOLVES

## Verdict vocabulary -- distinct, never mixed with document compliance

`GAPS-FOUND` | `COVERAGE-ASSESSED`

The lane **never** emits COMPLIANT / NON-COMPLIANT and **never** alters a
document verdict. A file can be COMPLIANT and its subtree GAPS-FOUND at the
same time; those answer different questions, and conflating them is the exact
misread that started this investigation.

## Remediation routing -- the part that inverts the original proposal

For each candidate, route in this order. Documentation is the LAST resort, not
the first:

1. **Is it a bug?** -> the remedy is a CODE fix. Do not document it. sol's
   point, and it is decisive: writing "this silently truncates at 65536" into
   ambient prose FOSSILIZES a defect that should have been made loud. The
   `native_registry` case is already a violation of the project's own stated
   "No silent fallbacks" invariant -- documenting it would enshrine a
   contradiction.
2. **Can it be made loud?** -> the remedy is an assert, an error return, or a
   test. Enforcement beats prose: it cannot go stale and it fails at the right
   moment.
3. **Is the constraint INTENTIONAL and DURABLE?** -> only now is it
   documentation, and it goes where the placement algorithm says (ambient for
   the code it describes), not wherever is convenient.
4. Otherwise -> report and let the human decide. Reporting a candidate is not a
   commitment to document it.

## Cost and idempotency bounds

- Bounded by the named subtree, never the repo.
- A per-run candidate ceiling; when it is hit, SAY SO in the report rather than
  silently truncating (silent truncation in the tool that reports silent
  truncation would be its own joke, and per the repo's own rule a capped run
  must announce the cap).
- Idempotency is NOT claimed. The lane is advisory and JUDGMENT-severity
  throughout, like the density lens. Claiming determinism for LLM hazard
  selection is the specific overreach that failed review -- the honest posture
  is "advisory, re-runs may differ, nothing auto-applies".

## What it deliberately does NOT do

- Does not run by default (`density` lens precedent, `:458`).
- Does not auto-apply anything.
- Does not add content to any CLAUDE.md by itself.
- Does not claim to be exhaustive. Two thorough reviewers sampled the same
  corpus and found largely DIFFERENT hazards; a report from this lane is a
  sample, and the report must say so.

## Validation gate before it ships

Precision on held-out corpora in at least one non-C/Python language family,
plus the near-miss control: a fixed-cap or dual-maintenance-looking construct
that IS documented, fails loudly, and is test-enforced must NOT be reported
(flecs supplies one in the `MB_HAZARD_CAP` / `MB_MAX_CHARS` contrast).

Recall on the flecs answer key is the WEAKER measure and must not be reported
alone -- the key derived the design, so recall is near-1 by construction.
