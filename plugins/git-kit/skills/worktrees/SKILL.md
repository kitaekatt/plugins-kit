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

1. Run the status command for the repository:

   ```bash
   "${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLAUDE_PLUGIN_ROOT}/scripts/worktrees.py" status --json
   ```

   If `enabled` is false, the lifecycle applies no worktree policy.
   If the user requests enablement, read
   [settings and approval](references/pool.md#cli-and-approval).
   Show the proposal and obtain approval before `configure --apply`.

2. For an enabled repository, take a ready spare with
   `take --agent <agent-id> --session <session-id> [--task <task-id>]`.
   Return success only when the command completes setup.
   Exit 4 reports an empty pool and a maintenance request.
   Report its builds, failures, holders, and disk evidence.
   A failed take preserves its claim and files.

3. Work in the returned path. Commit with plain `git commit`.
   The repository's review and delivery rules still apply.
   For a full push, invoke `release --claim <claim-id>` from outside that path.
   A partial push keeps the claim.
   Report push success separately from a release error.

4. Use `status --json` to inspect current Git observations, holders, errors,
   and maintenance. The record retains removed outcomes and parked work.
   Existing legacy namespaces require migration before lifecycle mutation.

Commands use the interpreter and script path from step 1.
For holder PID selection, push integration, safety evidence, record fields,
and shared locks, read [the lifecycle reference](references/pool.md).
