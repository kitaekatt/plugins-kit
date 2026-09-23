# The `bootstrap` command

A PATH command for inspecting Claude bootstrap passes and running a full pass
from a terminal.

## Terminal run scope

`bootstrap run` launches the same engine pass as the SessionStart hook and
`bootstrap codex-hook`: `engine/bootstrap_engine.py`, in `--console` mode.
The pass does everything a hook pass does, in the same order:

- self-setup and registry repair;
- every declared marketplace refresh, including bootstrap's own
  `alwaysUpdate` entry, before any plugin version check (engine Step 3c-mkt);
- the four user/project layers, in ascending priority:
  1. `~/.claude/bootstrap.json`
  2. `~/.claude/bootstrap.local.json`
  3. `<working-directory>/.claude/bootstrap.json`
  4. `<working-directory>/.claude/bootstrap.local.json`
- plugin updates for declared (and self-registered) plugins, in the same pass
  that refreshed their marketplace;
- env.json personalization and every installed plugin's own manifest.

Missing layer files are skipped. Later conflicting values win under shared
manifest merge rules. A layer that fails to parse is reported as a failure.

The project directory is the exact working directory, with no parent or Git-root
search. From `/`, the project candidates are `/.claude/bootstrap.json` and
`/.claude/bootstrap.local.json`; user requirements still apply.

The command prints the engine tree and each candidate layer's presence, then
streams checks and actions through the shared recorder.

A terminal pass differs from a hook pass only where it must:

| Terminal pass | Why |
|---|---|
| Console output; no `bootstrap.log` writes, engine version stamps (`last_version`, `engine_ran_version`), pending display file, or fix-all queue | The terminal is the display; the version stamps drive the next session's update handling |
| Not throttled by the cooldown, and does not consume or reset it | It is the explicit request for a pass now |
| `--project-key _global_`: the per-project interpreter record is neither read nor written | The record is keyed by the hook's hash of Claude's cwd, which a terminal cwd cannot reproduce |
| No Codex project hook is generated | A console pass never materializes `.codex/hooks.json` in the directory it runs from |
| Refuses a held lock (exit 2) instead of attaching | A running pass may have a different project scope |
| Exits 1 when the pass reports failures (`--exit-status`) | A hook pass always exits 0 so a defect cannot block a session |
| stdin is closed | A manifest command or custom script cannot prompt on, or hang on, the terminal |

## Invocation

```bash
bootstrap                 # report; follow a running lifecycle pass to completion
bootstrap --json          # non-blocking machine-readable status
bootstrap run             # run the full pass now for the working directory
bootstrap run --verbose   # accepted; a console pass is already verbose
bootstrap codex-hook      # synchronous full pass + Codex SessionStart JSON
bootstrap reset           # clear this project's next-session throttle
bootstrap reset --all     # all projects; --status and --project also supported
bootstrap install-hook    # administrator: write the ensure-bootstrap hook here
bootstrap python          # print the project interpreter for the cwd
bootstrap python --engine # print the interpreter this lever runs under
bootstrap --help
```

Bare `bootstrap` and `bootstrap --json` also print `BOOTSTRAP_PYTHON=<value>`
and `BOOTSTRAP_PROJECT_PYTHON=<value>` for the current working directory,
before any blocking follow of a running pass -- which interpreter this lever
itself runs under, and which interpreter a project call from this directory
would resolve to right now. For an opted-out project the second line reads
`BOOTSTRAP_PROJECT_PYTHON is not set (...)`.

## The `python` subcommand

`bootstrap python` prints one path: the project interpreter for the current
working directory, by the rule the terminal resolvers apply (see
references/python-interpreter.md). Walking up from the directory, the first
directory that either opts out (`"project_python": false` in its
`.claude/bootstrap.json` or `bootstrap.local.json`; never read at the home
directory, whose file is the user layer) or holds a venv decides. Then: no
value for an opted-out project; else an activated `$VIRTUAL_ENV`; else that
venv; else the bootstrap interpreter. For an opted-out project it prints an
empty line and exits 1, so `"$(bootstrap python)"` never runs a guessed
interpreter.

It does **not** read the per-project record file the engine writes: the CLI
cannot reproduce the SessionStart hook's project key from a native working
directory, so this is a fresh resolution every time, not a cache lookup.
`bootstrap python --engine` prints only the interpreter this lever runs
under (the value `BOOTSTRAP_PYTHON` has inside a pass it launches), skipping
project detection entirely.

The resolver is `bootstrap_lib.interpreter_env` itself (`walk_opted_out` and
`default_project_python`), imported rather than re-implemented;
`bootstrap_lib` is stdlib-only, so the lever still runs before the engine's
own dependencies exist.

`bootstrap install-hook` writes the project SessionStart hook that installs or
updates bootstrap on machines that lack it. It is documented, with its
opt-out, in fleet-management.md.

`bootstrap codex-hook` is the runtime command used by the project-local Codex
adapter that normal bootstrap creates at the canonical project root's
`.codex/hooks.json`. It is not a separate hook-installation step. A clean
non-console pass first uses the existing Codex CLI detector; when Codex is not
available, it skips this setup. When Codex is available, bootstrap installs the
adapter even if the ignore policy is not ready. The command runs the full
engine synchronously and emits the normal Codex `SessionStart` JSON response.
It rechecks the policies and appends Codex-only remediation if they are
missing or drift. For a Perforce ignore file, the remediation requires the
`p4 edit .p4ignore` command before the file is changed. Bootstrap does not
create this adapter for a failed automatic pass or a `--console` diagnostic.

