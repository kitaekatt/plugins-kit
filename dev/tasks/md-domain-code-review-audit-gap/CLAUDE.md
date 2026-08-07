# Project Overview

## Where we are today

A full `skills-kit:md-domain` audit was run against the flecs-ecs repo on 2026-08-07 --
all four artifact lanes, 109 files, 7 commits on branch `md-audit/skills-compliance`
(flecs-ecs `16f373b`..`d2073ee`). It applied 229 findings and produced a clean result by
its own criteria: 9/9 SKILL.md mechanically clean, cross-reference scan 0 errors /
0 warnings, one SERIOUS finding surfaced and resolved.

Immediately afterwards, TWO independent assessments were commissioned on the SAME
question the audit had just finished answering -- is the CLAUDE.md coverage sufficient to
support effective CODE REVIEW? Both were told to use md-domain's standards as a lens but
NOT to run its lane pipeline, precisely so they would not reproduce its blind spots:

- `report-opus.md` -- Claude Opus via the Agent tool (COMPLETE)
- `report-sol.md` -- gpt-5.6-sol via Codex CLI at max effort, a different model family
  (see plan.md for status)

Opus's verdict: **coverage is NOT sufficient for code review**, and -- this is the part
that matters here -- **the residual problem is not staleness**. It re-resolved ~40 cited
paths, symbols and constants across the audited docs and found essentially no rot left.
What it found instead was misallocation: the highest-risk surfaces carry no ambient
coverage at all, while ambient budget goes to port journals. Its examples include a test
script that `rm -rf`s tracked fixtures with nothing documenting it, a Lua query path that
silently drops unregistered component names, 39 bare `65536` literals at 73% of capacity
with no `#define`, and a difficulty parser coupled to the exact indentation of a
generated file.

**The audit passed every one of those.** That is the finding this task exists to explain.

This task is NOT about fixing flecs-ecs. It is about md-domain: a full, clean audit of a
project should not be able to coexist with that many undocumented, code-review-relevant
hazards. Something in what md-domain looks for -- or in how its CD (code-directory)
dimension defines a finding -- does not reach them.

### Environment

- cwd: `D:/dev/plugins-kit` (this repo, branch `dev`). The skill under investigation is
  `plugins/skills-kit/skills/md-domain/`.
- The audited project is a SEPARATE repo: `D:/dev/flecs-ecs`, branch
  `md-audit/skills-compliance` (pushed). Read-only for this task.
- The audit's own commits are the evidence trail: `git log 16f373b..d2073ee` in flecs-ecs.
- Windows box; use the plugin venv python for any skills-kit script
  (`~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe`).

## Where we want to get to

`skills-kit:md-domain` audits a project's CLAUDE.md corpus well enough that a clean audit
result is a MEANINGFUL signal for code review -- i.e. a reviewer working from the audited
docs is materially less likely to approve a defective change.

Concretely, the task is done when we can state, with evidence:

1. Which of the two reports' deficiencies are VALID (verified against flecs-ecs source),
   and which are noise or judgment calls we reject.
2. WHY the audit did not surface each valid one -- traced to a specific, named cause in
   md-domain (a criterion that does not exist, a criterion scoped too narrowly, a lane
   that never reads source, a taxonomy with no bucket for it, a disposition that silences
   it, an instruction that steers the lane elsewhere).
3. The conceptual gap those causes share, stated in one paragraph.
4. A proposed change to md-domain that closes it -- and, just as important, an argument
   for why the change does not simply bloat every CLAUDE.md with speculative warnings.

## Immediate Priorities

Work the items in `plan.md`'s `task_items` block, in priority order. Start with
`triage-report-findings` -- nothing downstream is trustworthy until the valid/invalid
split is settled against real source.

## Project vocabulary

- **the audit** -- the 2026-08-07 md-domain run over flecs-ecs, commits
  `16f373b`..`d2073ee`.
- **CD dimension** -- md-domain's code-directory criteria for CLAUDE.md
  (`references/standards/claude-md-standards.md` section 3): the shapes, observation
  kinds, anchoring discipline and the CD-5 value filter.
- **anchor** -- a concrete citation in a doc (file path, symbol, line, constant). The
  audit checked whether anchors RESOLVE and whether claims about them HOLD.
