# The `bootstrap` command

A PATH command for inspecting and driving a bootstrap provisioning pass from a
terminal, without starting Claude. It ships with this plugin and installs
itself; nothing about it is per-machine setup.

## Invocation

```
bootstrap                 report whether a pass is running; if one IS, stay
                          attached and stream it until it finishes
bootstrap --json          report only, never blocking -- the scripting form
bootstrap run             the same, and START a pass when none is running
bootstrap run --verbose   trailing flags pass through to the engine
bootstrap -h | --help     usage
```

In their handling of a pass that is already running, the two verbs differ in
exactly one clause: `run` also launches when nothing is. Neither ever starts a
SECOND pass. They do diverge elsewhere -- on an ambiguous marketplace, and in
their exit codes; both are below.

## Why neither form starts a second pass

A pass is single-instance, guarded by `proc_lock.engine_lock`. An engine
launched beside a live one acquires nothing, stands down, and prints nothing --
so a naive `run` during a pass would look, from the terminal, exactly like
bootstrap doing nothing at all.

Both forms therefore read the lock FIRST and attach when it is held. The read is
`proc_lock.lock_holder`, a pure query: never try-acquire-then-release, because
acquiring clears a stale lock and holds the mutex for an instant, so a status
probe could make a genuine launcher stand down. It applies the identical
staleness rules the acquisition applies, so the two can never disagree.

`run` re-checks after its child exits, too. Between the up-front check and the
engine's own acquire, another launcher (a SessionStart, the harvest, the
mid-session relaunch) can win the lock; a pass holding it afterwards means that
happened, and `run` attaches rather than exiting on a false all-clear.

## Streaming

Both an ATTACHED pass and a LAUNCHED one are streamed. The console engine prints
its verdict and its failures to stdout and nothing else, so a clean pass that
takes minutes otherwise showed a few lines of shell preamble and exited. The
per-check detail lives in the event stream, and streaming it is the whole reason
this command blocks.

Either path drops `events.watch` in the plugin's data directory. That marker
switches the pass recorder from its normal buffered write -- two file writes per
pass -- to a flush throttled at one second, for as long as a reader is attached,
and the marker is removed on the way out (on the normal path, on an exception,
and on the Ctrl-C that is the likeliest way a tail ends). Without it there is
nothing to tail mid-pass.

Two consequences worth knowing:

- A tail attaches at the CURRENT end of the stream, so records a pass emitted
  before you attached are not replayed. `bootstrap.log` holds the completed
  record.
- The LAUNCH path suppresses the verdict record while tailing, because the child
  process is already printing that verdict to the same terminal.

## The cooldown

`bootstrap run` is exempt in BOTH directions and needs no reset first.

It is never throttled BY the per-project cooldown: `--console` reads no hook
stdin, so the session-id guard never engages, and both skip gates exempt it from
the always-lane downgrade. It also does not WRITE the cooldown stamp -- an
explicit run from a terminal is not the session-start schedule, and advancing
that schedule would let a manual run silently consume the next session's pass.
Bypassing a throttle while still arming it for someone else is half a bypass.

## Exit codes

| Form | Code |
|---|---|
| bare `bootstrap` | always 0 -- "running" and "not running" are both correct answers to the question asked |
| `bootstrap run`, pass started | the engine's own exit code |
| `bootstrap run`, attached to someone else's pass | 0 |
| `bootstrap run`, ambiguous marketplace or no plugin tree | 2 |

Read `--json` to learn whether a pass was running. Never `$?` for the bare form.

## Marketplace scoping

The command acts on the marketplace that has a bootstrap data directory under
`${CLAUDE_BOOTSTRAP_DATA_ROOT:-~/.claude/plugins/data}`. It does not assume
`plugins-kit`: installed into `~/.local/bin` as a copy, it cannot derive its own
marketplace from its path.

With more than one, the bare command reports on all of them and follows one only
when exactly one is running -- tailing two engines at once would attribute lines
to the wrong one. `run` REFUSES rather than guess, because launching the wrong
engine provisions the wrong machine state silently. `BOOTSTRAP_MARKETPLACE`
names one.

## Pointing it at a different tree

`BOOTSTRAP_PLUGIN_ROOT` outranks discovery when set. It is the only way to run a
tree that is not the installed one -- a dev checkout, a worktree. Without that
precedence the command launches the INSTALLED engine while naming the requested
root, so a fix under test never runs and the run looks like it did.

Unset, resolution prefers the highest installed cache version, because that is
the code Claude Code actually loads; the marketplace clone is the fallback for a
machine with no cached install yet. Version directories sort numerically per
component, since lexical order puts `0.98.1` above `0.104.0` and would run a
superseded engine.

## How it reaches a machine

Nothing is installed by hand, and no per-machine configuration is involved.

1. `scripts/bootstrap.sh` and `scripts/bootstrap_cli.py` ship in this plugin.
2. The SessionStart hook copies them to `~/.local/bin/bootstrap` every session,
   alongside `bootstrap-reset-cooldown` and `env-reset-cooldown`. Unix gets a
   symlink so plugin updates flow automatically; Windows gets a copy, because
   symlinks there need elevation. Re-installed every session, so the lever
   tracks the cached plugin version and returns if deleted.
3. `~/.local/bin` is already on PATH because bootstrap puts it there -- it is
   where the engine installs `uv`, `gh`, standalone Python, and the `claude` CLI
   itself. The directory is declared in the plugin's own default config, and the
   engine persists it to the Windows user PATH (registry) and to shell rc files.

`bootstrap.sh` is a thin shim: it resolves the plugin tree and an interpreter,
then hands off to `bootstrap_cli.py`, which holds the behavior. The shim never
installs Python -- a status probe must not be able to trigger a multi-megabyte
download -- so on a machine whose first pass has not run, `bootstrap run` works
(the pass installs Python as its first act) and the status form reports that it
cannot read the lock.

## Troubleshooting

**`command not found` right after a first pass.** The PATH entry is persisted to
the registry or the rc files, but a shell that was already open does not have
it. Open a new terminal.

**It blocks.** That is the intended behavior whenever a pass is running, not a
hang. `--json` is the form that always returns immediately, and is what a script
or a hook should call.

**It printed a few lines and exited.** With nothing running, that is the whole
report. With something running it should have attached -- check the data
directory for a lock file whose recorded PID is alive.

**A left-behind `events.watch`.** Harmless but not free: every later pass flushes
once a second for a reader who has gone. Deleting the file restores the buffered
write.
