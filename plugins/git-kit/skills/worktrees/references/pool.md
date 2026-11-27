# Opt-in worktree lifecycle

This reference describes repositories that explicitly enable the lifecycle.
Installation alone imposes no worktree policy. Settings default to disabled.

## CLI and approval

Launch the CLI through the provisioned plugin:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" configure --propose --repo /path/to/repo
```

`GIT_KIT_ROOT` is the root of the installed git-kit version. Bootstrap
exports it. The plugin cache can also hold older git-kit versions, and an
older CLI can refuse the current repository settings, so name the CLI
through `GIT_KIT_ROOT`.

The following arguments use that same CLI. Every command accepts `--repo`
before or after its name. For example, a take is:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" take --agent <unique-id> --session <session-id> [--task <task-id>]
```

The `--agent` value is a claim ID. It must be unique among live claims,
because it is also the worktree folder name (`<root>/<repo>/<agent-id>`). Use
the task name or another unique ID, not the agent type, such as `claude`.
When a live claim holds the ID, take and adopt refuse. The error names that
holder's session, worktree path, and claim. Choose a different, unique ID.

| Arguments | Result |
|---|---|
| `configure --propose` | Show defaults and a project-fit proposal without changing settings. |
| `configure --apply approved.json` | Validate and write the approved repository settings. |
| `take --agent <unique-id> --session <session-id> [--task <task-id>]` | Claim a prepared spare and run project `after_take` commands. |
| `adopt --id w-... --agent <unique-id> --session <session-id>` | Claim a parked worktree and complete setup while preserving its branch and files. |
| `parked [--session s1 | --task task-id]` | Show all parked entries, or entries for one session or task. |
| `check-task-close --task task-id --json` | Return `{allowed, parked:[...]}` for a task-close check. |
| `discard --id w-... --propose` | Identify the target and the work that discard will remove. |
| `discard --id w-... --apply approved.json` | Apply targeted user direction for an unchanged proposal. |
| `release --claim c-...` | Release the exact claim after full delivery and successful safety checks. |
| `status [--json]` | Show records, current Git observations, process blockers, disk, and maintenance. |
| `migrate --inventory [--clone /path/to/clone ...]` | Journal legacy candidates and clone identities after the Claude-process guard passes. |
| `migrate --apply [--clone /path/to/clone ...]` | Resume safe removal or preservation, convert records, and request maintenance. |
| `maintenance` | Run the internal maintenance pass in the foreground. |

Status always emits JSON. `config` and `list` are status aliases.
`pool status` and `pool refill` delegate to status and maintenance.
`pool refill --background` requests a detached pass.
Raw `new`, positional `take <task-name>`, and raw `remove` refuse mutation.
The supported `take --agent <unique-id> --session <session-id> --task <task-id>`
form accepts task metadata. The positional legacy form refuses mutation.

Review the proposal with the user before applying it. Save the approved
`proposal` object as JSON, then pass its file path to `--apply`.
Apply changes only repository `.claude/bootstrap.json`, preserving unrelated
sections. Commit this file through the repository's normal procedure.
Lifecycle operations read the settings from the upstream branch, so the change
takes effect when its commit reaches that branch. The apply result names that
branch in `takes_effect`. User disk settings remain separate.

A usable git-kit installation must be enabled at user scope.

| Installation scenario | Lifecycle result |
|---|---|
| Usable, enabled user scope | Works after repository approval. |
| Project scope only | Refuses with "install git-kit at user level" and the install command. |
| Not installed | Not applicable: git-kit provides no lifecycle commands or hooks. |

An unconfigured repository directs the caller to configure.
A disabled repository creates no lifecycle state or worktrees.
An existing legacy namespace blocks mutation until migration.
For migration prerequisites, discovery, and recovery, read [Cutover](migration.md).

## Creation enforcement

For an enabled repository, the PreToolUse hook blocks these creation requests:

- The Agent tool with `isolation: "worktree"`.
- A Bash command that invokes Git's raw `worktree add` subcommand.
  Detection includes Git's `-C` argument and common command wrappers.

The hook reads tracked repository `.claude/bootstrap.json`, under `git_kit.worktrees`.
User settings and local overrides cannot enable this enforcement.
Disabled and unconfigured repositories keep their ordinary tool behavior.
The hook returns a PreToolUse `permissionDecision: "deny"` with the full take
command through `GIT_KIT_ROOT`, the rule that the agent ID is unique, and the
configure command for settings approval.

