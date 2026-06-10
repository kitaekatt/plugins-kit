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
