# Project Overview

## Where we are today

repo-sync -- the fleet's working-repo synchronizer (clone missing repos,
pull/push clean ones, nag about forgotten states) -- is a user-side contract
script, NOT a bootstrap capability. A 2026-07-22 three-repo investigation
(claude-settings, plugins-kit, env-config) established the current anatomy:

- The script: `~/.claude/scripts/env/repo-sync.sh` (~527 lines), invoked as
  an opaque `check|fix` shell string by the bootstrap env_checks engine via
  the `repo-sync` entry in `~/.claude/env.json`. Untested (claude-settings
  has no test convention); has shipped two field bugs (wrong-root clone from
  a `~/Dev` fallback; CRLF skip_repos parsing).
- Policy data: `~/.claude/config/repositories.yaml` (repo list + per-repo
  `sync: auto|report|skip`) and `env.json` `machines.<host>.skip_repos`.
- Bootstrap's own git code (`git_deps` clone-once provisioning, marketplace
  clone lifecycle) is a DIFFERENT concern: pull-only, never pushes, no
  dirty-tree policy, never reads repositories.yaml. No user-repo sync logic
  exists anywhere in the plugin; `git log --all -- '*repo-sync*'` here is
  empty.
- env-config's involvement is vestigial: `--sync` is a one-line delegation
  to `repo-sync.sh fix`; `--update-repos` runs per-repo update_scripts from
  the same registry. Neither needs to live there.
- IN FLIGHT ELSEWHERE: the script has an UNCOMMITTED "nag razor" rewrite in
  the `~/.claude` working tree (age-gated nagging: 24h dirty/diverged, 4h
  stuck, CWD-repo exemption). That rewrite must land and prove itself
  before this task's implementation starts -- see the gate below.

### Environment

- cwd: `D:/dev/plugins-kit` (this repo; task folder is
  `dev/tasks/bootstrap-absorb-repo-sync`).
- Windows 11 primary dev box; fleet spans macOS/Ubuntu/Windows machines.
- Repo conventions apply in full: tests mandatory under `tests/bootstrap/`,
  version bump + `publish.py` (dev -> master) for every change to reach
  consumers, plugin boundaries are hard boundaries. See repo-root CLAUDE.md.

## Where we want to get to

Bootstrap owns working-repo synchronization as a first-class, tested engine
feature; the user layer owns only policy data. Done when:

1. The engine has a declarative repos/sync feature (or equivalent) that
   clones missing repos, pulls/pushes clean on-branch repos, refuses dirty/
   diverged/mid-operation trees, and reports at appropriate volume -- with
   tests in `tests/bootstrap/` covering the state classification.
2. Policy stays user-side as data: repo list + sync policy, per-host
   skip_repos, and the nag thresholds as configuration, not code.
3. `~/.claude/scripts/env/repo-sync.sh` and the `repo-sync` env_check entry
   are retired; env-config's `--sync` lever is removed or re-pointed.
4. Behavior parity with the tuned nag razor is preserved (the tuning is a
   requirement to carry forward, not to redo).

## Immediate Priorities

Live menu: `task items` (plan.md's task_items block is the source of truth).

- HARD GATE: `nag-razor-signoff` -- the user must confirm the nagging
  behavior of the current script has been tuned sufficiently before any
  implementation begins here. The uncommitted rewrite in `~/.claude` has to
  land, run across the fleet for a while, and be judged quiet-but-honest.
  Until then only `design-repos-feature` (paper design) may proceed.
- Then `design-repos-feature`, then `implement-engine-feature`, then
  `cutover-and-retire`.

## Project vocabulary

- **repo-sync**: the working-repo synchronizer for the user's fleet repos
  (env-config, plugins-kit, llm-dev...). NOT bootstrap's `git_deps` (pinned
  read-only tool deps, clone-once) and NOT marketplace clone lifecycle --
  those resemble it but are provisioning concerns.
- **nag razor**: the reporting policy in the script's header -- a state is
  reported only if it is (1) not self-evident and (2) won't clear itself;
  age thresholds AGE_DIRTY/AGE_STALE 24h, AGE_STUCK 4h; the repo the
  session is standing in (CWD repo) is exempt from age-gated nags.
- **contract script**: the env_checks packaging convention -- one file with
  `check|fix` verbs, user-owned, invoked as an opaque shell string; engine
  re-runs check after fix as the authority ("no trust exceptions").
- **claude-settings**: the `~/.claude` repo, the fleet root, cloned first
  on every machine.
