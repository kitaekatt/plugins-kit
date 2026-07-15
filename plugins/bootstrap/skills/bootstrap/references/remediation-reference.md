# Remediation Reference

Detailed check methods and remediation actions for all condition categories the bootstrap engine can handle. For a summary table, see the SKILL.md "Remediable Condition Categories" section.

## Configuration Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Directory not in PATH | Read shell RC files or query OS environment variable | Modify persistent PATH configuration (platform-specific) |
| JSON file lacks expected entries | Compare reference entries against target file | Merge missing entries into target JSON |
| Application config setting not enabled | Read config/ini file for setting value | Write setting to config/ini file |

## Tool Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| CLI tool not installed | `shutil.which(name)` | Run platform-specific install command -> re-check -> escalate to fix-all only if still missing |

## Library / Data Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Python venv missing or broken | Check dir -> binary -> interpreter runs -> packages importable | `uv sync` from `pyproject.toml` |
| Project venv missing or broken | Same checks against `<project_dir>/.venv` | `uv sync --project <project_dir> [--extra ...]` |
| PyPI package missing | Check extracted file exists locally | Download from PyPI and extract |
| Git dependency not cloned / wrong branch / wrong pinned commit | Check dir exists + is a git repo + `rev-parse` matches the declared branch (or pinned `commit`) | Clone once; pinned commits are fetched + re-checked-out. No steady-state pull — an existing clone on the right branch is never updated against its remote |

## Marketplace Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Marketplace not registered | Check `known_marketplaces.json` for `installLocation` | `claude plugin marketplace add <url>` |
| Marketplace stale (`alwaysUpdate`) | Always (no check — unconditional on every session) | `claude plugin marketplace update <name>` |
| Marketplace pinned but clone at wrong commit | Resolve `pin` via `git rev-parse` (with `git fetch` + retry on a miss); compare resolved SHA to clone HEAD | `git checkout --detach <sha>`; force `autoUpdate: false` in `known_marketplaces.json`; record pin + prior `autoUpdate` in `marketplace_pins.json` |
| Pin removed from manifest but marker recorded | Compare manifest `pin` fields against `marketplace_pins.json` | `git checkout <default branch>` (origin/HEAD, falling back to master/main probing), then the normal marketplace update; restore recorded `autoUpdate`; remove the marker entry |
| Pin unresolvable (bad SHA/tag, clone missing) | `git rev-parse` still fails after fetch, or `installLocation` dir absent | Fix-all failure with guidance: check the SHA/tag, register the marketplace, or remove the pin |

While a marketplace is pinned, `alwaysUpdate` is skipped (a one-line "alwaysUpdate ignored while pinned" warning is emitted instead). See the `pin` section in [manifest-reference.md](./manifest-reference.md) for full semantics.

## Plugin Conditions

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Plugin not installed | Check installed plugins registry | Install plugin at declared scope |
| Plugin installed but unwanted | Check installed plugins registry | Uninstall plugin |
| Plugin out of date | `git ls-remote` vs cached commit SHA | Update plugin at declared scope |
| Plugin at wrong scope | Compare installed scope vs declared `scope` in manifest | Uninstall from current scope, reinstall at declared scope |

## Manual Operations (Blocking Conditions)

All manual operations represent a blocking condition where auto-configuration cannot complete without user intervention. These generate fix-all directives via the messaging protocol.

| Condition | Check Method | Remediation |
|-----------|-------------|-------------|
| Config information missing and can't be auto-detected | Check config file for required fields | Ask user for information, write to config file |
| External app requires config change and/or restart | Modification applied that requires restart | User restarts external application, types `fixed` |
| Claude Code requires config change and/or restart | Modification applied that requires restart | User restarts Claude Code |
| Operation needs elevation (sudo/UAC) in a non-interactive pass | Privilege probe (`sudo -n` / admin token) failed when the op ran | Deferred into ONE per-OS fix queue — `<data_dir>/elevate/queue.json` plus a `bootstrap-fix.{sh,bat}` launcher shim — surfaced as one aggregate `elevation_script` item. Windows: a `fix-all` re-run launches the fix runner itself (see below). Unix: user runs the shim; the next session's re-check clears it (nothing to confirm) |

### The fix queue and its runner

Deferred operations are serialized as **typed tasks** in
`<data_dir>/elevate/queue.json` — data, not generated shell text — and executed
by `bootstrap_lib/fix_runner.py` (`python fix_runner.py <queue.json>`), the one
place bootstrap has a TTY. The queue and its `bootstrap-fix.{sh,bat}` launcher
shim are rewritten every pass and deleted once nothing is deferred, so the offer
disappears when the operations succeed.

| Task kind | What the runner does |
|-----------|----------------------|
| `command` | `bash -c <command>`, `sudo`-wrapped on Unix when the task is `elevated` |
| `apt` | `apt-get update`, then one `apt-get install -y <all queued packages>` |
| `brew_installer` | Runs the official Homebrew installer. Never elevated — it refuses to run as root and elevates itself where it needs to |
| `secret` | Prompts with echo off, writes the value 0600 and user-owned. **Available but unwired** — no producer emits one yet |

