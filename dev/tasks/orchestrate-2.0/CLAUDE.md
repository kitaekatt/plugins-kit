# Project Overview

Replace the `orchestrate` skill's rendered policy -- prose characterising each
model tier -- with a decision tree DERIVED from an auditable principles source.
Shipped in awesome-kit 0.22.0; the remaining work is the drift guard, the
machine-half tightening, and the SKILL.md pass.

## Where we are today

The reshape is built, reviewed, committed and pushed. `awesome-kit 0.22.0`
(`23ef7f3` on `dev`) renders the policy as a decision tree resolved by ordered
elimination, stated in a controlled vocabulary. 523 tests green.

- **Live source of truth is still the YAML.** The renderer reads
  `plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml`. The
  principles and lexicon under `docs/planning/orchestrate-2.0/` are the design
  source but nothing reads them -- the derivation is currently a human/agent
  step, not a build step. Closing that gap is `drift-guard`.
- **Schema 2.** Decision half is `resolution`, `lexicon`, `shape`, `backend`,
  `ladders`, `agent_types`, `effort`, `announce`. Machine half (`backends`,
  `capacity`) and the whole layering half are unchanged.
- **Schema-1 overrides are inert.** `tiers`, `default_tier`,
  `backend_selection`, `implementation`, `pool_economics` are no longer read.
  The rendered footer prints a "Stale override -- NOT IN FORCE" warning naming
  the keys and the layer.
- **Measured, whole render:** 3,204 -> 2,547 tokens with Codex; 1,861 -> 1,447
  without. Decision half alone: 1,635 -> 1,342 and 1,289 -> 1,003.
- **The Codex model ids were broken on master until 0.21.1.** `terra` / `sol`
  are not dispatchable; `gpt-5.6-terra` / `gpt-5.6-sol` are. Verified by real
  dispatch, not inspection.

### Environment

- cwd: the repo root (`plugins-kit`). All paths below are repo-relative.
- Platform: Windows. Git Bash for POSIX, PowerShell available.
- Branch `dev`. `dev` and `origin/master` are level as of 0.22.0's publish.
- Tests: `uv run --extra dev pytest tests/awesome-kit/ -q` (523 pass, 1 skip).
- Render the policy with the plugin venv python, not `uv run python`:
  `~/.claude/plugins/data/plugins-kit/awesome-kit/.venv/Scripts/python.exe plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py`
- Simulate "Codex absent" by stripping PATH:
  `env PATH="/c/Windows/System32:/c/Windows" <same python> <same script>`
- Token counts: `uv run --no-project --with tiktoken python` with `o200k_base`.

## Where we want to get to

The rendered tree is mechanically derived from the principles, and it cannot
silently disagree with them. Falsifiable:

1. A check fails when the rendered tree changes without a corresponding
   principles change (`drift-guard`).
2. The principles and lexicon live in the skill's `references/`, not in
   `docs/planning/`, because the renderer reads them.
3. The machine half and SKILL.md have had the same partition applied, each
   measured as its own change.

## Immediate Priorities

