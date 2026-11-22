# Cutover for an enabled repository

Migration brings existing worktrees into the repository's approved lifecycle.
It preserves unfinished work and records every candidate's outcome.
The source checkouts and their branches remain in place.

## Prerequisites and command

1. Review the repository proposal from `configure --propose`.
2. Apply only the approved settings with `configure --apply approved.json`.
3. Commit `.claude/bootstrap.json` through the repository's normal procedure.
4. Verify that a usable git-kit installation is enabled at user scope.
5. Exit every Claude session on the machine.
6. From a terminal, run the inventory command:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" migrate --inventory --repo /path/to/clone
```

Both migration modes inspect live Claude processes.
Their output states the process check and its result.
A live Claude process refuses migration before candidate changes.
A session restart or compaction does not satisfy this prerequisite.
Uncertain process evidence preserves work.

The inventory command journals its findings but changes no candidate checkout.
After inspection, apply the inventory from the same terminal:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" migrate --apply --repo /path/to/clone
```

The same `--repo` argument works before the subcommand.
For a clone outside automatic discovery, append `--clone /path/to/other-clone`.
Repeat `--clone` for each such clone in inventory and apply.
Include every clone before completing apply. A completed journal refuses additional clones.

## Discovery and preservation

Migration discovers the selected clone, recorded source clones, and sibling
clones whose own `.git` entry identifies the same remote.
It also follows source identities and Git links in legacy lifecycle data.
It compares registered worktrees with the filesystem and includes each
`<clone>/.claude/worktrees/*` Agent-tool path.
Explicit `--clone` arguments cover clones outside those discovery paths.

Each candidate receives a stable record ID and an original-path mapping.
The journal and converted records live in git-kit's plugin data home:
`~/.claude/plugins/data/plugins-kit/git-kit/worktrees/<repo>/`.
`CLAUDE_BOOTSTRAP_DATA_ROOT` redirects that home for isolated test sessions.

Apply uses the ordinary release safety test against a freshly fetched upstream.
A safe candidate has clean files, no unique commits, no holder, and no live
or uncertain process evidence.
Unique ignored data also blocks removal.
Declared reproducible setup output can accompany safe retirement.

Apply removes only candidates that pass that test.
Every other candidate becomes parked without a holder, with its blockers recorded.
Unsafe Agent-tool paths remain at their original locations.
A later `adopt` can relocate an identified parked candidate through the claim protocol.
Migration grants no discard authority.
It removes neither branches nor a clone's main checkout.

## Journal and recovery

The cutover records these phases in the plugin data home:

| Phase | Result |
|---|---|
| `inventory` | Record clone identities, candidate paths, stable IDs, and legacy mappings. |
| `classify` | Reserve each candidate, recheck safety, then record removed or parked. |
| `metadata` | Preserve legacy metadata in plugin state after Git and converted records agree. |
| `complete` | Establish the lifecycle namespace and request background maintenance. |

Old pool state and taken records become durable lifecycle records.
Their previous holder details remain historical evidence.
Their active holder field is empty after cutover.
Metadata retirement follows agreement between Git and the converted state.
Verified metadata copies remain under `migration-legacy/` in the plugin state directory.
Candidate phases distinguish pending work, removal in progress, removed work, and parked work.

If an operation stops, inspect the reported error and journal.
Then rerun `migrate --apply` with the same clone arguments.
Completed candidates keep their IDs and outcomes.
An interrupted removal reconciles the directory, Git registration, and durable phase
before the operation proceeds.

After cutover, `status --json` shows parked entries and the maintenance result.
Maintenance prepares the approved pool within the user disk floor.
Take and adopt remain the lifecycle entry points for an enabled repository.
The creation hook enforces these entry points.
Git-kit schedules no SessionStart refill, timer, or idle-age removal.

Project delivery helpers call `release --claim` after full delivery.
They report push and release outcomes separately.
Project scripts still own review gates, main-status checks, and `core.hooksPath`.