- **the two reports** -- `report-opus.md` and `report-sol.md` in this folder.
- **valid deficiency** -- a report finding verified against flecs-ecs source, that a
  reviewer would plausibly be harmed by not knowing.

## Protocols

### Always-invoke skills (BEFORE any doc reads)

- `skills-kit:md-domain` -- the subject of the investigation. Read its
  `references/standards/claude-md-standards.md` (esp. section 3, the CD dimension) and
  `references/lanes/audit-lane.md`. Do NOT run the lane pipeline against flecs-ecs again;
  the question is what the pipeline MISSES, and a second run reproduces the same misses.

### Required reads on turn 1

1. This file.
2. `plan.md` (the item menu).
3. `report-opus.md` and `report-sol.md` -- the evidence.

### Opening response protocol

State which report findings you are triaging first and how you will verify them (source
paths in flecs-ecs), before editing anything.

### Communication protocol

Report the WHY-analysis as a causal chain, not a list of complaints: finding -> the
md-domain criterion that should have caught it -> why it did not. A conclusion that
md-domain "should look harder" is not an answer; name the mechanism.

## Behaviors

### Autonomy status

Analysis and authoring in this folder: autonomous. Changes to
`plugins/skills-kit/skills/md-domain/` itself: propose first -- this is a published
plugin and consumers inherit it.

### Authorizations

- Read anything in `D:/dev/flecs-ecs` (read-only; make no commits there).
- Run flecs-ecs's own tests/scripts read-only to verify a claim, EXCEPT
  `engine/tests/test_rest.sh`, which deletes tracked fixtures (that is itself finding 1
  in `report-opus.md`).

### Rules to follow

- Verify before accepting. Both reports were told to verify; check a sample anyway, and
  reject anything that does not hold. A wrong finding that drives a skill change is worse
  than a missed one.
- ASCII only in this folder's docs; repo-relative paths.
- Do not re-run the UNCHANGED md-domain audit over flecs-ecs to "check" -- it reproduces
  its own misses (see the protocol above). This does NOT bar item `regression-audit-flecs`:
  running the CHANGED pipeline against a known answer key is a test of the fix, not a
  reproduction of the miss. Distinguish the two by which md-domain is running.

### Sub-agent orchestration -- main-context preservation

Triage and verification are read-a-lot / conclude-small: delegate per
`awesome-kit:orchestrate`. The causal analysis (item `diagnose-why-audit-missed`) is the
judgment core -- keep it in the main context or give it a high-reasoning tier, and name
the model explicitly.

### Anti-patterns to avoid

- **Fixing flecs-ecs BEFORE the diagnosis lands.** flecs-ecs is the EVIDENCE; editing it
  during the investigation destroys the state the analysis is reasoning about. Improving
  those docs IS in scope now (user direction 2026-08-07) but only as the final item
  `improve-flecs-docs`, after the regression audit. Until then flecs-ecs stays read-only.
- **Concluding "the auditors were just better".** Both reports ran with md-domain's own
  standards in hand. If a fresh reader with the same standards finds what the lane
  misses, the difference is in the PROCESS, and that difference is the deliverable.
- **Accepting the reports' framing wholesale.** They were briefed by the same person who
  ran the audit; shared assumptions are possible. Where the two reports DISAGREE is
  unusually informative -- treat disagreements as findings, not noise.

## Relevant files

### Project folder

- `CLAUDE.md` -- this file; orientation and protocol.
- `plan.md` -- the `task_items` menu and forward overview.
- `log.md` -- dated history.
- `report-opus.md` -- assessment A (Claude Opus, Agent tool). Complete.
- `report-sol.md` -- assessment B (gpt-5.6-sol, Codex CLI, max effort).

### External files

- `plugins/skills-kit/skills/md-domain/` -- the skill under investigation (this repo).
  - `references/standards/claude-md-standards.md` -- section 3 is the CD dimension.
  - `references/lanes/audit-lane.md` -- the DETECT/gate/REMEDIATE pipeline.
  - `workflow/claude-md-detect.js` -- what a detect lane is actually told to look for.
- `D:/dev/flecs-ecs` @ `md-audit/skills-compliance` -- the audited project (read-only).
  - `git log 16f373b..d2073ee` -- the audit's own record of what it did find.
