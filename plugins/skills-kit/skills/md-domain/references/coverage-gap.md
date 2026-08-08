# The coverage gap: what a clean audit does not tell you

What md-domain does NOT check, why, and the one design that would close it.
Read this before proposing a change that makes the audit "look harder", and
before reading a COMPLIANT verdict as an endorsement.

## The gap, stated once

md-domain audits documentation as a self-contained artifact -- its schema, its
internal cohesion, and the truth of the assertions it makes -- but does not
audit the RELATION between the corpus and the code it exists to serve. Present
claims can be falsified; absent claims cannot be represented at all. Placement
is judged by md-to-md cohesion rather than by whether a fact reaches the reader
of the file it describes. Fact-checks are satisfied by the nearest prose rather
than by behaviour. An invariant the corpus asserts is not tested against the
code that must honour it.

COMPLIANT therefore means "internally coherent and locally accurate", which is
orthogonal to -- not weaker than -- fitness for review. A thin file that says
little, accurately, is COMPLIANT.

## How this was established

A full audit over a C/Python project (4 lanes, 109 files, 229 findings applied)
returned clean. Two independent reviewers, given these standards as a lens but
NOT the lane pipeline, then found a substantial set of code-review-relevant
hazards the audit had passed: a test script deleting git-tracked fixtures, a
query path silently dropping unregistered names, 39 bare `65536` literals at
73% of capacity, a parser coupled to a generated file's indentation. Triage
against source: 29 findings verified valid across the two reports, 1 rejected.

The misses were confirmed DETECT-side, not remediation-side: no declined-finding
frontmatter existed anywhere in the corpus and no audit commit mentioned a
suppressed finding. Two adversarial reviews of the resulting proposal (one
Claude, one gpt-5.6-sol) rejected three of its five parts.

## The six mechanisms

1. **Absent content is invisible by construction.** Section 3 states the CD
   dimension is "a validator over existing claims, not a gotcha crawler", and
   the detect lane repeats it operatively. Of ~40 rules that can fire on a
   CLAUDE.md, four can fire on silence (H-1/H-2/H-3, A-4), and all four concern
   the doc's self-description or a fact's location. CD-6 is the near miss: it
   detects EROSION from a prior state, so a file that was always thin passes.
2. **The classic/code-directory split withholds source from part of the
   corpus.** A `classic` file never triggers a code read at all.
3. **Nothing audits ambient coverage.** No rule walks outward from a source file
   to ask whether an ancestor CLAUDE.md covers it. A fact can be accurate,
   resolvable, and unreachable -- documented in a sibling subtree that never
   loads for the reviewer who needs it. This is the mechanism with NO
   implementation; the other five are addressed or scoped below.
4. **"True in kind" excluded counts** -- addressed 2026-08-07 by the
   exact-enumeration typing (A-3 count claims + the CD-4 narrow exception).
5. **Code violating a stated invariant was unrepresentable** -- addressed
   2026-08-07 by CD-2b.
6. **The value filter is SILENT on exactly the content that bloats** --
   historical records are a carve-out, so port journals consuming 27-30% of a
   file are not flagged. Unaddressed; lifting the carve-out is a default-on
   behavior change for every consumer and was deliberately not bundled.

## Why the obvious fix is wrong

The instinct is a hazard sweep: read the code, enumerate what the docs fail to
mention. Both reviewers rejected it, on grounds worth preserving.

- **The predicate is not enumerable.** A five-label list (silent truncation,
  silent drop, fail-open, destructive-on-tracked-files, dual-maintenance) is a
  restatement of this file's own value lattice, which the standards already
  grade JUDGMENT. Non-idempotency lives in the CANDIDATE set -- which of ~10^4
  source constructs get surfaced -- not the label set. In TypeScript "silent
  truncation" is every unchecked `as`; in Go every `_ =`.
- **Budget does not net out.** Ambient load is PER FILE. Deleting 161 lines from
  a deep file creates no room in the file that needs the warning.
- **Documenting a hazard can fossilize a bug.** Several of the strongest
  findings were defects whose right remedy was a code fix or a loud failure, not
  ambient prose describing them as behaviour to preserve.

## The design that would close it

An opt-in `coverage` lane, distinct from document compliance:

- **Unit:** `(code subtree, its ambient CLAUDE.md chain)` -- the first lane whose
  subject is code. This is why it cannot be a criterion inside `audit_claude_md`:
  the per-file lanes enumerate CLAUDE.md files, and no criterion can have a
  subject its lane cannot enumerate.
- **Discovery:** a named directory or the current diff. No whole-repo default.
- **Exclusions, checked before any read:** vendored, generated, symlinked-out,
  nested repos and submodules (the ancestor walk stops at `.git`, so a nested
  repo's ambient chain is not the outer repo's).
- **Verdict vocabulary:** `GAPS-FOUND` / `COVERAGE-ASSESSED`. Never emits
  COMPLIANT/NON-COMPLIANT and never alters a document verdict. A file can be
  COMPLIANT and its subtree GAPS-FOUND simultaneously.
- **Remediation routing, in order:** is it a bug (fix the code); can it be made
  loud (assert, error return, test); is the constraint intentional and durable
  (only now document it, placed per the placement algorithm); otherwise report
  and let a human decide.
- **Honest posture:** advisory, JUDGMENT throughout, idempotency NOT claimed,
  a per-run candidate ceiling that is ANNOUNCED when hit.
- **Reuses what exists:** the authoring direction already defines the
  present-and-silent observation kinds. The gap is that the audit direction
  refuses to look, not that the vocabulary is missing.

## Validation discipline this established

Criteria changes here are validated on a HELD-OUT corpus before shipping, not
after. The 2026-08-07 changes were measured twice, and both runs changed the
rules:

- The regression run caught a blocking defect -- enumeration depth was
  unspecified under an APPLIED FIX, so a lane could write a NEW wrong number
  into a doc, which is worse than the stale one it replaced.
- A held-out C#/C++ run produced 2 false positives that became the closed-set
  and unit-ambiguity gates. Neither could have been derived from the C/Python
  corpus the rules were designed against.

Recall against a corpus that DERIVED the design is near-1 by construction and
must never be reported alone. Precision on held-out material is the measure
that means something.

**Open:** no TypeScript, Rust, or Go corpus has been tested. Generality across
those families is unvalidated.