The runner **prints the plan** before executing anything — one labeled line per
task, marked `admin` where elevated. A data file is more opaque than the shell
script this replaced, whose real virtue was that the user could read it before
approving; the plan restores that. The UAC (or sudo) prompt is the consent, the
plan is the disclosure.

It **continues past a failed task** rather than aborting on the first one (the
old `.bat` aborted): the tasks are independent, and the engine's next re-check —
not the runner — is the authority on what actually cleared, so a task that fails
here simply stays failed there. Exit codes: `0` every task completed, `2` at
least one did not, `3` the queue is unreadable or names an unknown kind (a
version skew — failing loudly beats skipping an elevated task silently, which
would look like success to the re-check).

**Privilege is per task, not per run.** On Unix the runner runs **as the user**
and wraps only `elevated` tasks in `sudo`, so anything it creates stays the
user's and `$HOME` is already correct — the old script ran wholesale under `sudo`
(`HOME=/root`), which is why it had to rewrite `~`. On Windows the engine
launches the whole runner elevated in one UAC hop; UAC preserves the user
profile, so `elevated` is effectively advisory there.

**Why a `secret` kind at all.** Elevation is not the only operation that needs a
console — gathering a secret hits the same wall for a different reason, and the
runner is the only place with a TTY to prompt on. Routing a secret through that
console also keeps it out of the hook output and the Claude transcript. It is
designed for, not yet wired up.

### fix-all is user consent for elevation (Windows)

A SessionStart pass must never trigger a UAC or sudo prompt. But when the user
**types `fix-all`**, that is an explicit interactive request for remediation —
so the fix-all re-run of the engine (invoked with the `--fix-all` flag, e.g.
`bash <plugin_root>/hooks/sessionstart/session-bootstrap.sh --console
--fix-all`) handles a non-empty fix queue by **launching the runner itself**:
`Start-Process -Verb RunAs -Wait -PassThru` on the engine's own interpreter, so
the wait covers the real elevated process rather than an unelevated shim that
relaunches itself and exits early. The UAC prompt appears, the engine waits up
to 10 minutes, and on success runs a re-check pass (without `--fix-all` — it can
never loop the prompt) so the elevated items clear in the same cycle. If the
user declines UAC, a task fails, or the wait times out, the engine reports the
outcome and falls back to the run-it-yourself shim.

On Ubuntu/macOS the fix-all run has no TTY for a foreground `sudo` (or a secret
prompt), so the shim remains the only path there. That asymmetry is **encoded,
not remembered**: the engine attaches a `fix_all_cmd` to the aggregate item
exactly when the launch can happen (Windows, and not already inside a fix-all
run — which doubles as the loop guard), and `_is_auto_fixable` reads that field
as the eligibility signal. So the item is genuinely fix-all eligible wherever it
is offered — the footer can no longer print "None of these are fix-all eligible"
directly above an item telling the user to type `fix-all`.

### What the user sees when elevation is the only issue

An elevation-only pass skips the numbered list and the log dump entirely and
emits the aggregate's own focused message:

```
Bootstrap found issues that need admin access: <labels>.

Type 'fix-all' to fix them. You'll be asked to approve an admin prompt.
```

`<labels>` are the queued tasks' labels — an entry's `description` when it has
one, else its `name` (tools read `Install <name>`). On Unix the second line
instead names the shim to run ("It asks for your password where needed"), and a
Windows launch that was declined or failed falls back to the same shim, prefixed
with the launch outcome.

The per-task `needs_elevation` items are **suppressed** from the numbered list
while the aggregate exists, since it speaks for them; without an aggregate they
surface raw rather than vanishing. There is **no `fixed` ritual on this path**:
the env gate re-runs the phase every session until `last_result` is clean, so
confirming would be redundant.

## Display Timing

Bootstrap results (including remediation instructions) surface on the **first user prompt after the engine completes**, not at session start. The SessionStart hook emits suppressed JSON immediately and forks the engine to the background. A UserPromptSubmit hook (`bootstrap-display.sh`) checks for `bootstrap_display.pending` on each prompt; if found, it emits the contents and renames the file to `bootstrap_display.displayed`.

This means the user can start typing immediately. If the engine finishes before the first turn completes, results appear on that turn. If the engine is still running (e.g. slow marketplace fetch), results appear on the next turn after completion.

## User Experience Outcomes

From the user's perspective, there are three possible outcomes on session start:

| What the user sees | What happened |
|--------------------|---------------|
| Nothing | All checks passed (or cache hit) — environment is ready |
| Nothing (first run after install) | Tool was missing, install ran silently, re-check passed — logged internally, no user-visible output |
| Nothing (very first session, fresh machine) | Python was being bootstrapped; the engine runs fully on the next session. No `bootstrap.log` exists yet. |
| Fix-all message | Something needs user action: install failed, no install command, missing config, or external app needs restart |

**Healthy steady state**: The user sees nothing. Bootstrap is working correctly when it's invisible.

**Verifying bootstrap ran**: Check `~/.claude/plugins/data/bootstrap/bootstrap.log`. Entries appear after the first successful engine run. No log file = engine hasn't completed a full session yet (normal on first run of a fresh machine).
