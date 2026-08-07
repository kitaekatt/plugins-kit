# Proposal: closing md-domain's coverage gap

Status: **SUPERSEDED by `review-reconciliation.md`.** Two independent adversarial
reviews (Claude Opus: SOUND WITH REVISIONS; gpt-5.6-sol: SERIOUSLY FLAWED) found
Parts 2, 3 and 5 and the anti-bloat argument unsound as written. Read
`review-reconciliation.md` for the revised plan; this document is retained as the
reviewed subject, NOT as the current proposal. Known errors in it: Part 3
reinvents the existing `P_stale_factual_claim` (`claude-md-standards.md:548`);
M5's "cannot represent" claim is false (`H2_inverted_absence`, `:551-552`);
M2 is wrong for this corpus (the flecs root classifies as code-directory);
CD-4 is at `:422` not `:427`; DIFF-CLEAN is at `audit-lane.md:368-370` not
`:347-349`.

## The problem being solved

A full `skills-kit:md-domain` audit over the flecs-ecs repo (2026-08-07, 4 lanes,
109 files, 229 findings applied) returned clean by its own criteria. Two
independent reviewers, given md-domain's own standards as a lens but NOT its lane
pipeline, then found a substantial set of code-review-relevant hazards the audit
had passed. Triage against source: report A 20 VALID / 4 PARTIAL / 1 REJECTED,
report B 9 VALID / 3 PARTIAL / 0 REJECTED. Full evidence in `findings.md`.

Diagnosis in one paragraph: md-domain audits the documentation as a
self-contained artifact -- its schema, its internal cohesion, and the truth of
the assertions it happens to make -- but never audits the RELATION between the
corpus and the code it exists to serve. Present claims can be falsified, absent
claims cannot be represented at all, placement is judged by md-to-md cohesion
rather than by whether a fact reaches the reader of the file it describes,
fact-checks are satisfied by the nearest prose rather than by behaviour, and an
invariant the corpus asserts is never tested against the code that must honour
it. COMPLIANT therefore means "internally coherent and locally accurate", which
is orthogonal to -- not weaker than -- fit for review.

Six named mechanisms (M1-M6) are set out in `findings.md` with citations. This
document proposes what to do about them.

## Part 1 -- Honest verdict vocabulary

**Mechanism closed:** the framing failure that produced this whole
investigation (a clean result read as "these docs are good for review").

**Change:** redefine what COMPLIANT asserts, and state explicitly in the audit
report that coverage was NOT assessed. `claude-md-standards.md:52` currently
defines COMPLIANT as the absence of FAIL findings; nothing anywhere asks an
outcome-level question.

**Precedent:** the skill already does exactly this for review mode.
`audit-lane.md:347-349` defines DIFF-CLEAN as "a weaker and more honest claim:
*this change introduced no failure*, not *this file is clean*." The same honesty
is available to COMPLIANT and costs nothing.

**Cost:** documentation-only. No new checks, no new findings, no content added to
any consumer's CLAUDE.md.

## Part 2 -- Ambience check

**Mechanism closed:** M3 (nothing audits whether a fact is ambient for the code
it describes).

**Change:** when a doc documents a hazard about a path, verify the doc is an
ancestor of that path. Mechanical; no judgment.

**Evidence it is needed:** `engine/src/{main.c,native_registry.c,platform.h}`
have only the root file as an ancestor, while the facts about them live in
`engine/src/engine/CLAUDE.md:69`, `engine/src/rest/CLAUDE.md:44` and
`sandboxes/AvKrt/CLAUDE.md:18` -- none ambient. `platform.h` has zero mentions
corpus-wide. The native dual-maintenance step is at `sandboxes/HIERARCHY.md:133`,
a sibling tree. The Windows symlink-degradation hazard is at `tools/CLAUDE.md:21`
but not ambient for `web/matrix/`.

**Critical constraint -- the remedy is RELOCATE OR REFERENCE, never COPY.**
`FLECS_SEED` (`sandboxes/avk/CLAUDE.md:40`) is correctly non-ambient for
`engine/tests/` and does not appear there at all; an implementation that
"copies the fact closer" would import an inapplicable contract. Ambience is
directional.

**Cost:** net-negative or neutral on words -- it relocates existing content.

## Part 3 -- Count-shaped claims verified by counting

**Mechanism closed:** M4.