- **fleet data vs machine data**: repositories.yaml and env.json are
  tracked fleet data; `$DEVROOT` and skip_repos values are per-machine.

## Protocols

### Always-invoke skills (BEFORE any doc reads)

- `Skill(bootstrap:bootstrap)` -- engine/manifest vocabulary this work
  designs against.

### Required reads on turn 1

1. `plan.md` -- accomplished + next concrete actions.
2. `investigation-2026-07-22.md` -- the findings this task is built on
   (read before the design step; skim otherwise).

### Opening response protocol

After invoking the always-invoke skills AND reading the required docs above,
BEFORE any tool use, end the first turn with:

> "Read plan.md. Current goal: <restated in own words>. Starting with:
> <first concrete action>. Unclear / blocked on: <issue, or 'none'>."

### Communication protocol

`/verbose-updates` three-part end-of-turn template:

> What changed: <action + paths>.
> Where it sits: <relation to in-flight work>.
> Required user action: <one decision OR "All requested work complete -
> ready to end session">.

## Behaviors

### Autonomy status

- User returns cold between sessions; design work may proceed autonomously,
  but the `nag-razor-signoff` gate is a USER decision -- never mark it
  passed on inference.

### Authorizations

- Read-only investigation across `~/.claude`, `$DEVROOT/env-config`, and
  this repo is standing-authorized.
- No implementation commits until the gate item is cleared by the user.

### Rules to follow

- ASCII only in all files; no absolute paths in the artifacts (use
  `~/.claude/...` and `$DEVROOT/...` anchors).
- Every engine change lands with tests and a version bump; publish via
  `publish.py` per repo-root CLAUDE.md.
- Policy belongs in data, mechanism in the engine: no repo names, paths, or
  thresholds hardcoded in plugin code.

### Sub-agent orchestration -- main-context preservation

- Push bounded heavy work (cross-repo greps, history mining, doc sweeps) to
  background sub-agents; main reads reports, not inputs.

### Anti-patterns to avoid

- Silent-go-to-work: do the opening response protocol before tools.
- Rebuilding the nag policy from scratch in the engine -- the tuned script
  IS the spec; port its classification table, do not reinvent it.
- Treating `git_deps` as the extension point -- it is clone-once
  provisioning with different semantics (no push, no dirty policy); a new
  feature, not a git_deps flag.
- Starting implementation "just a little" before the gate clears.

## Relevant files

### Project folder

Contents of `dev/tasks/bootstrap-absorb-repo-sync/` -- this task's own
working tree.

- `CLAUDE.md` -- self (this file); auto-loaded orientation.
- `plan.md` -- accomplished + forward overview (its task_items block is the
  open-item menu); required read on turn 1.
- `investigation-2026-07-22.md` -- condensed findings of the three-repo
  investigation (anatomy, history, seam assessment); the design input.
- `log.md` -- on-demand history.
- `task.yaml` -- the structured task record (status lives here).

### External files

#### The thing being absorbed (user side)

- `~/.claude/scripts/env/repo-sync.sh` -- the contract script; its header
  comment documents the nag razor and refusal rules; currently carries the
  uncommitted rewrite.
- `~/.claude/config/repositories.yaml` -- repo registry + sync policy;
  stays user-side as data.
- `~/.claude/env.json` -- `repo-sync` env_check entry (with
  agent_instructions) and `machines.<host>.skip_repos`.
- `~/.claude/docs/fresh-machine.md` -- fresh-machine doctrine; already
  stale about repo-sync (lists a dropped repo, pre-rewrite behavior); must
  be updated at cutover.

#### Engine side (this repo)

- `plugins/bootstrap/bootstrap_lib/engine.py` -- env pass + env_checks
  dispatch (`_env_phase_env_checks`); where or beside which the new feature
  lands.
- `plugins/bootstrap/bootstrap_lib/env_features.py` -- `run_env_command`,
  timeout, last-line output capture (the one-line channel the nag razor
  works around).
- `plugins/bootstrap/bootstrap_lib/git_dep_check.py` -- the clone-once
  git_deps machinery; a semantics reference, not an extension point.
- `plugins/bootstrap/skills/bootstrap/references/manifest-reference.md` --
  env_checks contract spec; gains the new feature's docs.
- `tests/bootstrap/` -- where the feature's tests go.

#### Downstream

- `$DEVROOT/env-config/python/env_manager/scripts/update.py` -- `--sync`
  (delete at cutover) and `--update-repos` (decide its new home).
