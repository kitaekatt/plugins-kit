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
            The helper fetches upstream before creating the branch. Its default source
            is the main checkout's upstream; --from <remote>/<branch> selects another
            fetched upstream branch. Work in the returned path. Never create a task
            checkout elsewhere. Existing unpublished work needs preservation before
            moving it; creation does not move edits from the original checkout.
        - n: 3
          action: |
            Inspect leftovers with "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" list.
            The helper refreshes remotes and reports all root/repo/task entries.
            no_unpublished_commits means every commit is reachable from a remote;
            merged means HEAD is contained in the main checkout's upstream;
            gone means the configured tracking branch disappeared. Dirty entries
            and unpublished commits require preservation, even when merged or gone.
        - n: 4
          action: |
            After the task is pushed or merged, run outside its directory:
            "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" remove <path>.
            The helper refuses unpublished commits and dirty entries, including
            ignored files. Git also refuses locked entries. Cleanup then runs
            git worktree remove and git worktree prune. A failure
            stops cleanup; report it. Branch deletion is a separate user decision.
      checklist:
        - Effective opt-in checked before applying the lifecycle
        - Task created under the configured root from a fetched upstream branch
        - Published or merged task removed and pruned with unpublished work preserved
```