An internal build creates a staging checkout through the lifecycle worker.
The worker proves maintenance-lock ownership and its recorded process identity.
An environment variable cannot grant build authority.
A raw tool request remains refused even during a legitimate background build.

Git-kit registers no SessionStart maintenance hook.
Completed and blocked lifecycle operations request maintenance.
Project SessionStart duties, such as main-status reports and `core.hooksPath`,
remain project-owned.

## Settings

Repository settings live under `git_kit.worktrees` in tracked
`.claude/bootstrap.json`. User and local opt-ins cannot enable a repository.

Lifecycle operations read these settings from the upstream branch, not from the
source checkout's working tree. A main checkout that is behind its upstream
therefore cannot apply old settings. Status and the gate read the upstream
remote-tracking ref as the clone last fetched it. Each operation that fetches,
such as take, adopt, release, discard and maintenance, reads the settings
again from the fetched commit. Setup, `rebuildable` and the other checks then
use the settings of the commit that they act on.

The working-tree file only names the first branch to read: its `pool.upstream`,
or the remote default branch. When that branch does not exist yet, reading
starts at the remote default branch. The settings on the branch must name the
same branch. One step follows a changed `pool.upstream`; a disagreement after
that step blocks the operation. An unreadable or invalid file on the branch
blocks the operation with an error that names the branch and commit. Git-kit
never uses the working-tree copy instead. When the branch has no
`.claude/bootstrap.json`, the repository is unconfigured.

Validation checks tracked paths (`rerun_files`, `recreate`, `rebuildable`)
against the files of that commit. The worktree `root` and user
`disk_floor_percent` come from the bootstrap layers on disk, including an
untracked `bootstrap.local.json`, because they are machine settings.
The creation-enforcement hook, `check-task-close` and `pool --quiet-if-off`
still decide whether the lifecycle applies from the working-tree file.

```json
{
  "git_kit": {
    "worktrees": {
      "enabled": true,
      "after_take": [],
      "before_release": [],
      "pool": {
        "size": 2,
        "setup": [
          {"id": "root", "argv": ["npm", "ci"], "recreate": ["node_modules"]},
          {"id": "bootstrap", "argv": ["bootstrap", "run", "--project-only"],
           "path_bound": true, "recreate": [".venv"]}
        ],
        "rerun_files": {
          "package-lock.json": ["root"],
          ".nvmrc": ["root"],
          "uv.lock": ["bootstrap"]
        }
      }
    }
  }
}
```

The example requires every listed input to be tracked.
Remove inputs that the repository does not use.

