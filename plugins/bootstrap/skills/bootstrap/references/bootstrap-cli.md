# The `bootstrap` command

A PATH command for inspecting Claude bootstrap passes and applying user/project
requirements from a terminal.

## Terminal run scope

`bootstrap run` merges and applies only these four files, in ascending priority:

1. `~/.claude/bootstrap.json`
2. `~/.claude/bootstrap.local.json`
3. `<working-directory>/.claude/bootstrap.json`
4. `<working-directory>/.claude/bootstrap.local.json`

Missing files are skipped. Later conflicting values win under shared manifest
merge rules. A parse error stops provisioning so a broken override cannot allow
lower-priority requirements to run unexpectedly.

The project directory is the exact working directory, with no parent or Git-root
search. From `/`, the project candidates are `/.claude/bootstrap.json` and
`/.claude/bootstrap.local.json`; user requirements still apply.

The command prints the engine tree and each candidate manifest's presence, then
streams checks and actions through the shared recorder. It uses the shared
manifest handlers directly, without installed plugin manifest discovery, the
legacy `user-bootstrap.json`, env.json personalization, self-provisioning, or
implicit project setup. Project operations such as `project_venv`, `project_npm`,
and `agent_skills_link` run when declared in the merged layers.

A `plugins` or `marketplaces` entry authored in one of these four files still
installs or updates what it explicitly declares. Installing a plugin does not
add that plugin's own manifest to this terminal run. Claude's automatic lifecycle
retains its full plugin-provisioning scope.

## Invocation

```bash
bootstrap                 # report; follow a running lifecycle pass to completion
bootstrap --json          # non-blocking machine-readable status
bootstrap run             # apply the four user/project layers
bootstrap run --verbose   # accepted for console compatibility
bootstrap reset           # clear this project's next-session throttle
bootstrap reset --all     # all projects; --status and --project also supported
bootstrap --help
```

## Running passes and exit codes

The terminal runner uses the shared single-instance lock. If another pass is
running, `bootstrap run` refuses with exit code 2 and asks you to retry after it
finishes. It never attaches to that pass: its manifests or project may differ.
The runner also checks the lock atomically to cover a race after the initial
probe. Refusal does not alter lifecycle cooldowns or version stamps.

Bare `bootstrap` retains its status-and-follow behavior. `bootstrap --json`
always returns immediately. The status probe reads the lock without acquiring
or clearing it.

| Form | Exit code |
|---|---|
| Bare `bootstrap` | 0 whether idle or running |
| `bootstrap run` | 0 on success; 1 on manifest/provisioning failure |
| `bootstrap run`, busy or ambiguous marketplace | 2 |
| `bootstrap run`, missing plugin tree | 2 |
| `bootstrap reset` | The delegated reset script's exit code |

## Cooldowns and records

`bootstrap run` neither consumes nor advances the SessionStart cooldown. It also
leaves plugin lifecycle version stamps and env.json state unchanged.
`bootstrap reset` delegates to `bootstrap-reset-cooldown` to clear the cooldown
and session guard for the next genuine Claude session.

The CLI creates an `events.watch` marker while tailing and removes it afterwards.
The recorder retains console events in `bootstrap_events.jsonl`. Bare status
attaches at the current end of the event stream without replaying older output.

## Engine and data discovery

`BOOTSTRAP_PLUGIN_ROOT` selects an explicit engine tree. Otherwise discovery
prefers the highest cached bootstrap version, with the marketplace clone as
fallback. Version components sort numerically.

Data directories are discovered under
`${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}`. With multiple marketplaces,
`run` requires `BOOTSTRAP_MARKETPLACE` to choose its engine/data context. This
selection does not add plugin manifests to the four-layer run. Bare status can
report all marketplaces. Reset acts on all marketplaces unless scoped by the
environment.

The SessionStart hook installs the shell shim into `~/.local/bin/bootstrap`.
The shim resolves an existing interpreter and delegates to `bootstrap_cli.py`.
Terminal execution goes through `bootstrap_run.py`, which presents the shared
`bootstrap_lib.layered_bootstrap` capability. Without Python, let Claude's normal
lifecycle provision it first. Reset remains available without Python through
its shell delegate.
