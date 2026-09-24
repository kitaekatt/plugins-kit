# bootstrap

Installs each plugin's tools, Python venvs, and config automatically -- your
plugins just work.

## Why this exists

Most Claude Code plugins ship prompts and skills, which run anywhere. Almost
none ship *scripts*, because a script has to run on a machine you have never
seen -- unknown OS, unknown tools on PATH, no Python venv, no guarantee `uv`
or `gh` exists. Even the narrow problem of creating a Python venv for a
plugin automatically has no standard solution in the ecosystem today (the
going practice is a one-off `npm install` in a SessionStart hook).

Bootstrap is that missing layer. A plugin declares what it needs -- tools,
a venv from its `pyproject.toml`, git dependencies, config -- in a
`bootstrap.json`, and bootstrap brings each user's machine into that state
at session start. Every non-trivial plugin in this marketplace (p4-kit,
git-kit, unreal-kit, skills-kit) ships working Python to strangers'
machines on top of it, which is the existence proof that it works.

## What it does

Bootstrap is the dependency-management layer for the plugins-kit marketplace.
At session start it reads each installed plugin's `bootstrap.json` manifest
and puts the machine in the state that plugin needs:

- **System tools** -- uv, gh, jq, etc., installed via verified downloads
  (checksummed) when missing.
- **Per-plugin Python venvs** -- created with uv from each plugin's
  `pyproject.toml` at a stable path, with import checks after install.
- **Git dependencies** -- repos cloned once, pinned commits re-checked out.
- **Shared libraries** -- libs one plugin publishes and others import, linked
  across venvs.
- **Per-user config** -- config files seeded, autodetected where possible,
  with a fix-all prompt for anything that genuinely needs the user.

Users of the other plugins never run pip, create a venv, or edit PATH by
hand. If a check fails in a way bootstrap can fix, it fixes it silently; if
user action is truly required, it aggregates everything into one fix-all
message on the first prompt.

## Healthy bootstrap is silent

**No output at session start means every check passed.** Silence is the
success case, not a sign that bootstrap is broken. The first session after
installing a plugin may take longer while tools and dependencies download;
after that, steady state is quiet.

## The `bootstrap` command

Bootstrap installs a `bootstrap` command into `~/.local/bin` on every session,
so a pass can be inspected or driven from any terminal without starting Claude.
On Windows it works from Git Bash, cmd.exe and PowerShell: a `bootstrap.cmd`
beside the bash script runs it under Git for Windows bash.

```bash
bootstrap          # is a pass running? if so, wait for it and stream it
bootstrap --json   # report only, never blocking (the scripting form)
bootstrap run      # run the full bootstrap pass now, for the working directory
bootstrap codex-hook  # synchronous Codex SessionStart adapter
bootstrap reset    # clear this project's cooldown (--all, --status, --project)
```

`bootstrap run` runs the same engine pass as the SessionStart hook: it
refreshes declared marketplaces, updates declared plugins to newly published
versions, and processes every installed plugin's manifest, the four
user/project layers, and env.json personalization. The project is the exact
working directory, with no parent-directory search. It prints each candidate
layer path before the pass starts.

`bootstrap run` is not throttled by the cooldown, so it is the way to apply a
published update now. It does not consume or reset the cooldown, and it writes
no engine version stamps, log file, or per-project interpreter record. Its output
goes to the terminal. It exits 0 on a clean pass and 1 when the pass reports
failures.

A running pass makes `bootstrap run` refuse with exit code 2, because attaching
could inherit a different project scope. Bare `bootstrap` still follows a
running pass. A launched terminal run streams its own checks and actions.

`bootstrap reset` is the other half: it runs no pass, it clears the cooldown
stamp and the session-id guard so the NEXT session start is a real pass. It is
the right move on a machine where bootstrap seems to be doing nothing. It is the same lever as the
`bootstrap-reset-cooldown` command, which keeps its own name on PATH; all of
its flags, `--help` included, pass straight through.

With more than one marketplace installed, `bootstrap` reports on all of them
and `bootstrap run` asks you to set `BOOTSTRAP_MARKETPLACE` rather than guess
which engine to run.

After a clean automatic pass with the applicable ignore policy already in
place, bootstrap creates a machine-local `.codex/hooks.json` with a
`SessionStart` hook. Starting Codex then runs the same full bootstrap engine
and injects its remediation context into the Codex session. If the policy is
missing, Claude receives the remediation and bootstrap defers hook creation so
the first generated `.codex` tree cannot leak into source control. The hook
rechecks the project's `.gitignore` and applicable `.p4ignore` for `/.codex/`;
Perforce remediation tells the agent to run `p4 edit .p4ignore` before changing
that file. Codex may require a one-time review/trust of the generated project
hook through `/hooks`.

To verify bootstrap actually ran for a plugin, read its log:

```
~/.claude/plugins/data/plugins-kit/<plugin>/bootstrap.log
```

If the log does not exist, bootstrap never reached that plugin. The usual
cause is the per-project cooldown: after a successful pass, bootstrap skips
re-checking for a window. If a plugin misbehaves right after an update, clear
the cooldown and start a new session:

```bash
bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh
```

If a Python venv itself looks wrong -- uv reports a Python it cannot run, or
a Windows junction under uv's python directory does not resolve -- diagnose
it directly:

```bash
bash plugins/bootstrap/scripts/diagnose-python-venv.sh
```

It reports uv's installation and installed Python versions, checks uv's
python directory for the Windows junction/mount-point problem, and inspects
the bootstrap plugin's own venv (`pyvenv.cfg` and whether its Python runs).
Read-only -- it makes no changes to the system.

## What it writes outside its own directory

Bootstrap provisions the machine, so it deliberately writes beyond the plugin
directory. The honest list:

- `~/.claude/plugins/data/` -- per-plugin venvs, synced shared libs, config,
  logs, and bootstrap's own state files.
- `~/.local/bin` -- downloaded tool binaries (uv, gh, jq, ...).
- `~/.local/share/python-standalone` -- a standalone CPython build, installed
  only when no suitable Python exists on the machine.
- **Windows user PATH (registry)** and **shell rc files** (bash/zsh) -- adds
  `~/.local/bin` and related entries.

The PATH and rc edits exist for one reason: plugin scripts must be able to
find their tools from any shell, not just the session that installed them.

## Install

Usually you do not install bootstrap directly -- every plugin in this
marketplace declares it as a dependency, so installing any of them pulls
bootstrap in automatically. Manual install, if you want it explicitly:

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install bootstrap
```

## When not to use it

Bootstrap is the substrate for this marketplace's plugins. There is no reason
to install it standalone -- on its own it provisions nothing you would use
directly. Pointing bootstrap at a *different* marketplace is not supported
yet -- its self-setup and discovery are currently wired to plugins-kit. That
decoupling is a deliberate future step, not a limitation of the approach; the
problem it solves (above) is the same for any marketplace that wants to ship
real tools.