| Field | Rule |
|---|---|
| `enabled` | Boolean, default false. Explicit repository approval enables the lifecycle. |
| `root` | Absolute or `~/` path, default `~/.worktrees`. The namespace is `<root>/<repo>`. |
| `after_take`, `before_release` | Lists of nonempty argument arrays. Default: empty lists. See [Project extensions](#project-extensions). |
| `pool.size` | Positive integer, default 2. Ready spares and active builds count toward capacity. |
| `pool.upstream` | Optional remote-tracking branch, such as `origin/dev` or `upstream/develop`. The remote must exist, the name must have the form `<remote>/<branch>`, and `..` is invalid. Default: the remote default branch resolved below. |
| `pool.rebuildable` | Optional list of relative gitignore-style globs for reproducible ignored outputs. Default: empty list. No glob can match tracked files. |
| `pool.idle_claim_hours` | Integer, default 2. Maintenance parks a live session's claim after this many hours without change or process use. 0 keeps every live claim. |
| `setup[].id` | Unique stable ID: letters, digits, underscore, or hyphen, starting with a letter. |
| `setup[].argv` | Nonempty argument array. Commands run in declaration order without a shell. |
| `setup[].path_bound` | Boolean, default false. Repeat this step after relocation during preparation, take, or adoption. |
| `setup[].recreate` | Relative reproducible outputs. Paths cannot overlap or contain tracked data. |
| `rerun_files` | Map from tracked inputs to known setup IDs. Default: empty map. |

Preparation runs every setup step in declaration order.
Take follows the selection rules in [Claims](#claims).
The record stores input object IDs and a definition hash.
`pool.upstream` selects the upstream for spare creation, freshness, and safety checks.
Set it when integration uses a branch such as `dev` or `develop`, or a fork's
canonical `upstream` remote.
When the setting is absent, the upstream is the remote default branch, named by
the remote's HEAD. Git-kit uses `origin`, or the sole remote when `origin` is absent.
It reads the local remote HEAD symbolic ref, then queries the remote if absent.
An undetermined default branch blocks the operation. The source checkout's
current branch and its tracking settings do not select the upstream.
`configure --propose` includes `pool.upstream` with the resolved remote default.
Review and change that value as needed before approving the proposal.
The root uses bootstrap's effective layered settings, including user, project,
local overrides, and profiles. Repository enablement and setup approval still
come only from tracked repository settings.

The proposal includes one `npm ci` step per tracked `package-lock.json`
directory, including the root. Each lockfile maps to its own step.
Untracked lockfiles are excluded.

The proposal includes no `pool.rebuildable` entries by default.
After project-specific user approval, Claude can add entries under `git_kit.worktrees.pool`:

```json
{
  "rebuildable": [
    "packages/*/dist/",
    "*.tsbuildinfo",
    "web/dist",
    ".venv.sync.sha256",
    "node_modules/"
  ]
}
```

These globs use Git's ignore syntax, including `*`, `**`, and directory patterns.
Patterns must be relative and contain no `..` component.
Absolute paths, negations, comments, backslashes, and `.git` paths are invalid.
Git-kit rejects settings whose globs match tracked files.
It repeats this check against each candidate worktree before classification.
Only ignored files that match a glob count as reproducible.
Unmatched ignored files remain unique data and block retirement.
Untracked files still block retirement, even when a glob matches their names.
This setting does not add setup commands or guarantee that setup rebuilds these outputs.

User `~/.claude/bootstrap.json` and `bootstrap.local.json` can set
`git_kit.worktrees.disk_floor_percent`. The default is 10 percent.
Project layers cannot change it. At or below the floor, only new builds block.
Takes, release, reconciliation, reuse, and removal remain available.
Setup can still fail for lack of space.

## Claims

Take reserves a holder under the claim lock before it renames a spare.
The journal records both paths before the rename.
The destination is `<root>/<repo>/<agent-id>`.
An agent ID that a live claim holds refuses the take and names that holder.
The optional task is record metadata.

Take repairs registration, fetches the source upstream, and proves a fast-forward.
It creates a separate branch and runs project `after_take` commands.
The READY spare already passed setup before it entered the pool.
Take repeats every `path_bound` setup step at the claim path.
It also repeats steps mapped by `rerun_files` when their recorded input blobs differ after the fast-forward.
Selected steps run in `pool.setup` declaration order.
A step selected by both rules runs once. Take records the current input blobs after these steps pass.
Other setup steps stay prepared, even when their settings change.
Maintenance owns pool preparation, output removal, recycling, and parked rechecks.
Selected setup steps recreate only their own declared outputs.
Additional project commands for the claim belong in `after_take`.
The result's `timings` object reports phase durations in seconds.
It includes `initialize`, `holder`, `claim`, `freshness`, `branch`, `path_bound`, `rerun`, `after_take`, `finalize`, `maintenance`, and `total`.
The `path_bound` and `rerun` durations sum the selected setup calls in each category.
A step selected by both rules counts only toward `path_bound`.
The `maintenance` duration covers the background launch request, and `total` includes project commands and that request.
An empty pool blocks take with exit 4 and requests maintenance.
A failed take retains its claim, files, failed step, and log.
Incomplete claims never become available spares.

Take and adopt use the nearest Claude Code CLI ancestor as the holder.
Records include its PID, process start identity, executable, and command.
If no Claude ancestor exists, the caller must supply `--holder-pid <pid>`.
Otherwise, take and adopt refuse the claim.

Reconciliation parks a claim after proven Claude holder death, or when the claim is idle
(see [Retirement and maintenance](#retirement-and-maintenance): no change for `pool.idle_claim_hours` and no process using it).
For death, if the record has a session ID, a live Claude process for that session blocks parking.
Uncertain session evidence also blocks parking for death.
An older holder record without Claude executable or command evidence is not dead after PID death.

## Parked work and adoption

`parked` includes taskless entries. Its optional filters select the last
holder's session or the recorded task. A full entry includes path, branch,
task, session, last holder, waiting work, approved boundary, and process blockers.
Take returns stored metadata for its session or task and counts the other entries.
It does not inspect parked paths. The explicit `parked` command observes their current state.

`adopt` reserves a parked worktree under the same claim lock as take.
The caller names one stable worktree ID. The adopting agent ID must have no
live claim; otherwise the error names that holder.
A holder or live or uncertain process evidence blocks adoption.
A competing adopter receives the winning holder's details.

Adoption moves the worktree to the agent path and repairs Git registration.
It preserves the existing branch, commits, and files. It repeats path-bound
setup after relocation. A setup error retains the adopting holder and files.
Adoption consumes no ready spare.

`check-task-close --task task-id --json` returns an `allowed` boolean and a
`parked` array. Parked work for that task makes `allowed` false.
Taskless work remains visible in project status and the unfiltered parked list.
Disabled or unconfigured repositories return `allowed: true` with an empty array.
An enabled repository's evidence error exits 2 and does not permit closure.

## Task-close hook

A task-kit pre-close command can call `check-task-close` before close or archive.
The command returns `{ "allowed": true, "parked": [] }` when the task has no
parked work. A nonempty `parked` array makes `allowed` false.

The repository's `.claude/bootstrap.json` can set `task_kit.pre_close` to a
nonempty argument array or an object with `argv` and optional `required_paths`.
For an interpreter command, guard the git-kit script's existence:

```json
{
  "task_kit": {
    "pre_close": {
      "argv": ["/resolved/bootstrap-python", "/resolved/git-kit/scripts/worktrees.py", "check-task-close", "--task", "{task_id}", "--json", "--repo", "{repo}"],
      "required_paths": ["/resolved/git-kit/scripts/worktrees.py"]
    }
  }
}
```

Replace the example paths with resolved values from `BOOTSTRAP_PYTHON` and
`GIT_KIT_ROOT`. This setting does not expand shell variables.
`required_paths` contains absolute literal paths. Whole arguments `{task}`, `{task_id}`,
and `{repo}` expand to the canonical task ref, folder slug, and absolute
project root. Other arguments remain literal. The command runs without a
shell, from the project root, with a 120-second timeout.
Task matching uses the exact string stored by take's `--task` argument.
Use `{task_id}` for stored slugs or `{task}` for stored canonical task refs.

Exit 0 with `allowed: true` and `parked: []` permits close or archive.
Other responses, nonzero exits, invalid JSON, and timeouts block before mutation.
An absent setting preserves ordinary task-kit behavior. An absent configured
executable or required path also preserves that behavior and prints an omitted-check diagnosis.
Task-kit imports no git-kit code and enables no hook automatically.

## User-directed discard

Only explicit user direction for identified work authorizes discard.
`discard --id w-... --propose` shows the target and work at risk without removal.
Save its returned `proposal` field as JSON, including the token and inventory fingerprint.
After the user directs that loss, add this field to the approved JSON file:

```json
{
  "user_direction": {
    "actor": "user",
    "id": "w-...",
    "text": "The user's explicit direction about this target and its identified work"
  }
}
```

This example shows the added field, not a complete proposal.
Pass the complete file to `discard --id w-... --apply approved.json`.
The target ID and inventory must still match the proposal.
A changed inventory requires another proposal and user direction.

A holder, live or uncertain process evidence, or ambiguous scope blocks discard.
Submodules and tracked files marked assume-unchanged or skip-worktree make the
loss scope ambiguous and block discard.
`--force` supplies no authority and bypasses no guard.
The record retains the identified lost work, user direction, and outcome.
Ordinary retirement continues to require the full safety proof.

When the worktree path, its source clone and that clone's Git directory all
no longer exist, the proposal's `lost_work` has `absent: true`, empty `files`,
`commits` and `branch_refs`, the `checked` paths, and a `statement` that
nothing remains on disk to lose. The same user direction applies it. Apply
repeats the holder and declared-process checks, retires the record as
`removed`, and deletes nothing. If any checked path exists again, apply refuses.

## Project extensions

Repository `after_take` and `before_release` settings contain lists of argument arrays:

```json
{
  "after_take": [["node", "tools/worktree-after-take.mjs"]],
  "before_release": [["node", "tools/worktree-before-release.mjs"]]
}
```

These settings belong under `git_kit.worktrees` in tracked `.claude/bootstrap.json`.
Commands run sequentially in the worktree directory, without a shell.
After-take commands run at the final claim path. Adoption completes its setup first.
Before-release commands run while the holder still owns the claim.
A script error retains ownership and records the script and log.
Release errors remain separate from any successful push.

Each command receives these environment variables:

| Variable | Value |
|---|---|
| `GIT_KIT_WORKTREE_ID` | Stable worktree ID. |
| `GIT_KIT_WORKTREE_PATH` | Current absolute worktree path. |
| `GIT_KIT_WORKTREE_CLAIM` | Current holder claim. |
| `GIT_KIT_EXTENSION_PHASE` | `after_take` or `before_release`. |
| `GIT_KIT_EXTENSION_RESULT` | Absolute path for an optional JSON result file. |

A command can write this result object:

```json
{
  "processes": [{"pid": 123, "start": "platform process start identity", "runtime_paths": ["/absolute/runtime/path"]}],
  "resources": [{"project_resource": "opaque claim"}],
  "result": {"project_status": "ready"}
}
```

Git-kit retains stdout, stderr, result JSON, process identities, and opaque resources.
The record's `extension` object stores command history under each phase name.
Each run records its arguments, path, times, exit, outcome, result, and log paths.
Logs live under `logs/`; optional result files live under `extensions/` in the
lifecycle state directory. An empty result cannot erase earlier process evidence.
Live or uncertain declared processes block retirement, even after the holder exits.
An interrupted command or unreadable or invalid resource report leaves uncertain evidence.
That evidence blocks adoption, discard, and retirement until inspection resolves it.
The lifecycle retains the command logs and result file for inspection.
Project scripts own server startup, port allocation, routing, and public-slot claims.
Git-kit stores their evidence without interpreting those project resources.

Project-only setup, such as `bootstrap run --project-only`, keeps global launcher links unchanged.
Project extension scripts must preserve those links too.

## Push integration

Agents use plain `git commit`. After a full push, the caller runs release
from any directory, including the claimed worktree:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" release --repo /path/to/source --claim c-...
```

The library seam is `worktree_lifecycle.release(repo: Path, claim: str)`.
The caller imports it from git-kit's provisioned `scripts` directory.
It returns the release result and maintenance outcome, or raises `ValueError`.
For a Python caller in the provisioned environment:

```python
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["GIT_KIT_ROOT"]) / "scripts"))
from worktree_lifecycle import release

result = release(Path(source_checkout), claim_id)
```

The caller reports push success separately from a release error.
Partial delivery keeps the claim. Later commits outside upstream also block release.
An old token cannot release a subsequent holder.

## Retirement and maintenance

Release proves safety before it atomically persists the cleared holder.
Uncommitted files or commits outside upstream end the safety check early: the
ignored-file and process scans do not run, and the evidence lists only those blockers.
A live or uncertain builder blocks release.
Release ignores its callers. They are its caller chain, which is each parent
process from the release process up to and including the holder's recorded
process. They also include every process below each chain member except the
holder, at any depth: all processes of the shell command that runs release,
such as `grep` and `head` in `release ... | grep ... | head`, a pipeline element
that runs through a shell function or a `( ... )` subshell, or a `sleep &` of that
command. Their cwd or open files in the worktree do not block release.
The release process itself never blocks, so release can run with its cwd in
the worktree. Its directory scan runs from the filesystem root, so the scanner
does not report itself. A `before_release` command finishes before the scan, so
a server it leaves running is no longer in that subtree (the system reparents
it) and still blocks release.
A chain that does not reach the holder (same PID and start) excludes nothing.
An ignored process that ends or changes identity during inspection blocks release.
Every other process still blocks. This includes the holder's other children and
their subtrees, such as another shell command of the session, a background job
or a server.
Maintenance has no callers. It reuses or removes a released worktree only
after every process, including an ignored sibling, stops using it.
Retirement requires clean tracked files, no untracked files, no holder,
and no live or uncertain process found by the platform inspection described below.
It also requires no `+` in `git cherry <fresh-upstream> HEAD`,
no unique ignored data, and no locked Git registration.
The main checkout never enters retirement. A remote task branch cannot prove delivery.

Only ignored outputs declared by `setup[].recreate` or matched by
`pool.rebuildable` qualify for removal. Git-kit checks each output before removal.
Unique ignored data remains in place. Removal uses no blanket `--force`.
The lifecycle never removes branches.

Each completed or blocked public operation requests background maintenance.
The worker runs the installed git-kit (`installed_plugins.json`), not the caller's version; with no entry there, it runs the caller's.

Disabled and unconfigured repositories perform no pass.
One OS lock coordinates all clones of a remote.
Internal steps continue the same pass. No timers exist.

Maintenance resolves interrupted journals and dead holders first.
A holder is dead when its process ended and no live Claude process names its
session, in argv or in Claude Code's `sessions/<pid>.json` with a matching
`procStart`. A claim whose session still lives is idle when nothing in the
worktree changed for `pool.idle_claim_hours` (Git HEAD, index, HEAD log, changed
paths) and no process uses it. Maintenance parks dead and idle claims with
`last_holder`. An idle claim also keeps `last_claim`, so the session's own later
release succeeds without a change.
Unfinished work becomes parked. Safe candidates enter reuse before new builds.
Reuse completes full setup before ready publication.
Surplus safe candidates enter removal. Removed records remain available.
A setup or Git error stops the pass and retains its evidence.
Detached stdout and stderr go to a log. Status shows launcher and worker errors.

A recorded source clone that no longer exists (no `.git` at its path) blocks
nothing else. Git never runs in that path. Status shows a `git_blockers` line
for each of its records. Take skips its ready spares. Maintenance parks its
unheld records, keeps holder claims, and adds one `failures` line for each such
clone that lists the live records that name it. A clone stays in
`namespace.json` `sources` while a live record names it. A pass drops it once
no live record names it, and reports that drop once. The configured source clone
never leaves `sources`. Maintenance deletes nothing on disk for a missing clone.
To retire a record whose files are also gone, use
[User-directed discard](#user-directed-discard).

On macOS, process inspection requires `ps` and `lsof` with sufficient visibility.
On Linux it reads `/proc` for processes owned by the current UID.
Other users' processes are skipped. Unreadable same-user evidence blocks retirement.
On Windows, directory-use inspection reports uncertain evidence and blocks
automatic retirement. Claims and new builds remain available.

## Shared state and record schema

The checkout namespace is `<root>/<remote-basename>/`.
Clones share the same remote URL.
Each worktree registration must match its recorded source clone and Git common directory.
A basename collision fails with an identity error.

All lifecycle state belongs in git-kit's user-scoped plugin data home:
`~/.claude/plugins/data/plugins-kit/git-kit/worktrees/<repo>/`.
The repo key uses the same remote basename as the checkout namespace.
The path resolves through `bootstrap_guard.data_dir("git-kit")`, so
`CLAUDE_BOOTSTRAP_DATA_ROOT` redirects the plugin data tree for test sessions.
Worktree checkouts remain under the configured root.

| Path under the lifecycle state directory | Purpose |
|---|---|
| `namespace.json` | Version, remote URL, source clone, common Git directory, and migrated clone `sources`. |
| `records/<stable-id>.json` | Durable record and current transition journal. |
| `logs/<stable-id>.log` | Setup command output and errors. |
| `logs/<stable-id>-<phase>-<token>.log` | Extension command output and errors. |
| `extensions/<stable-id>-<phase>-<token>.json` | Optional result written by an extension command. |
| `claim.lock` | Holder reservation, rename journal, and retirement exclusion. |
| `fetch.lock` | Serialized source fetches. |
| `maintenance.lock` | One maintenance worker across clones. |
| `launch.lock` | Launcher/worker acceptance handshake. |
| `maintenance.json`, `maintenance.log` | Worker identity, outcome, and output. |
| `launcher-error.json` | Failed launcher operation and reason. |
| `migration.json` | Clone identities, legacy-to-record mappings, and resumable cutover phases. |
| `migration.lock`, `migration-legacy/` | Cutover exclusion and verified copies of retired legacy metadata. |

Record version 1 contains `id`, `repo`, `path`, `state`, `operation`, `holder`,
`task`, `branch`, `head`, `upstream`, `boundary`, `waiting`, `setup`,
`processes`, `extension`, `safety`, `worker`, and `error`.
Operation stores name, phase, previous state, time, and rename paths.
Holder stores agent, session, claim, claim time, PID, and start identity.
Setup stores built OID, definition hash, input OIDs, selected steps, and
the final-path result. Errors store step, reason, exit code, and log.
Release retains `last_claim`. Dead-holder recovery retains `last_holder`.
Adoption retains the prior waiting details in `last_waiting`.
Optional `discard` stores the inventory-bound proposal, `user_direction`, and outcome,
including a reconciled removal outcome after an interrupted discard.
Status adds live `observed` fields and marks stored safety as historical.
Status, parked, check-task-close and an empty-pool take print each record with
bounded evidence. In `safety`, `discard` and `error`, a list other than
`blockers` becomes `<key>_count`, and text over 500 characters is cut.
`record_file` names the record file that keeps the full evidence.
