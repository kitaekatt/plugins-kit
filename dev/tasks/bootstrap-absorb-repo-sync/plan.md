# Plan: Bootstrap absorb repo sync

## Accomplished

- 2026-07-22: Three-repo investigation of repo-sync's anatomy, history, and
  seam; findings condensed into `investigation-2026-07-22.md`. Task created.

## Forward overview

```yaml
task_items:
  items:
    - id: nag-razor-signoff
      title: "User confirms nagging is tuned sufficiently (HARD GATE)"
      state: blocked-user
      priority: P1
      note: "Uncommitted rewrite in ~/.claude must land + prove out; user call only"
    - id: design-repos-feature
      title: "Design the engine's repo-sync feature (mechanism/policy split)"
      state: available
      priority: P1
      note: "Paper design may proceed before the gate; no code"
    - id: implement-engine-feature
      title: "Implement + test the feature in bootstrap"
      state: blocked-user
      priority: P2
      note: "after nag-razor-signoff and design ratified"
    - id: cutover-and-retire
      title: "Retire script + env_check entry; clean up env-config and docs"
      state: deferred
      priority: P3
      note: "after implement-engine-feature ships and converges on the fleet"
```

### nag-razor-signoff -- User confirms nagging is tuned sufficiently

This is the task's explicit precondition, set by the user at task creation:
bootstrap absorbs repo-sync only once the nagging behavior has been tuned
sufficiently. Concretely:

1. The nag-razor rewrite currently uncommitted in `~/.claude`'s working tree
   (`scripts/env/repo-sync.sh`) gets committed and syncs to the fleet.
2. It runs in daily use long enough for the user to judge the volume: quiet
   about self-evident/fresh states, loud about forgotten/stuck ones.
3. The user says so. Do not infer this from the absence of complaints; ask
   via AskUserQuestion when the design is ready and the rewrite has had
   fleet time.

The tuned behavior then becomes the parity spec for the engine feature.

### design-repos-feature -- Design the engine feature

Produce a design doc (in this folder) covering:

- Feature shape: a new declarative env.json feature (working-repo sync),
  NOT an extension of git_deps (different semantics: push-back, dirty-tree
  policy, unpinned branches).
- Mechanism/policy split: engine owns state classification (missing /
  stuck / detached / wrong-branch / dirty / diverged / ahead / behind /
  clean), clone/pull --ff-only/push mechanics, and refusal rules; data owns
  the repo list, sync policy, skip_repos, and nag thresholds
  (AGE_DIRTY/AGE_STALE/AGE_STUCK as config with current values as
  defaults).
- Where the registry lives: keep `~/.claude/config/repositories.yaml` or
  fold into env.json -- decide with migration cost in view.
- Reporting: the current script contorts around the engine's one-line,
  silent-ok/loud-fail channel; decide whether the feature gets a native
  informational tier (per-repo action lines, "fixed silently" reporting)
  instead of porting that contortion.
- agent_instructions parity: keep the investigate-then-AskUserQuestion
  protocol for states fix refuses to touch.
- Port map: script behavior -> engine behavior, including the CWD-repo
  exemption (needs a session-cwd input the engine must obtain) and the
  no-fallback `$DEVROOT` rule.
- Test plan: state-classification table tests, refusal tests, fix/re-check
  flow; mirror `tests/bootstrap/test_env_checks.py` style.

### implement-engine-feature / cutover-and-retire

Later items; detail them at design ratification. Cutover checklist so far:
remove the `repo-sync` env_check entry from env.json, retire
`~/.claude/scripts/env/repo-sync.sh`, remove env-config's `--sync` (bare
delegation) and decide `--update-repos`'s home, update
`~/.claude/docs/fresh-machine.md` (already stale) and both repos'
CLAUDE.md doctrine sections.