**Change:** a numeric claim must be checked against enumerated code, not against
adjacent prose. `claude-md-standards.md:427` currently asks whether a claim is
"still true **in kind**" -- and a count that is wrong is still true in kind.

**Evidence:** drift that survived the audit in a file it read and edited.
`monkey-baiting/CLAUDE.md:149` says "the eleven native systems" against 19 actual
registrations; `:1399` says 13 tests against 16; `sashimi/CLAUDE.md` says 26
systems against 25; `clients/CLAUDE.md` says 71 clips against 72. The stale
"eleven" is echoed in the source's own comment at `mb_engine.c:429` -- so a
fact-check performed against the nearest human-readable text CONFIRMS the wrong
doc.

**Cost:** bounded and mechanical. Corrects numbers; adds no content.

## Part 4 -- Invariant-vs-code check

**Mechanism closed:** M5.

**Change:** where an ambient doc states a quotable invariant, test the CODE
against it. Reuses H-11's existing verbatim-quote discipline
(`audit-lane.md:88-94`), which today only checks a subject doc against an
ancestor's declared convention (doc-vs-doc).

**Evidence:** root `CLAUDE.md:120` decision #12 "No silent fallbacks", violated
by `native_registry_register` silently no-opping on an unknown sandbox name
(`engine/src/native_registry.c:27-33`). `monkey-baiting/CLAUDE.md:1190`
"Comparability is never assumed", contradicted by `archive.py:491-494` failing
OPEN.

**Note:** in both cases the documentation is RIGHT and the code is WRONG. The
finding emitted is a code finding, not a doc addition. This is a class md-domain
cannot currently represent, because its subject is the doc.

**Cost:** no content added to any doc.

## Part 5 -- Hazard sweep (opt-in)

**Mechanisms closed:** M1 (absent content invisible by construction) and M2
(`classic` files never read source).

**Change:** an opt-in sweep that reads source and surfaces undocumented hazards.
This is the part the skill explicitly excluded: `claude-md-standards.md:400`
calls the CD dimension "a validator over existing claims, not a gotcha crawler --
it does not scan the directory for *new* gotchas to add (that is the authoring
direction; doing it here would be non-idempotent and expensive)".

**Gate -- a mechanical predicate, not "what might a reviewer want".** Every valid
finding in the triage carries a *silent-failure signature*: silent truncation,
silent drop, fail-open, destructive operation on tracked files, or
dual-maintenance. Restricting the sweep to that enumerable set is what answers
the idempotency objection -- the same scan yields the same set.

**Opt-in, mirroring the density lens** (`claude-md-standards.md:458`, "Never runs
by default").

## The anti-bloat argument

The objection -- that this just adds speculative warnings to every CLAUDE.md --
is real, and the skill states it itself. Three answers:

1. **Parts 1-4 add ZERO words to any CLAUDE.md.** They relocate content, correct
   numbers, and report code defects. Four of six mechanisms close without writing
   a single speculative warning.
2. **Part 5 is gated on a mechanical predicate** (the silent-failure signature
   list), which addresses the stated non-idempotency objection on its own terms
   rather than overriding it.
3. **Budget-neutral by construction.** Pair part 5 with lifting M6's SILENT
   carve-out on historical records (`claude-md-standards.md:389-394`, `:555`), so
   hazards DISPLACE port journals rather than accumulating on top of them. In
   flecs-ecs that trade is roughly 325 lines of "(done)" staging notes
   (monkey-baiting Port status 49 lines; sashimi Port decisions 161/605; sashimi
   clients leaderboard 115/380) against the hazards both reviewers independently
   flagged.

## Honest assessment of risk

Part 5 is where the risk lives, which is why it is opt-in. Parts 1-4 are
corrections to checks that already exist and currently under-deliver.

Unresolved: whether part 2's ambience check can be made mechanical enough to
avoid judgment calls at scale, and whether the silent-failure predicate in part 5
is genuinely enumerable across languages or only looks so on this C/Python corpus.

## How this would be measured

`regression-test-design.md` in this folder: per-mechanism recall against the
pre-registered answer key from triage, three negative controls (FLECS_SEED must
NOT be flagged; already-ambient root facts must NOT be re-proposed; the rejected
mb_loop finding must stay rejected), precision count, and a net-line-delta budget
check. The test must run BEFORE any flecs-ecs doc improvements, or the control
corpus is destroyed.
