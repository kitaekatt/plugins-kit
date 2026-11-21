---
_schema_version: 1
name: worktrees
author: christina
skill-type: technique-skill
description: Use when inspecting or operating a repository's opt-in worktree lifecycle. Do NOT use for code review.
---

# Opt-in worktree lifecycle

This skill describes repositories that enable `git_kit.worktrees` through
approved repository settings. Installation alone imposes no checkout policy.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Operate the repository-approved worktree lifecycle and report its outcomes.
  scope:
    covers:
      - inspecting lifecycle enablement, claims, and maintenance
      - taking, adopting, and releasing worktrees in enabled repositories
      - inspecting parked work and task-close blockers
      - applying discard only for explicit user direction about identified work
      - proposing settings when the user requests enablement
    excludes:
      - code review
      - enabling a repository without approval
  techniques:
    - id: worktree_lifecycle
      name: Opt-in worktree lifecycle
      keywords: [worktree, pool, claim, adopt, parked, release, discard, maintenance]
      goal: Operate the configured lifecycle while preserving claims and unpublished work.
      preconditions:
        - The selected repository is a Git checkout with origin or exactly one remote.
      gotchas:
        - A partial push keeps the claim; report push success separately from a release error.
        - A failed take preserves its claim and files. An empty pool exits 4 and requests maintenance.
        - Adoption preserves the branch and files. Live or uncertain process evidence blocks adoption.
        - A discard proposal is not authority. Only targeted user direction authorizes its application.
      steps:
        - n: 1
          action: |
            Run "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" status --json.
            If enabled is false, the lifecycle applies no worktree policy.
            If the user requests enablement, read references/pool.md#cli-and-approval.
            Show the proposal and obtain approval before configure --apply.
            Use the same interpreter and script for the commands below.
        - n: 2
          action: |
            Inspect parked [--session <session-id> | --task <task-id>] for waiting work.
            To resume an identified parked entry, run adopt --id <worktree-id> --agent <agent-id> --session <session-id>.
            To claim a ready spare, run take --agent <agent-id> --session <session-id> [--task <task-id>].
            Report take or adoption success only when the command completes setup.
            Take shows this session's and task's parked entries in full and counts the rest.
            On exit 4, report the empty pool and maintenance request, including builds,
            failures, holders, and disk evidence. Report a failed take with its preserved claim.
        - n: 3
          action: |
            Work in the returned path. Commit with plain git commit.
            Apply the repository's review and delivery rules.
            After a full push, run release --claim <claim-id> from outside that path.
            Report push and release outcomes separately; retain the claim after partial delivery.
            Repository after_take and before_release scripts retain ownership on failure.
        - n: 4
          action: |
            Run status --json to inspect current Git observations, holders, errors, and maintenance.
            Records retain removed outcomes and parked work.
            Before task closure, inspect check-task-close --task <task-id> --json.
            For task-kit integration, read references/pool.md#task-close-hook.
            If the user requests discard, read references/pool.md#user-directed-discard before proposing or applying it.
            Report a legacy namespace as requiring migration before lifecycle mutation.
      checklist:
        - "[ ] Repository enablement checked; any configuration application approved"
        - "[ ] Parked work inspected; take or adoption outcome reported with the final path or preserved claim and failure evidence"
        - "[ ] Push and release outcomes reported separately; partial delivery retains the claim"
        - "[ ] Status inspected for holders, errors, maintenance, and migration blockers"
        - "[ ] Any discard application records targeted user direction and unchanged proposal evidence"
```

For holder PID selection, push integration, safety evidence, record fields,
and shared locks, read [the lifecycle reference](references/pool.md).
