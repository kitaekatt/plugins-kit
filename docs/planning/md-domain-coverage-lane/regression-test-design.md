# Regression test design: does a changed md-domain surface what the clean audit missed?

Status: DRAFT, pending the proposal decision. Durable-output candidate -- if the
md-domain change ships, this belongs in the plugin (a test fixture / reference),
not in this folder.

## Why the design comes before the fix

The answer key exists exactly once and it exists now: the triaged valid-deficiency
list in `findings.md`, established against a flecs-ecs corpus that the unchanged
audit passed cleanly. That corpus is the control. Any edit to flecs-ecs docs --
including the improvements requested for item `improve-flecs-docs` -- destroys it,
because a later audit would then be scored against docs written to satisfy the
change under test. Hence the ordering constraint recorded in `plan.md`:
`regression-audit-flecs` MUST precede `improve-flecs-docs`.

## What is being measured

NOT "does the audit produce more findings". A change that merely raises finding
volume is indistinguishable from the bloat the proposal must survive. The measure
is per-mechanism recall against a fixed, pre-registered answer key, paired with a
precision check that the new findings are the KNOWN ones rather than new noise.

## Pre-registered answer key

Scored per mechanism (M1-M6 in `findings.md`), because the five proposal parts
close different mechanisms and a single aggregate score would hide which part
did the work.

| Mechanism | Expected to be surfaced by | Answer-key instances (from triage) |
|---|---|---|
| M3 ambience | proposal part 2 | `engine/src/{main.c,native_registry.c,platform.h}` uncovered; `sandboxes/HIERARCHY.md:133` non-ambient to `engine/src/`; `tools/CLAUDE.md:21` symlink hazard non-ambient to `web/matrix/` |
| M4 counts | proposal part 3 | `monkey-baiting/CLAUDE.md:149` (11 vs 19); `:1399` (13 vs 16); `sashimi/CLAUDE.md` (26 vs 25); `clients/CLAUDE.md` (71 vs 72) |
| M5 invariant-vs-code | proposal part 4 | `native_registry.c:27-33` vs root `CLAUDE.md:120`; `archive.py:491-494` vs `monkey-baiting/CLAUDE.md:1190` |
| M6 value filter | lifting the historical-record carve-out | monkey-baiting "Port status" (49 lines); sashimi "Port decisions" (161/605); sashimi clients leaderboard (115/380) |
| M1/M2 hazard sweep | proposal part 5 (opt-in) | `test_rest.sh` fixture deletion; Lua silent component drop; `hearts[64]`; 127-coord ceiling; WASM 65,536 truncation; `took_damage` parity; `native_registry` no-op |

## Required negative controls

A recall-only test rewards a change that flags everything. Three controls, each
derived from a case the triage already settled:

1. **FLECS_SEED must NOT be flagged as missing from `engine/tests/`.** It is
   correctly non-ambient there (`sandboxes/avk/CLAUDE.md:40`, avk-specific) and
   does not appear under `engine/tests/` at all. A part-2 implementation that
   proposes copying it has implemented "copy the fact closer" instead of
   "relocate or reference", which is the exact bloat failure.
2. **The already-ambient facts must NOT be re-proposed.** Root `CLAUDE.md:48`
   (test macros) and `:115` (`ecs_iter_fini`) are ambient for `engine/tests/`.
   Re-proposing them reproduces report A's over-broad `engine/tests/` file, which
   the triage rejected in favour of B's narrowing.
3. **The rejected finding must stay rejected.** report-opus #6e ("15 lines of
   glossary") is false; the mb_loop balance-loop section is ~180 lines of good
   hazard prose. A run that flags that section as low-value has mis-implemented
   the value filter.

## Scoring

- Per mechanism: instances surfaced / instances in key.
- Precision: findings emitted that are NOT in the key and NOT defensible on
  inspection. Report the count; do not auto-fail (the key is a floor, not a
  ceiling -- both reports sampled disjointly, so unknown-but-real hazards exist).
- Controls: any control violation is a FAIL for the part it implicates,
  regardless of recall.
- Budget: net line delta across the corpus. Part 5 paired with the M6 carve-out
  lift should be roughly neutral (~325 journal lines available to displace).

## Scope

Targeted, not a full re-run. Restrict to the directories the answer key touches:
`engine/src/`, `engine/src/rest/`, `engine/tests/`, `sandboxes/monkey-baiting/`,
`sandboxes/sashimi/`, `sandboxes/avk/`, `avk/clients/{wasm,console}/`,
`matrix/`, `web/matrix/`, `tools/`.

## Held-out corpus availability (checked 2026-08-07)

Both reviews made held-out precision in a non-C/Python language family a
PRECONDITION for publishing the criteria changes. A scan of `$DEVROOT` found no
qualifying repo: the only candidates with enough CLAUDE.md files
(`christina-norman` 25, `private-plugins` 9) are Python-primary, and
`woodworking-sim` has just one CLAUDE.md.

**Partial substitute, used deliberately:** a large C#/C++ game corpus -- 130
CLAUDE.md files, 1106 `.cs` alongside 1081 `.cpp`. C# is a different type
system and documentation culture from the training corpus (nullable
references, exceptions, LINQ), so it is a real precision test, just not the
TypeScript/Rust/Go one specified. Its C++ half is NOT held out and its results
there must be discounted.

**Residual gap, stated so it is not forgotten:** no dynamic-typed-web
(TypeScript), no ownership-model (Rust), no explicit-error-return (Go) corpus
was tested. The predicate's generality across those families remains
UNVALIDATED, which is the exact overfitting risk both reviewers named. Do not
describe the criteria as validated across languages on the strength of a C#
run.

## Known limitation

The key was built by two LLM reviewers plus four verification units, not by
exhaustive human audit. Recall is measured against what we found, not against
what exists. This bounds the claim the test can support: it can show a mechanism
was closed; it cannot show the corpus is now sufficient for review.
