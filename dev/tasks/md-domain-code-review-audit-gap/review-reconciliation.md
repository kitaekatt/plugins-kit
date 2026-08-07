# Review reconciliation: two independent critiques of `proposal.md`

Two adversarial reviews were commissioned on the same brief (seven axes),
against the same artifacts, by different model families:

- Claude Opus, Agent tool -- verdict **SOUND WITH REVISIONS**. Text folded into
  this document (not retained verbatim).
- gpt-5.6-sol, Codex CLI at xhigh -- verdict **SERIOUSLY FLAWED**. Verbatim in
  `review-sol.md`.

The verdict gap is smaller than it looks: they agree on almost every axis and
differ mainly on whether the salvageable parts justify shipping anything now.

## Where both agree (treat as settled)

| Claim | Both reviewers |
|---|---|
| M1 (absent content invisible by construction) | CORRECT -- the one mechanism that fully survives |
| Part 1 (honest verdict vocabulary) | SHIP IT, first, narrowly worded |
| Part 2 ambience "is mechanical" | FAILS -- ancestry is mechanical only after a human judges that a statement is a hazard *about* a path |
| Part 5 silent-failure predicate | FAILS -- semantic outcomes, not detectable shapes; a finite label list does not bound the candidate set |
| Anti-bloat displacement argument | FAILS -- journals and hazards are not fungible; budget is per-file ambient load, not corpus lines |
| Regression test | FAILS as validation -- the answer key derived the changes it scores; recall is ~1 by construction |
| M5 already representable | BOTH cite `H2_inverted_absence` (`claude-md-standards.md:551-552`) -- "the fix is in the code/repo, not the CLAUDE.md". My "cannot represent" claim was wrong. |
| Counts need a type distinction | CONVERGENT, independently: Opus "contractual vs illustrative", sol "exact-enumeration vs illustrative-magnitude". Same idea, same remedy. |
| My citations | Both flagged the same errors: DIFF-CLEAN is `audit-lane.md:368-370` not `:347-349`; "true in kind" is `claude-md-standards.md:422` not `:427`. |

## Where they disagree (the informative part)

### 1. M2 -- how wrong is it?

- **Opus:** overstated. flecs-ecs's root has no `claude_md:` block, so
  "every root project CLAUDE.md is classic" is false on my own corpus.
- **sol:** wrong outright, with stronger evidence -- it read
  `scripts/discover_claude_md.py:86-124` and reports that `classic` is forced
  only by a schema block or a skill directory, so a run classifies the flecs
  root as **code-directory**. It further notes classic files are not
  source-blind anyway: A-3 checks flags and class names against repo state
  (`claude-md-standards.md:219-227`) and P covers numeric claims (`:548`).

**SETTLED 2026-08-07 by experiment -- and neither reviewer was fully right.**
The discoverer was run read-only against the flecs root: it classifies as
**code-directory** (`discover_claude_md.py:99-100` forces classic only on a
sibling SKILL.md; `:120-121` only on a `claude_md:` block; neither holds, and
Signal A at `:116` fires independently on the CMakeLists/Caddyfile siblings).
So sol correctly overturns Opus on the dimension.

But sol's cited rescue criteria do NOT apply: `P_stale_factual_claim` (`:548`)
is explicitly scoped to "a classic (non-code-directory) CLAUDE.md", and A-3
(`:219-227`) lives under `## 2. Classic standards`. Neither is eligible for
this file. The CD dimension that DOES apply excludes new gotchas by design
(`claude-md-standards.md:416`), and CD-2 stays silent because `python3` and the
ctest suites all resolve fine.

**Therefore: a MISSING-RULE (scope) problem, not a criterion-application
failure.** M1 is vindicated on this file too. Direct consequence for the
count-typing item: the drifted count files are code-directory, and P cannot
see them -- so Opus's "extend P's scope" framing is right and sol's "the rules
existed and did not fire" is wrong.

### 2. M3 -- genuinely unrepresented, or partly covered?

- **Opus:** real and unrepresented; found no rule walking outward from source.
- **sol:** only partly absent -- C-5/R-1 and the placement framework already
  require a fact to live with its reader set (`claude-md-standards.md:108-152`,
  `cohesion-principles.md:201-231`), so the proposal must explain why those
  failed on explicitly misplaced facts.

sol also lands a correction neither I nor Opus made: **`platform.h` has zero
corpus mentions, so it is an M1 case, not an M3 case.** An ambience check cannot
relocate a fact that does not exist. My M3 evidence list conflated the two.

### 3. Part 4 -- ship it or defer it?

- **Opus:** "the best idea in the proposal" -- ship narrowed, gated on the
  verbatim-quote posture (`claude-md-standards.md:309`), disposition SERIOUS.
