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
