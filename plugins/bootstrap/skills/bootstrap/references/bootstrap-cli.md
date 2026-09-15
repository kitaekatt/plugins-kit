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
bootstrap install-hook    # administrator: write the ensure-bootstrap hook here
bootstrap --help
```

`bootstrap install-hook` writes the project SessionStart hook that installs or
updates bootstrap on machines that lack it. It is documented, with its
opt-out, in fleet-management.md.

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

## The `profile` subcommands

```bash
bootstrap profile                    # show status, selection, chain, available
bootstrap profile --json             # same, plus an AskUserQuestion `question`
bootstrap profile --project-dir P    # resolve against project P, not the cwd
bootstrap profile set <name|none>    # select a profile, or "none" for base only
bootstrap profile set <name> --user     # write to ~/.claude/bootstrap.local.json
bootstrap profile set <name> --project  # write to <project>/.claude/bootstrap.local.json
bootstrap profile clear              # remove the selection
bootstrap profile clear --user       # clear from the user-local file specifically
bootstrap profile clear --project    # clear from the project-local file specifically
```

All three resolve state through the same engine function
(`bootstrap_lib.engine._load_layered_manifests_ex`) a live bootstrap pass uses,
so the CLI and the engine can never disagree about what is selected. `--json`
on the bare form adds a `question` field -- the AskUserQuestion payload for the
current status, `null` when the project declares no profiles at all -- so a
caller can go straight from `bootstrap profile --json` to asking the user with
no separate lookup.

**`bootstrap profile` (status).** Reports `status`, the current `selected`
name and its `source` file, the applied `chain`, every `available` profile
with its `description` and `extends`, any `warnings` or `errors`, and the
`write_target` a `set` with no `--user`/`--project` flag would use. Exit 0
always -- a status report is not itself an error, even under `invalid` or
`unknown`.

**`bootstrap profile set <name|none>`.** Refuses immediately, before reading
or writing anything, when a bootstrap pass currently holds the engine lock
(exit 2) -- a running pass may be about to rewrite the very local file this
command would write to. Otherwise: a named profile must be declared (exit 1,
nothing written, if it is not -- `none` is always accepted, even when the
current `profiles` declaration is `invalid`); the selection is written
atomically to the resolved target (every other key in that file is
preserved); a project-local write is additionally excluded from Git; and the
command then launches a bootstrap pass against the same project to converge
the new selection, streaming its output the same way `bootstrap run` does.
Without `--user`/`--project`, the target is the state's own `write_target`
(see manifest-reference.md). If no bootstrap plugin tree can be found to run
the converging pass, the selection is still written and reported, but the
command exits 1 and says to run `bootstrap run` manually.

**`bootstrap profile clear`.** Same lock refusal (exit 2) as `set`. Removes
the `profile` key from the resolved target file (or does nothing, exit 0, if
the file does not declare one) and does **not** converge -- it prints that
bootstrap asks again on the next bootstrap pass, which a skipped cooldown may
defer to a later session.

| Form | Exit code |
|---|---|
| `bootstrap profile` / `--json` | 0 |
| `bootstrap profile set`, wrote and converged | 0 |
| `bootstrap profile set`, unknown name, or write/converge failure | 1 |
| `bootstrap profile set`/`clear`, a pass holds the lock | 2 |
| `bootstrap profile set`/`clear`, ambiguous marketplace | 2 |
| `bootstrap profile clear`, wrote (or nothing to remove) | 0 |

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
