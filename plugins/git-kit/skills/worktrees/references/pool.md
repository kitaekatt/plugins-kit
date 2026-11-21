# Shared pool of ready worktrees

The pool keeps task worktrees ready before a task starts. A ready worktree is
on the latest upstream commit, and every setup step of the repository passed in
it. A take moves one ready worktree to the task path at once. Code:
`scripts/worktree_pool.py`. Tests: `tests/git-kit/test_worktree_pool.py`.

## Configuration

The repository declares the pool in `git_kit.worktrees` of its
`.claude/bootstrap.json`. The pool works only when `enabled` is `true`.

```json
{
  "git_kit": {
    "worktrees": {
      "enabled": true,
      "pool": {
        "size": 2,
        "setup": [["npm", "ci"], ["bootstrap", "run", "--project-only"]],
        "rebuild_when_changed": ["package-lock.json", "uv.lock"],
        "after_take": {"remove": [".venv"], "steps": [["bootstrap", "run", "--project-only"]]}
      }
    }
  }
}
```

| Key | Default | Meaning |
|-----|---------|---------|
| `size` | `2` | The number of ready worktrees that a refill keeps. |
| `setup` | `[["bootstrap", "run"]]` | Commands, as argument arrays, that run in order in the worktree. No shell. |
| `rebuild_when_changed` | `[]` | Paths. When the upstream changed one of them after a spare was built, the take runs `setup` again. |
| `after_take.remove` | `[]` | Ignored paths inside the worktree that the take deletes after the move. |
| `after_take.steps` | `[]` | Commands that the take runs at the task path after the move. |

Use `after_take` for setup output that records its own absolute path. A Python
venv does: its console scripts and `activate` name the venv path, so a moved
venv is broken. Remove it and make it again at the task path. `node_modules`
links are relative and survive the move. The take refuses to delete a path that
Git does not ignore.

## Where the pool is

- All clones of one remote share one pool. The pool folder is
  `<root>/<repo>/ready/`, where `<repo>` is the last path part of the remote
  URL (`origin`, or the only remote) without `.git`.
- A taken worktree moves to `<root>/<repo>/<task>`. This is the same
  filesystem, so the move is one rename.
- `pool.json` records the remote URL and the source clone. When the URL does
  not agree, the pool stops with an error: two remotes share the folder name.
- The task name `ready` is reserved.
- The pool records, logs and locks stay in the pool folder, not in the plugin
  data folder. Every clone must use the same locks and records, and they
  describe the worktrees beside them: when the root moves, they move too.

## Source clone

The first clone that runs a refill records itself as the source clone. Every
spare is a worktree of that clone, so:

- No extra clone uses disk space; the source clone has the objects already.
- `git worktree list` in the source clone shows the full pool.
- Every task branch from the pool is in one repository, so a branch name is
  checked in one place. The take refuses a branch name that exists there.

When the source clone moves, set `source` in `pool.json` to another clone of the
same remote.

## Refill

`worktrees.py pool refill` builds spares one at a time until the pool has
`size` spares.

1. Only one refill runs. It holds `ready/.refill.lock`. A second refill exits at
   once with `"skipped": "another refill is running"`.
2. A build starts in `ready/.staging/<id>/` from the fresh upstream commit, with
   checkout hooks off: the declared `setup` steps replace them.
3. Each `setup` step runs in the staging folder. Its output goes to
   `ready/.state/<id>.log`, and `ready/.state/<id>.json` shows the current step.
4. When every step passed, the build is renamed to `ready/<id>/`. Thus a
   half-built worktree is never in the pool.
5. When a step fails, the refill removes the staging worktree, keeps the record
   with its reason (step, command, exit code) and the log, and exits non-zero.
   The pool keeps the last 5 failed records.
6. A `building` record without a running refill is from a build that stopped.
   The next refill records it as failed (`abandoned`) and removes its staging
   worktree. It refuses to remove one that has unpublished commits.

Triggers:

- Each take starts a detached refill.
- Each Claude Code session start: git-kit's SessionStart hook
  (`hooks/sessionstart/pool-refill.sh`) starts a detached refill and returns at
  once. It does nothing outside a Git checkout or when no pool is configured.
  When it finds no Python interpreter at all, it prints one warning and exits 0.
  The hook is used, not the bootstrap pass, because the bootstrap pass runs at
  most once an hour for each project.
- Setup steps run with `GIT_KIT_POOL_BUILD=1`. Under it, a refill does not
  start, so a build cannot start another build.

A detached refill writes to `ready/.refill.log`.

## Take

`worktrees.py take <task>`:

1. Renames the oldest spare to `<root>/<repo>/<task>`. The rename is the claim.
   When two agents take at the same time, only one rename of a spare can
   succeed; the other agent tries the next spare. The take also holds
   `ready/.claim.lock` for the rename, because Windows does not promise an
   atomic directory rename.
2. Repairs the Git worktree links after the move.
3. Fetches the source clone (one fetch at a time, under `ready/.fetch.lock`)
   and creates the branch `<task>` on the fresh upstream commit. A spare that is
   behind the upstream moves forward; a spare has no commits of its own.
4. When the upstream changed a `rebuild_when_changed` path since the spare was
   built, it runs `setup` again in the task worktree. A failure keeps the
   worktree, names the step and the log, and exits with the step's exit code.
5. Deletes the `after_take.remove` paths and runs `after_take.steps` at the
   task path. A failure keeps the worktree, names the step and the log, and
   exits with the step's exit code.
6. Starts a detached refill (before steps 2-5, so a slow take does not delay it).

The JSON result has `path`, `branch`, `spare`, `base`, `head`, `rebased` and
`setup: {ran, exit, changed}` and `after_take: {removed, exit}`.

## Empty pool: a hard block

When no spare is ready, the take stops with exit code 4. It does not make a
fresh worktree and does not run a setup. The message names:

- the builds in progress, with their current step and log;
- builds that stopped (abandoned);
- the failed builds, with their reasons and logs;
- the taken worktrees that still exist.

The take also starts a refill. Report the details to the user and stop the task. To build one
now in the foreground: `worktrees.py pool refill`.

## Status

`worktrees.py pool status` prints JSON: `spares`, `building`, `abandoned`,
`failed` and `taken`.

## Safety

- Spares and taken worktrees are only under the configured root.
- `list` does not report the pool folder. Remove a taken worktree with
  `remove`, as for any task worktree: it refuses unpublished work.
