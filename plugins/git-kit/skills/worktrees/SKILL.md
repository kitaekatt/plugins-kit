---
_schema_version: 1
name: worktrees
author: christina
skill-type: technique-skill
description: Use when starting Git task work or managing task worktrees. Do NOT use for code review.
---

# Task worktrees

This workflow applies only when the effective `git_kit.worktrees.enabled`
setting is `true`. Configuration and helper details: [git-kit README](../../README.md#task-worktrees).

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Manage task checkouts under the root selected through bootstrap manifests.
  scope:
    covers:
      - checking the effective opt-in before starting Git task work
      - creating, inspecting and retiring task checkouts when enabled
    excludes:
      - code review
      - enabling the setting or changing permissions without the user's request
  techniques:
    - id: task_worktree
      name: Opt-in task worktree lifecycle
      keywords: [task, worktree, cleanup]
      goal: Carry out the configured task checkout lifecycle without losing unpublished work.
      preconditions:
        - The current directory is inside a Git checkout.
      gotchas:
        - A pushed task branch is published but can still be unmerged into the main upstream.
      steps:
        - n: 1
          action: |
            Run "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" config.
            Read enabled and root from the JSON output. If enabled is false, stop this
            technique; it imposes no checkout policy and changes no files.
        - n: 2
          action: |
            When enabled, task worktrees belong at <root>/<repo>/<task>, where repo is
            the main checkout's folder name and task is one portable path component.
            Create the task with "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" new <task>.
            The default source is the main checkout's fresh upstream. --from <ref>
            accepts a remote-tracking branch, local branch, tag or commit. The helper
            fetches for remote-tracking sources; local sources need no network.
            A failed fetch stops creation with the Git error.
            After creation, new runs bootstrap run in the new worktree and streams
            its output to stderr. This sets up the worktree's own venv and packages.
            Never link package folders from the main checkout.
            --no-setup skips setup for a quick read-only look or a repo without a
            bootstrap manifest. User layers apply even without a project manifest.
            A bootstrap failure, including lock exit 2, keeps the worktree and
            reports its path and exit code, then exits non-zero without retry.
            The JSON result includes setup: {ran: bool, exit: int|null}.
            Work in the returned path. Never create a task
            checkout elsewhere. Existing unpublished work needs preservation before
            moving it; creation does not move edits from the original checkout.
        - n: 3
          action: |
            Inspect leftovers with "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" list.
            The helper refreshes remotes and reports all root/repo/task entries.
            no_unpublished_commits means every commit is reachable from a remote;
            merged means HEAD is contained in the main checkout's upstream;
            gone means the configured tracking branch disappeared. dirty reports
            tracked changes and untracked files; ignored reports ignored files
            separately. Dirty entries
            and unpublished commits require preservation, even when merged or gone.
        - n: 4
          action: |
            After the task is pushed or merged, run outside its directory:
            "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" remove <path>.
            The helper refuses unpublished commits, tracked changes and untracked
            files. Ignored files block removal by default because they can hold
            local secrets or state. When the user requests deletion of ignored files,
            add --include-ignored; the other refusal checks still apply.
            Git also refuses locked entries. Cleanup then runs
            git worktree remove and git worktree prune. A failure
            stops cleanup; report it. Branch deletion is a separate user decision.
      checklist:
        - Effective opt-in checked before applying the lifecycle
        - Task created under the configured root from the default fresh upstream or selected ref
        - Published or merged task removed and pruned with unpublished work preserved
```
