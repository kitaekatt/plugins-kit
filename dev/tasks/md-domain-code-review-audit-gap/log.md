# Log: md-domain code review audit gap

(no entries yet -- rotate completed-step detail and decision rationale here)
- 2026-08-07: update: priority = 'P1'; description = 'Determine which CLAUDE.md code-review deficiencies found by two independent assessments are valid, then diagnose why a full md-domain audit of the same corpus did not surface them -- the conceptual gap in how md-domain audits for code-review fitness.'; skills_to_invoke = ['skills-kit:md-domain']
- 2026-08-07: update: refresh (no field edits)

## 2026-08-07 (session: review-audit-gap)

- Triage of both reports COMPLETE against flecs-ecs source (4 parallel verification
  units). report-opus 20 VALID / 4 PARTIAL / 1 REJECTED; report-sol 9 VALID /
  3 PARTIAL / 0 REJECTED.
- Disagreements settled from source. B right on engine/src/ and the engine/tests/
  narrowing; A right on the tracked-fixture deletion; B's unique findings are a
  sampling gap in A, not a blind spot.
- Causal analysis COMPLETE: six named mechanisms + the conceptual gap (findings.md).
- Declined-findings check: ZERO md-audit-declined frontmatter, no suppression in
  the 7 audit commits -- the misses are DETECT-side, not remediation-side.
- Two adversarial reviews of proposal.md (Opus: SOUND WITH REVISIONS; gpt-5.6-sol:
  SERIOUSLY FLAWED). Parts 2, 3, 5 and the anti-bloat argument failed by both.
  proposal.md marked SUPERSEDED; review-reconciliation.md carries the revised plan.
- M2 settled by experiment: the flecs root classifies as CODE-DIRECTORY (ran the
  discoverer), but P_stale_factual_claim and A-3 are classic-only, so it is a
  missing-rule SCOPE problem, not a criterion-application failure. Neither
  reviewer had this fully right.
- SHIPPED (dev, f56c7f3): skills-kit 0.39.0, Part 1 -- COMPLIANT is scoped to the
  document criteria that ran; coverage explicitly not assessed. Golden corpus
  green, merge gate adds zero new FAILs.
- SHIPPED (dev, 28918df): .gitignore dev/* + !dev/tasks/ -- dev/ was hiding every
  task folder in this repo from version control, defeating the task system's own
  durability contract. This folder is now tracked.
- Authored coverage-lane-spec.md (replaces the failed Part 5).
- SHIPPED (dev, bea0cf7): skills-kit 0.40.0 -- count typing (P scope extension to
  code-directory, exact-enumeration vs illustrative-magnitude) + CD-2b
  invariant_violated_by_code. Validated BEFORE shipping: flecs regression 6/6
  answer-key rows with all 4 negative controls silent, which caught a blocking
  defect (unspecified enumeration depth under an APPLIED FIX could write a NEW
  wrong number); held-out C#/C++ precision run 4 TP / 2 FP, whose false positives
  produced the closed-set and unit-ambiguity gates. Residual gap recorded: no
  TS/Rust/Go corpus available, generality unvalidated.
- flecs-ecs docs improved on branch docs/coverage-improvements (6 commits, not
  pushed). Control branch md-audit/skills-compliance preserved at d2073ee with its
  dirty-tree evidence intact. Net +42 lines across a ~3,200-line corpus; two
  previously uncovered directories (engine/src, engine/tests) now have ambient
  files. All 4 count claims corrected by independent enumeration.
- NOTE: marketplace.json deliberately left uncommitted -- a regen swept a
  concurrent session's bootstrap 0.72.0 -> 0.73.0 bump. Whoever commits next
  should regen and carry both.
