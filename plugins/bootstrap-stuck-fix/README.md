# bootstrap-stuck-fix

**Temporary remediation plugin. Scheduled for withdrawal -- see "Withdrawal" below.**

Repairs a malformed record in Claude Code's plugin registry
(`~/.claude/plugins/installed_plugins.json`) that permanently and *silently*
wedges a machine on an old bootstrap version.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install bootstrap-stuck-fix
```

(The campaign normally distributes it via a tracked project `settings.json`
-- see "Distribution and withdrawal" below -- but a manual install works for
a single affected machine.)

## The defect

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

## What it does

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
syncs that file and starts a session receives the fix. **Withdraw it after the
known user population has run it** (target: ~1 month) by removing that
enablement.

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
