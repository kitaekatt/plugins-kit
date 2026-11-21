# Opt-in worktree lifecycle

This reference describes repositories that explicitly enable the lifecycle.
Installation alone imposes no worktree policy. Settings default to disabled.

## CLI and approval

Launch the CLI through the provisioned plugin:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${GIT_KIT_ROOT:?requires git-kit provisioning}/scripts/worktrees.py" configure --propose --repo /path/to/repo
```

The following arguments use that same CLI. Every command accepts `--repo`
before or after its name.

| Arguments | Result |
|---|---|
| `configure --propose` | Show defaults and a project-fit proposal without changing settings. |
| `configure --apply approved.json` | Validate and write the approved repository settings. |
| `take --agent a1 --session s1 [--task task-id]` | Claim a spare and complete setup at its final path. |
| `release --claim c-...` | Release the exact claim after full delivery and successful safety checks. |
| `status [--json]` | Show records, current Git observations, process blockers, disk, and maintenance. |
| `maintenance` | Run the internal maintenance pass in the foreground. |

Status always emits JSON. `config` and `list` are status aliases.
`pool status` and `pool refill` delegate to status and maintenance.
`pool refill --background` requests a detached pass.
Raw `new`, task-named `take`, and raw `remove` refuse mutation.

Review the proposal with the user before applying it. Save the approved
`proposal` object as JSON, then pass its file path to `--apply`.
Apply changes only repository `.claude/bootstrap.json`, preserving unrelated
sections. Commit this file through the repository's normal procedure.
User disk settings remain separate.

A usable git-kit installation must be enabled at user scope.

| Installation scenario | Lifecycle result |
|---|---|
| Usable, enabled user scope | Works after repository approval. |
| Project scope only | Refuses with "install git-kit at user level" and the install command. |
| Not installed | Not applicable: git-kit provides no lifecycle commands or hooks. |

An unconfigured repository directs the caller to configure.
A disabled repository creates no lifecycle state or worktrees.
An existing legacy namespace blocks mutation until migration.

## Settings

Repository settings live under `git_kit.worktrees` in tracked
`.claude/bootstrap.json`. User and local opt-ins cannot enable a repository.

```json
{
  "git_kit": {
    "worktrees": {
      "enabled": true,
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
| `pool.size` | Positive integer, default 2. Ready spares and active builds count toward capacity. |
| `setup[].id` | Unique stable ID: letters, digits, underscore, or hyphen, starting with a letter. |
| `setup[].argv` | Nonempty argument array. Commands run in declaration order without a shell. |
| `setup[].path_bound` | Boolean, default false. Repeat this step after each relocation. |
| `setup[].recreate` | Relative reproducible outputs. Paths cannot overlap or contain tracked data. |
| `rerun_files` | Map from tracked inputs to known setup IDs. Default: empty map. |

Changed inputs select mapped steps once, in declaration order.
A definition change selects every step. Path-bound steps repeat after relocation.
The record stores input object IDs and a definition hash.
The upstream branch comes from the source checkout's tracking settings.
The root uses bootstrap's effective layered settings, including user, project,
local overrides, and profiles. Repository enablement and setup approval still
come only from tracked repository settings.

The proposal includes one `npm ci` step per tracked `package-lock.json`
directory, including the root. Each lockfile maps to its own step.
Untracked lockfiles are excluded.

User `~/.claude/bootstrap.json` and `bootstrap.local.json` can set
`git_kit.worktrees.disk_floor_percent`. The default is 10 percent.
Project layers cannot change it. At or below the floor, only new builds block.
Takes, release, reconciliation, reuse, and removal remain available.
Setup can still fail for lack of space.

## Claims and push integration

Take reserves a holder under the claim lock before it renames a spare.
The journal records both paths before the rename.
The destination is `<root>/<repo>/<agent-id>`.
The optional task is record metadata.

Take repairs registration, fetches the source upstream, and proves a fast-forward.
It creates a separate branch and completes selected setup.
An empty pool blocks take with exit 4 and requests maintenance.
A failed take retains its claim, files, failed step, and log.
Incomplete claims never become available spares.

`--holder-pid` names the long-lived holder process. Its default is the CLI parent.
Callers with short-lived shell parents must supply the actual holder PID.
Records include PID and process start identity.

Agents use plain `git commit`. After a full push, the caller runs release
from outside the claimed directory:

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
A live or uncertain builder blocks release.
Retirement requires clean tracked files, no untracked files, no holder,
and no live or uncertain process found by the platform inspection described below.
It also requires no `+` in `git cherry <fresh-upstream> HEAD`,
no unique ignored data, and no locked Git registration.
The main checkout never enters retirement. A remote task branch cannot prove delivery.

Only declared, ignored reproducible outputs qualify for removal.
Unique ignored data remains in place. Removal uses no blanket `--force`.
The lifecycle never removes branches.

Each completed or blocked public operation requests background maintenance.
Disabled and unconfigured repositories perform no pass.
One OS lock coordinates all clones of a remote.
Internal steps continue the same pass. No timers or idle-age cleanup exist.

Maintenance resolves interrupted journals and dead holders first.
Unfinished work becomes parked. Safe candidates enter reuse before new builds.
Reuse completes full setup before ready publication.
Surplus safe candidates enter removal. Removed records remain available.
A setup or Git error stops the pass and retains its evidence.
Detached stdout and stderr go to a log. Status shows launcher and worker errors.

On macOS, process inspection requires `ps` and `lsof` with sufficient visibility.
On Linux it reads `/proc` for processes owned by the current UID.
Other users' processes are skipped. Unreadable same-user evidence blocks retirement.
On Windows, directory-use inspection reports uncertain evidence and blocks
automatic retirement. Claims and new builds remain available.

## Shared state and record schema

The checkout namespace is `<root>/<remote-basename>/`.
The remote URL and source common directory must agree across clones.
A basename collision fails with an identity error.

All lifecycle state belongs in git-kit's user-scoped plugin data home:
`~/.claude/plugins/data/plugins-kit/git-kit/worktrees/<repo>/`.
The repo key uses the same remote basename as the checkout namespace.
The path resolves through `bootstrap_guard.data_dir("git-kit")`, so
`CLAUDE_BOOTSTRAP_DATA_ROOT` redirects the plugin data tree for test sessions.
Worktree checkouts remain under the configured root.

| Path under the lifecycle state directory | Purpose |
|---|---|
| `namespace.json` | Version, remote URL, source clone, common Git directory. |
| `records/<stable-id>.json` | Durable record and current transition journal. |
| `logs/<stable-id>.log` | Setup command output and errors. |
| `claim.lock` | Holder reservation, rename journal, and retirement exclusion. |
| `fetch.lock` | Serialized source fetches. |
| `maintenance.lock` | One maintenance worker across clones. |
| `launch.lock` | Launcher/worker acceptance handshake. |
| `maintenance.json`, `maintenance.log` | Worker identity, outcome, and output. |
| `launcher-error.json` | Failed launcher operation and reason. |

Record version 1 contains `id`, `repo`, `path`, `state`, `operation`, `holder`,
`task`, `branch`, `head`, `upstream`, `boundary`, `waiting`, `setup`,
`processes`, `extension`, `safety`, `worker`, and `error`.
Operation stores name, phase, previous state, time, and rename paths.
Holder stores agent, session, claim, claim time, PID, and start identity.
Setup stores built OID, definition hash, input OIDs, selected steps, and
the final-path result. Errors store step, reason, exit code, and log.
Release retains `last_claim`. Dead-holder recovery retains `last_holder`.
Status adds live `observed` fields and marks stored safety as historical.