## Running passes and exit codes

The terminal pass uses the shared single-instance lock. If another pass is
running, `bootstrap run` refuses with exit code 2 and asks you to retry after it
finishes. It never attaches to that pass: its project may differ. The engine
also checks the lock atomically to cover a race after the initial probe, and
exits 2 when it stands down (`--exit-status`).

Bare `bootstrap` retains its status-and-follow behavior. `bootstrap --json`
always returns immediately. The status probe reads the lock without acquiring
or clearing it.

| Form | Exit code |
|---|---|
| Bare `bootstrap` | 0 whether idle or running |
| `bootstrap run` | 0 on a clean pass; 1 when the pass reports failures or crashes |
| `bootstrap run`, busy (before or during launch) or ambiguous marketplace | 2 |
| `bootstrap run`, missing plugin tree | 2 |
| `bootstrap reset` | The delegated reset script's exit code |

## Cooldowns and records

`bootstrap run` neither consumes nor advances the SessionStart cooldown, and it
does not write the engine version stamps. Because the cooldown does not throttle
it, it is the way to force a pass now: for example, to apply a plugin version
published since the session started. `bootstrap reset` delegates to
`bootstrap-reset-cooldown` to clear the cooldown and session guard for the next
genuine Claude session.

The CLI creates an `events.watch` marker while tailing and removes it afterwards.
The recorder retains console events in `bootstrap_events.jsonl`. Bare status
attaches at the current end of the event stream without replaying older output.

## The `profile` subcommands

```bash
bootstrap profile                    # show status, selection, chain, available
bootstrap profile --json             # same, plus `question`/`profile_listing`/`needs_typed_choice`
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
on the bare form adds three fields the human form does not need: `question` --
the AskUserQuestion payload for the current status, `null` when the project
declares no profiles at all; `profile_listing` -- every available profile as
plain text, one per line (`""` under `no_profiles`); and `needs_typed_choice`
-- `true` when more than three profiles are declared, `false` otherwise
(always `false` under `no_profiles`). A caller can go straight from
`bootstrap profile --json` to asking the user with no separate lookup: print
`profile_listing` first when `needs_typed_choice` is `true` (the question
then names no profile and offers a typed-choice option instead), then ask
with `question` either way.

**`bootstrap profile` (status).** Reports `status`, the current `selected`
name and its `source` file, the applied `chain`, every `available` profile
with its `description` and `extends`, any `warnings` or `errors`, and the
`write_target` a `set` with no `--user`/`--project` flag would use. The human
form renders `available` through the same `render_profile_listing` helper the
`--json` form's `profile_listing` field carries (indented two spaces for
terminal readability; the helper itself returns unindented lines), so the two
outputs cannot drift into two spellings of the same list. Exit 0 always -- a
status report is not itself an error, even under `invalid` or `unknown`.

**`bootstrap profile set <name|none>`.** Refuses immediately, before reading
or writing anything, when a bootstrap pass currently holds the engine lock
(exit 2) -- a running pass may be about to rewrite the very local file this
command would write to. Otherwise: a named profile must be declared (exit 1,
nothing written, if it is not -- `none` is always accepted, even when the
current `profiles` declaration is `invalid`); the selection is written
atomically to the resolved target (every other key in that file is
preserved); a project-local write is additionally excluded from Git; and the
command then launches the same pass `bootstrap run` launches, against that
project, to converge the new selection, and streams its output.
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
`run` requires `BOOTSTRAP_MARKETPLACE` to choose its engine/data context. Bare status can
report all marketplaces. Reset acts on all marketplaces unless scoped by the
environment.

The SessionStart hook installs the shell shim into `~/.local/bin/bootstrap`.
The shim resolves an existing interpreter and delegates to `bootstrap_cli.py`.
`run` and `profile set` launch `engine/bootstrap_engine.py` under
`BOOTSTRAP_PYTHON` (else the CLI's own interpreter) -- the same entry
`codex-hook` launches -- adding `--console --project-key _global_
--exit-status`. `codex-hook` passes none of these three flags. Without
Python, let Claude's normal lifecycle provision it first. Reset remains
available without Python through its shell delegate.

On Windows the hook also writes `bootstrap.cmd`, `bootstrap-reset-cooldown.cmd`
and `env-reset-cooldown.cmd` beside the extensionless levers. cmd.exe cannot
run an extensionless bash script, and Windows PowerShell 5.1 resolves one but
does not run it: the command returns at once with no output. Both shells run
the `.cmd` instead. It starts the lever under the Git for Windows bash that the
hook ran under, by absolute path, so it never reaches WSL's `System32\bash.exe`.
It forwards all arguments and the exit code. If that bash has moved, the `.cmd`
exits 127 with a message, and the next session start rewrites it. There is no
`.ps1` twin: PowerShell prefers a `.ps1`, and the default Restricted execution
policy refuses it. Source: `hooks/sessionstart/lever-cmd-shim.sh`.
