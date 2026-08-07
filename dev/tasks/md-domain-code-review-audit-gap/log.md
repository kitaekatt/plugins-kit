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