Live menu: `task items` (plan.md's task_items block is the source of truth).

- `review-and-commit` is the blocker on everything else. Three changes sit
  UNCOMMITTED in a tree contaminated by other sessions' work
  (`plugins/bootstrap/`, `plugins/skills-kit/`, `marketplace.json`, a second
  task folder). Stage by explicit path; run `/git-code-review` on the staged
  set. This folder itself is UNTRACKED -- commit it too.
- Two version bumps, not one (user decision): the machine-half prose change and
  the SKILL.md partition are measured against different baselines, and folding
  them produces exactly the mixed-basis figure this project already shipped once.
- `promote-design-docs` is unblocked but now COUPLED to the drift guard, which
  hardcodes `docs/planning/orchestrate-2.0/` and stores its baseline there.
  Move the docs and the baseline and the path together, or the guard keeps
  passing while gating nothing.
- Standing warning: any change to a model identifier must be validated by an
  ACTUAL dispatch before it ships. The `terra`/`sol` defect survived because
  the policy is prose nobody executes.

## Project vocabulary

- **rung** -- a step on a ladder (was "tier"). The vocabulary changed in prose
  but NOT in the config key: `capacity.tier_overrides` keeps its name for
  override back-compat, and the rendered capacity section still says "Manual
  tier overrides". Do not "fix" one without the other.
- **ladder** -- one ordered rung list per backend. Rungs compare within a
  ladder, never across.
- **decision half / machine half** -- the two halves of `orchestration.yaml`.
  Decision is derived from principles; machine is what this box has.
- **`[skill]` / `[concept]` term** -- a `[skill]` term selects a branch and
  renders; a `[concept]` term justifies a choice already made and never does.
- **glossed / bare** -- whether a term renders with its gloss. Glossed terms
  gloss at FIRST occurrence in document order, bare after.
- **item** -- the unit below the task, in plan.md's `task_items` block.
- **hand-off** -- the task TYPE in `task.yaml`, not the skill name.

## Protocols

### Always-invoke skills (BEFORE any doc reads)

Invoke every `Skill(...)` line `task work` emitted, in the order printed,
before reading any doc. That block is the complete required set
(`task.yaml`'s `skills_to_invoke` plus the baseline); there is no second list
to consult. Skill invocations are pre-authorized -- do not ask.

### Required reads on turn 1

- `plan.md` (this folder).
- `docs/planning/orchestrate-2.0/README.md` -- orients the design set in ~90
  lines and carries the measurement table.

`log.md` is on-demand: read it for why a decision went the way it did.

### Opening response protocol

After the required reads, before tool use:

> "Invoked: <skills, as emitted by `task work`>. Read plan.md + the design
> README. Current goal: <restated in own words>. Starting with: <first
> concrete action>, dispatched to <sub-agent / inline, with the reason>.
> Unclear / blocked on: <issue, or 'none'>."

### Communication protocol

Default to the three-part end-of-turn template. Project-specific overrides:

- State measured numbers with their basis. This work has already shipped one
  overstated figure (a decision-half number compared against a whole-render
  baseline); say which is which every time.
- When relaying a sub-agent's findings, say what was verified independently
  and what is being taken on report.

## Behaviors

### Autonomy status

The user is engaged and reads turns. They make the publish call themselves.
Design decisions are collaborative -- surface trade-offs with a recommendation
rather than asking open questions.

### Authorizations

- Editing anything under `plugins/awesome-kit/skills/orchestrate/` and
  `tests/awesome-kit/`.
- Running the test suite, rendering the policy, dispatching `codex exec`.
- Committing and pushing to `dev`.
- NOT pre-authorized: publishing to `master` (the user runs `publish.py`), and
  version bumps beyond what a commit needs.

### Rules to follow

- **Principles first, then derive.** Change a principle in
  `docs/planning/orchestrate-2.0/tier-principles.md`, THEN re-derive the tree.
  Never edit the rendered tree and back-fill a principle to match it.
- **Clean-room derivation is the test.** Give a sub-agent ONLY the principles
  and lexicon and have it derive the artifact; its gap report is the finding.
  An author cannot test their own specification -- they fill gaps from memory.
- ASCII only in tracked files. No absolute paths in artifacts.
- Targeted test runs (`tests/awesome-kit/`), not the full suite.
- `uv run python` in shell scripts, never bare `python`.

### Sub-agent orchestration -- main-context preservation

Dispatch generation; keep conclusions. Reads-a-lot / emits-a-lot work goes to
a background agent even when it is easy. Specifically here: derivations,
implementation against a settled spec, and any pass over the 643-line
principles file.

Main context holds: the design decisions, the measured numbers, and what has
been independently verified vs taken on report.

### Anti-patterns to avoid

- **Quoting a decision-half number against a whole-render baseline.** Already
  shipped once. The machine half is ~1,205 tokens with Codex and was
  deliberately out of scope, so any "total" claim must include it.
- **Trusting a green suite.** 512 tests passed over four real defects,
  including one that silently widened the gate on the most expensive rung.
- **Fixing a rung without sweeping its dependents.** Deleting the haiku rung
  left a `render: required` guard attached to a rung that no longer existed
  and a `[skill]` term that selected nothing. Criteria, guards, render tags,
  vocabulary and overturn conditions are all dependent text.
- **Asserting a test rather than checking it.** Two tests here could not fail.
  A negative assertion needs a positive control.
- **Editing the rendered tree directly.** It is generated.

## Relevant files

### Project folder

- `CLAUDE.md` -- this file; auto-loaded.
- `plan.md` -- accomplished + the `task_items` block; read on turn 1.
- `log.md` -- decision rationale and dead ends; on-demand.

### External files

#### The design source (not read by any code yet)

- `docs/planning/orchestrate-2.0/README.md` -- orientation, the measurement
  table, the derivation method, remaining work.
- `docs/planning/orchestrate-2.0/tier-principles.md` -- the criteria, with
  rationale, dated prices, and an explicit ledger of what is not known
  (section 7). 643 lines; dispatch passes over it.
- `docs/planning/orchestrate-2.0/lexicon.md` -- the controlled vocabulary:
  per-term test, `[skill]`/`[concept]`, `render: bare|glossed`, anti-terms.

#### The live skill

- `plugins/awesome-kit/skills/orchestrate/defaults/orchestration.yaml` -- what
  the renderer actually reads. Schema 2.
- `plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py` --
  the renderer. Layering half must stay semantically stable for overrides.
- `plugins/awesome-kit/skills/orchestrate/references/configuration.md` -- the
  user-facing schema doc, including the schema-1 migration table.
- `plugins/awesome-kit/skills/orchestrate/SKILL.md` -- 2,612 tokens, unaudited.
- `tests/awesome-kit/test_orchestration_guidance.py` -- 523 tests.

#### Repo conventions

- `CLAUDE.md` (repo root) -- publish flow, the cache-keys-on-version rule, the
  gotchas. `plugins/CLAUDE.md` -- plugin implementation conventions.