- **sol:** FAILS as specified -- undefined subject, severity, verdict
  interaction, remediation owner, and review-mode attribution. If the doc is
  right and the code is wrong, marking the doc NON-COMPLIANT is false, and
  leaving it COMPLIANT next to a code failure is confusing. A generalized
  invariant sweep also exceeds md-domain's declared subject (`SKILL.md:14-23`).

**Both are right about different things.** The idea is sound (H2 precedent);
the specification is absent. It ships only once the verdict interaction is
defined -- which is sol's point, and is a prerequisite rather than a refutation.

## What sol raised that nobody else did

1. **Many "undocumented hazards" are simply bugs.** Documenting a fail-open or a
   silent truncation can FOSSILIZE behaviour that should instead be fixed, made
   loud, or removed. `native_registry` is already a violation of the project's
   own stated "No silent fallbacks" invariant (`flecs-ecs/CLAUDE.md:120` vs
   `engine/src/native_registry.c:27`). Any remediation must route
   code-fix / test-enforcement FIRST, and document only when the constraint is
   intentional and durable. This inverts part of the proposal's purpose and is
   the single most important point either review made.
2. **A cheaper path already exists in the skill.** The AUTHORING direction is
   already supposed to find high-value kinds that are "present and silent"
   (`claude-md-standards.md:352-359`, `:448-450`). Expose that as a
   non-mutating, bounded `coverage/suggest` lane over a chosen directory or the
   current diff, reusing the existing placement algorithm
   (`authoring-lane.md:69-97`). Captures most of M1 without inventing a
   universal hazard taxonomy or touching document compliance.
3. **The missing fourth negative control, concretely.** A source-level near
   miss: a fixed-cap or dual-maintenance-looking construct that IS documented,
   fails loudly, and is test-enforced must NOT be reported. flecs already
   supplies one -- the `MB_HAZARD_CAP` / `MB_MAX_CHARS` contrast
   (`report-opus.md:52-58`).
4. **The regression test does not even test M2** -- the M2 examples (guarded
   ctest suites, literal `python3`) are absent from the key, which instead lists
   `native_registry`, an M5 case. And Part 1 has no test at all.

## Independently verified while the reviews ran

Opus's sharpest structural challenge -- that these hazards might have been
DETECTED and then declined or silenced, which would invalidate M1/M2 -- was
checked and **cleared**: zero `md-audit-declined` frontmatter anywhere in
flecs-ecs, no suppression language in any of the 7 audit commits, no report
artifact listing unapplied findings. The misses are detect-side.

One detail from that check strengthens M1: the `65536` literals DO appear in the
audit's diff (`e8f0a1e`), cited as the correct current array size while
documenting the general "undersized buffers silently drop units" rule. The audit
read the constant, wrote the general rule, and did not flag the specific defect
-- operating in the doc's frame (is this value stated correctly?) rather than
the code's (is this value safe?).

## Revised plan

**Ship now (both reviewers endorse):**

1. **Part 1, narrowly worded.** "COMPLIANT means no FAIL under the listed
   document criteria; code-review coverage was not assessed." Reconcile across
   `claude-md-standards.md:50-56` and `audit-lane.md:339-349`.

**Ship after a small specification step:**

2. **Count typing** -- classify claims as `exact-enumeration` vs
   `illustrative-magnitude`, require executable enumeration only for the former.
   Implement as a SCOPE EXTENSION of the existing `P_stale_factual_claim` to
   code-directory files, not as a new rule, with a provenance entry explaining
   why `claude-md-standards.md:369` is being narrowed. (Both reviewers; Opus adds
   that P's real failure is that `claude-md-detect.js` never TELLS the lane to
   enumerate count-shaped claims -- P appears only in the taxonomy enum at
   `:115` and the mapping line at `:254`.)
3. **Part 4** -- only once subject, severity, verdict interaction, and
   remediation owner are defined. Disposition SERIOUS, fix in code.

**Demote to advisory / prototype:**

4. **Part 2** -> advisory finding for explicit out-of-scope path anchors only,
   with exclusions for vendored, generated, symlinked, and nested-repo trees.
   Not a compliance rule, not a completeness check.
5. **Part 5** -> REPLACED by sol's `coverage/suggest` lane: opt-in, non-mutating,
   unit = (code subtree, ambient ancestor chain), distinct verdict vocabulary
   (`GAPS-FOUND` / `COVERAGE-ASSESSED`), never alters document COMPLIANT, and
   routes candidates to code-fix/test-enforcement before documentation.

**Before any of 2-5 is published:** held-out corpora in at least three other
language families (TypeScript, Rust, Go or Python), scored on PRECISION, plus
the near-miss control and a consumer-regression control counting new FAILs on a
corpus that previously passed.

## Open question for the user

The reviews split on whether the M2 misses are a dimension gap or plain
criterion-application failure. Settling it needs one narrow experiment: re-run
ONLY the existing A-3 / P criteria against the flecs root file and see whether
they fire. That is the one place where re-running the unchanged pipeline is
justified, since the question is whether existing rules work, not whether the
pipeline reproduces its misses.
