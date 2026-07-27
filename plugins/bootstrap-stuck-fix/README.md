# bootstrap-stuck-fix

**Remediation plugin, kept published as a safety net until every known
machine runs bootstrap >= 0.62.0 -- see "Distribution and withdrawal"
below.**

Repairs the defects that permanently wedge a machine on an old bootstrap
version. Two are covered, independent of each other -- a machine can have
either or both:

| Defect | Script | Shape |
|---|---|---|
| Malformed duplicate registry record | `repair_registry.py` | two records under one ref, one a `user`-scope/`projectPath` chimera |
| Update requested at the wrong scope | `repair_update_scope.py` | one well-formed record, but bootstrap asks the CLI for the *manifest's* scope |

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install bootstrap-stuck-fix
```

(The campaign normally distributes it via a tracked project `settings.json`
-- see "Distribution and withdrawal" below -- but a manual install works for
a single affected machine.)

## Defect 1 -- malformed duplicate registry record

An affected registry holds two records under `bootstrap@plugins-kit`:

```
{"scope": "user", "projectPath": "D:\\dev\\env-config", "version": "0.45.0", ...}
{"scope": "user",                                       "version": "0.52.0", ...}
```

The first is malformed -- a *user*-scope record carrying a `projectPath`.
Claude Code's trust/adoption flow writes it when a plugin is enabled in a
tracked **project** `.claude/settings.json` while bootstrap wants user scope.
A later `claude plugin install <ref> --scope user` does not match it and
**appends** instead of replacing, leaving two records where healthy plugins
have one.

## Why an affected machine cannot recover on its own

Every registry reader picks `entries[0]` -- the stale record:

- Claude Code's loader runs the SessionStart hook from the **old** cache dir,
  so newer engine code never executes.
- bootstrap's version check reads the old record, "updates" the *other* one,
  and logs `updated [old -> new]` every session. **The log lies**, so the
  machine looks healthy and nobody investigates.
- `harvest` -- whose entire job is "run the newest engine on disk" -- is
  blinded by the same first-entry pick and concludes it is already current.

`claude plugin uninstall` / `install` does **not** clear it (verified
empirically: the reinstall replaced the healthy record and left the malformed
one). So a fix shipped in a newer bootstrap can never reach an affected
machine. That is the entire reason this is a separate plugin: a brand-new
plugin has no prior version to be wedged on, so it installs and runs current
code on the first session.

## Symptoms

`engine_ran_version` never advances while the log claims it does:

```
cat ~/.claude/plugins/data/plugins-kit/bootstrap/engine_ran_version   # stuck
tail ~/.claude/plugins/data/plugins-kit/bootstrap/bootstrap.log       # "updated X -> Y" every session
```

### What it does

One narrow rule, bootstrap only:

> same ref, >1 records, at least one `scope: user` **without** `projectPath`,
> and one or more `scope: user` **with** `projectPath`
> -> drop the `projectPath`-bearing record(s)

It deliberately does **not**:

- touch `scope: project` records (a genuine per-project install is legitimate)
- touch `version` or `installPath` -- it removes a malformed duplicate, it does
  not force a version. The surviving record is well-formed Claude-Code-authored
  data, so there is nothing for Claude Code to re-sync and revert.
- act at all unless a healthy record would survive (better to leave a machine
  wedged than to deregister its bootstrap)
- act on any other plugin ref

Writes atomically, backs up to `installed_plugins.json.bootstrap-stuck-fix.bak`,
is idempotent, and exits 0 on every path -- it runs unattended on every session
start and must never break one.

**It takes effect on the NEXT session.** Claude Code reads the registry and
loads plugins at startup, before SessionStart hooks fire. So affected users need
two sessions to converge.

## Defect 2 -- update requested at the wrong scope

bootstrap takes the scope it wants from the project manifest
(`{"ref": "plugins-kit:bootstrap", "scope": "user"}`) and runs
`claude plugin update <ref> --scope user`. But the plugin is installed at
whatever the registry records -- commonly a genuine project-scope install:

```
{"scope": "project", "projectPath": "C:\\dev\\<project>", "version": "0.57.0"}
```

The CLI resolves by scope, does not find it at `user`, and refuses:

```
Failed to update plugin "bootstrap@plugins-kit":
Plugin "bootstrap" is not installed at scope user
```

Updating is orthogonal to scope: the plugin needs updating where it *lives*,
not where the manifest wishes it lived.

**Why the machine cannot recover on its own.** The failure blocks delivery of
its own fix. A corrected bootstrap can be published, but installing it is the
exact operation that fails -- so every *later* bootstrap fix is stranded behind
this one too. The machine reports the same error every session, forever.

`ensure_registry_scope` cannot help: it deliberately refuses to touch any record
carrying `projectPath`, because stamping a scope onto one manufactures the
defect-1 chimera. That refusal is correct -- this registry record is
well-formed, and nothing in the registry needs repairing. The wedge is purely
in which scope the request is made at.

### What it does

> exactly one bootstrap record, installed version < marketplace version
> -> `claude plugin update bootstrap@plugins-kit --scope <recorded scope>`

It deliberately does **not**:

- edit the registry -- the record is well-formed; this reads it, never writes it
- force or choose a version; the CLI picks, exactly as on a healthy machine
- act when >1 record exists (that is defect 1, and acting on an ambiguous
  registry could deregister bootstrap)
- act when already current, so the common path is two local file reads and
  spawns no subprocess

On success it writes `~/.claude/bootstrap-stuck-fix-update.json`. A *failed*
update prints one line rather than staying silent -- a silent persistent failure
is precisely how this wedge went unnoticed. Exits 0 either way.

Dry run:

```bash
python plugins/bootstrap-stuck-fix/scripts/repair_update_scope.py --dry-run
```

## Verifying reach

On repair it writes `~/.claude/bootstrap-stuck-fix.json` recording what was
dropped. That marker is the only evidence the campaign reached anyone -- check
it before withdrawing the plugin.

Dry run:

```bash
python plugins/bootstrap-stuck-fix/scripts/repair_registry.py --dry-run
```

## Distribution and withdrawal

Distributed by enabling it in a tracked project `settings.json`, so anyone who
syncs that file and starts a session receives the fix. Kept published as a
safety net (decision 2026-07-22): the repair is idempotent and silent on
healthy machines, so leaving it in place costs nothing and covers stragglers
that surface late. If it is ever withdrawn, remove that enablement.

**Superseded natively as of 2026-07-26.** bootstrap >= 0.62.0 carries both
repairs itself, so a machine running it never reaches either wedge:

- `plugins/bootstrap/bootstrap_lib/registry_repair.py` -- the chimera
  registry repair, and
  generalized to **every** ref rather than just `bootstrap@plugins-kit`
  (Claude Code's loader picks `entries[0]` for every plugin, so any ref's
  chimera pins that plugin to old code). Runs once per bootstrap pass, before
  any plugins phase.
- `marketplace_lifecycle.update_plugin` -- updates at the scope the registry
  records rather than the scope the manifest wants, so a genuine project-scope
  install is no longer refused by the CLI. This is the root-cause fix that
  `scripts/repair_update_scope.py` remediates after the fact.

Per user decision 2026-07-26 this plugin can therefore be **deleted** once
every known machine reports
`~/.claude/plugins/data/plugins-kit/bootstrap/engine_ran_version` >= 0.62.0:

```bash
cat ~/.claude/plugins/data/plugins-kit/bootstrap/engine_ran_version
```

Until then it stays published, because the native fix only protects machines
that are **not yet** wedged. A machine already stuck on a pre-0.62.0 bootstrap
cannot adopt 0.62.0 by itself -- the stall blocks delivery of its own fix -- so
it still needs this plugin to heal first, and only then does its
`engine_ran_version` advance.

Two notes for whoever withdraws it:

1. Machines that never ran it stay wedged permanently. That is accepted: the
   population is known and finite.
2. **Do not copy this distribution pattern for durable plugins.** Enabling a
   user-scope plugin via tracked *project* settings is exactly what triggers
   the malformed record this plugin exists to repair. It is acceptable here
   only because this plugin is disposable and never needs to update.

This plugin clears the existing backlog. It does nothing about *new* cases --
that requires the record-selection hardening in the bootstrap engine itself
(notably harvest picking the highest version rather than `entries[0]`, so old
engines launch newer on-disk code and future wedges self-resolve).
