# Bootstrap Engine Internals

How the bootstrap engine discovers, processes, and remediates plugin dependencies on session start.

## Two-Phase Architecture

Bootstrap uses a fire-and-forget model to avoid blocking session start:

1. **SessionStart hook** (instant): Runs only the skip gates on the foreground path, emits `{"continue": true, "suppressOutput": true}`, then dispatches ALL provisioning work — PATH setup, Python ensure/download, Windows registry PATH writes, the engine launch (`--background`) — as a detached `_provision` subshell with every fd redirected. The redirection is load-bearing, not hygiene: Claude Code blocks session readiness on the hook's process exit AND stdout-pipe EOF (measured 2026-07-20; a child that inherits stdout blocks exactly like foreground work). Foreground cost is ~tens of ms on every path, including cold start; contract pinned by `tests/bootstrap/test_sessionstart_detach.py`.

2. **Engine (background)**: Runs all checks (tools, venv, marketplace, plugins, etc.), writes results to `bootstrap.log`, and — if there's anything to display — writes display JSON atomically to `bootstrap_display.pending` in the data directory. When everything passes silently, no pending file is created.

3. **UserPromptSubmit hook** (`hooks/userpromptsubmit/bootstrap-display.sh`, every prompt, ~0ms when idle): Checks for `bootstrap_display.pending`. If present, emits its contents and renames it to `bootstrap_display.displayed`. If absent, exits immediately with no output. The `.displayed` file is preserved for debugging and as a handshake signal — the engine can overwrite it with a new `.pending` file when fresh results are available. **Why UserPromptSubmit (not Stop)**: UserPromptSubmit supports `hookSpecificOutput.additionalContext`, which injects the log + remediation directives into Claude's context; Stop hooks reject `hookSpecificOutput` via schema validation. The pending JSON therefore carries `systemMessage` (user-facing) plus `hookSpecificOutput` with `hookEventName: "UserPromptSubmit"` and `additionalContext` (Claude-facing). There is no `decision`/`reason` field.

This means users see bootstrap results on the first turn after the engine completes, rather than waiting for the engine before the session starts. Console mode (`--console`) bypasses this entirely and runs synchronously with plain text output.

## Engine Phases

The bootstrap engine has two distinct setup phases:

1. **Self-setup** (step 3): Engine prerequisites — tools, PATH entries, an optional Windows Python-stub check, and venv — declared in `config.json` under `self_setup`. These make the engine itself runnable (e.g. uv, git, PyYAML). Processed before any `bootstrap.json`. The Python-stub check is Windows-only and fires only when a `python.exe` matching one of the configured `stub_markers` (default: `WindowsApps`) is the first hit on PATH ahead of the bootstrap-installed standalone Python; on failure it writes a self-elevating `fix_python_path.bat` to the user's Desktop and adds a fix-all entry asking the user to run it as administrator. On non-Windows machines and on Windows machines without the problem, it's silent.
2. **Plugin bootstrap** (step 4): Ecosystem management — marketplaces and plugins — declared in each plugin's `bootstrap.json`. The engine auto-discovers which installed plugins need bootstrapping by scanning for `bootstrap.json` in each plugin's install path (resolved from `plugins/installed_plugins.json`, **falling back to the cache layout** — see below).

   **Registry-v2 fallback** (`plugin_resolve.discover_cache_plugins`, added 0.47.0): newer Claude Code keeps `installed_plugins.json` at `{"version": 2, "plugins": {}}` for marketplace installs — enablement lives in settings `enabledPlugins` and the code in `~/.claude/plugins/cache/<mkt>/<plugin>/<version>/`. Observed live 2026-07-16: after wiping `~/.claude/plugins`, all plugins re-synced and ran but the registry stayed empty, so the engine provisioned nothing but bootstrap itself. For any *enabled* ref the registry doesn't record, discovery synthesizes the entry from the cache (highest version dir = the code Claude Code loads); registry entries always take precedence. The enablement filter comes from `_load_enabled_refs` (settings `enabledPlugins` + registry); with no enablement source at all the fallback stays off — never provision blindly. The harvest has the same fallback (`harvest._cache_installed_bootstrap`) for reading bootstrap's installed version.

   **Dev layout note**: When running the engine directly against the source tree (e.g. `python plugins/bootstrap/engine/bootstrap_engine.py --plugin-root plugins/bootstrap ...`), `plugins/installed_plugins.json` does not exist. `list_enabled_plugins()` returns `[], False` and sibling plugins (unreal-kit, p4-kit, ...) are not auto-discovered. This is expected and not part of any real dev workflow — the engine runs cleanly with no plugin output.

Discovery results are cached in `plugins/data/plugins-kit/bootstrap/config.json` under `bootstrap_cache` to avoid repeated filesystem scans — entries are added on first discovery and removed if `bootstrap.json` disappears (e.g. after a plugin update). Users can permanently opt out a plugin by adding its ref to `no_bootstrap` in that config file.

### Step 3b2: Registry chimera self-repair (`registry_repair.py`, 0.62.0)

Claude Code's registry can hold a malformed *chimera* record: a `scope: "user"`
record that also carries a `projectPath`, written by the trust/adoption flow
when a plugin is enabled in a tracked **project** `.claude/settings.json` while
the plugin wants user scope. A later `claude plugin install --scope user`
does not match it and **appends**, leaving two records under one ref. Claude
Code's own loader picks `entries[0]`, so the stale record decides which cache
dir the plugin loads from -- for bootstrap that means the old engine runs
forever while its log claims it updated (reported upstream as
claude-code#79892).

The engine repairs this once per pass, immediately after the venv activation
and **before any plugins phase** (the layered manifest in Step 3c and the
per-plugin manifests in Step 4 both run `_phase_plugins`), so version checks
and updates read a clean registry. The rule applies to **every ref**, not just
bootstrap -- the `entries[0]` pick breaks any plugin the same way:

> \>1 records, at least one `scope: user` **without** `projectPath`, and one or
> more `scope: user` **with** `projectPath` -> drop the `projectPath`-bearing
> record(s)

Deliberately narrow: `scope: "project"` records are never touched (a genuine
per-project install is legitimate), `version` / `installPath` are never
rewritten (it removes a duplicate, it does not force a version), and a ref is
skipped entirely unless a healthy user record survives -- better a wedged
machine than a deregistered plugin. Unreadable or unparseable registries are a
silent no-op; this runs on every session start and must never break one.

Write discipline mirrors `ensure_registry_scope`: back up to
`installed_plugins.json.registry-repair.bak` (best-effort), write atomically,
and **only when records are actually dropped** -- the registry's mtime arms the
SessionStart cooldown's registry-change bypass, so a no-op rewrite every pass
would re-arm a full bootstrap pass every session. Dropping records emits an
`action_entry` naming the refs and versions; a clean registry emits a
verbose-only `ok_entry`.

Takes effect on the **next** session: Claude Code reads the registry and loads
plugins at startup, before SessionStart hooks fire.

This is the root-cause fix, for machines that are **not yet** wedged. It does
not remediate machines already stuck on an older bootstrap -- they cannot adopt
0.62.0 by the very mechanism that is broken there. Those still need the
separate `bootstrap-stuck-fix` plugin (`scripts/repair_registry.py`), which has
no prior version to be wedged on. See the delivery-path rule in the repo
CLAUDE.md and the `update_lifecycle` fact in the bootstrap SKILL.md.

### Plugin updates target the recorded scope

`marketplace_lifecycle.update_plugin` runs `claude plugin update <ref> --scope
<scope>` with the scope the registry says the plugin is **installed at**, not
the scope the manifest wants. Update where it lives, not where the manifest
wishes it lived: the CLI resolves by scope and refuses outright ("Plugin X is
not installed at scope user") on a mismatch, which wedged updates forever for
genuinely project-scoped installs. The authoritative record comes from
`plugin_resolve.pick_registry_record` (which prefers the non-`projectPath`
shape); the caller's `scope` argument is only the fallback for a registry with
no usable record (the registry-v2 empty `plugins` map). Scope *correction* is a
separate concern, handled by `ensure_registry_scope` and the scope-remediation
path in `_phase_plugins`.

The resolved scope drives **both** the `--scope` argument and the
`_run_claude_scoped(args, scope, project_dir)` call, so the settings file made
writable ahead of the CLI's rewrite (`settings_path_for_scope`) is the same one
the CLI actually writes. `install_plugin` / `uninstall_plugin` keep using the
caller's scope: those genuinely choose where a plugin should live, whereas an
update only follows where it already lives.

Same split as above: this prevents the stall, it does not clear one. A machine
already stalled needs `bootstrap-stuck-fix`'s
`scripts/repair_update_scope.py`, which runs the scoped update from outside the
broken path.

### Step 4 Processing Order

Plugins are processed in a deterministic order:
1. **Bootstrap plugin** (`plugins-kit:bootstrap`) — ensures marketplace updates happen first
2. **Same-marketplace plugins** (other plugins from plugins-kit) — alphabetically
3. **Other marketplace plugins** — alphabetically

This ordering ensures marketplace updates complete before dependent plugins check versions.

### Step 4b: Phase 2 Re-scan

After Step 4 completes, the engine re-scans for newly installed plugins. This handles plugins that were installed during Step 4 (e.g. via a `plugins` manifest entry or a bootstrap script that calls `install_plugin`). The re-scan:

1. Calls `list_enabled_plugins()` again (reads `installed_plugins.json` fresh from disk)
2. Filters out already-processed plugins using a `processed_plugin_refs` set
3. Processes only new plugins using the same `_bootstrap_single_plugin()` helper

This is a **single pass** — no recursive re-scanning. Plugins installed by Phase 2 plugins bootstrap on the next session start. This eliminates one of the two restarts previously needed: install + bootstrap now happen in the same session.

For each discovered plugin, the engine resolves the plugin's install path via `plugins/installed_plugins.json` (e.g. `~/.claude/plugins/cache/plugins-kit/unreal-kit/0.1.5`) and processes bootstrapping in two phases:

1. **Manifest phase**: If `bootstrap.json` exists, the engine reads it and calls the appropriate library primitives for each declared operation. No plugin code runs — the engine drives everything.
2. **Script phase**: If a bootstrap script exists, the engine imports it and calls its entry point. The script runs **in-process** within a try/except, so one plugin's failure doesn't affect others. Scripts share state with the engine (e.g. aggregating fix-all directives) and avoid subprocess overhead.

Either phase is optional — a plugin can provide just a manifest, just a script, or both.

### Step 4c: Shared-lib convergence sweep

Shared-library *consumer* links (writing `<lib>.pth` into a plugin's own venv, declared via `shared_lib_imports`) happen inline while that plugin's manifest is processed. If a consumer is processed **before** the owner publishes the lib (plugins run in sort order, so this is purely an ordering accident), the inline `link_shared_lib` soft-skips with *"not yet published; will retry next session"* — an avoidable extra session/restart.

After the full plugin loop (Step 4 + the 4b re-scan), **every owner has published**, so the engine runs one idempotent re-link sweep (`_shared_lib_convergence_sweep`) over all processed plugins: a consumer-before-owner link that skipped inline now succeeds in the **same** session. `link_shared_lib` returns `cached` when the `.pth` is already correct, so consumers that linked fine inline are cheap no-ops (their `cached`/`skipped` results go to `ok_entries`, which are verbose-only). In steady state the sweep is fully silent; it only surfaces a section when it genuinely converged or failed a link.

This is the engine-side half of "provision everything in one pass." The shell-side half is the cooldown registry-change bypass (see [Throttling](#throttling)): together they remove the common reasons a user had to reload Claude more than once after a plugin update.

### Step 4d: Reload/restart advisory

The two fixes above let bootstrap provision a plugin's deps/libs/venv in a single pass. The one thing bootstrap **cannot** do in-session is make Claude Code load plugin *code & hooks* — Claude Code loads plugins at session start, before this SessionStart hook runs. So when a pass can **prove** the running session is missing a plugin's code, it emits a notice. The notice is **informational, not action-required**: it rides in the normal display output (label `<mkt>:bootstrap@<v> notice`) with **no relay directive** in `additionalContext` — whether and when to restart is the user's call (the old "ACTION REQUIRED — surface this now" preamble made Claude present routine update notices as urgent; removed 2026-07-16).

The provable case: a plugin that **entered the registry during this pass** — a layered `plugins:` install (Step 3c), a per-plugin install, or a bootstrap script's `install_plugin` (Step 4b). Claude Code loaded plugins *before* this hook installed those, so they aren't active yet. The engine detects them by **diffing the installed-plugins registry** (snapshot before Step 3c vs after Step 4b: `_read_installed_plugins` + `_resolve_newly_installed`) — **not** Step 4b's `new_plugins`, which silently misses a layered `plugins:` install (that lands in the registry *before* Step 4's scan, so Step 4 absorbs it and it never appears in `new_plugins` — the gap the cache-kit end-to-end test surfaced). `_reload_advice(newly_installed)` then builds a one-line, user-facing advisory, branching on whether the new plugin registers a **`SessionStart`** hook (`_plugin_ships_sessionstart_hook`):

- **Registers a `SessionStart` hook** → *restart Claude (or restart your IDE if Claude runs inside one)* — only a fresh session re-fires `SessionStart`; `/reload-plugins` reloads its registration but won't re-run it.
- **otherwise** (skills/commands/event-hooks only) → *run `/reload-plugins`* to load it in-session.

The `SessionStart`-specific branch is deliberate and **measured** (not the old "any hook → restart" folklore): `/reload-plugins` *does* reload hook **registrations** in-session, and a hook's **script content** is read live from disk every run. The only thing a reload can't do is re-*fire* `SessionStart`. Full rule + the probe method: [plugin-reload-lifecycle.md](plugin-reload-lifecycle.md).

Crucially, a plugin merely **updated** at session start gets **no notice**: the restart that applied the update already loaded its new code, and Parts 1+2 provisioned its deps in that same pass — a notice there would be noise on every publish. The advisory is gated behind config `notify_reload_needed` (default `true`); set it `false` to silence it.

The inverse failure — a SessionStart pass that never ran at all because Claude Code was still syncing the marketplace when SessionStart fired (fresh machine) — is handled hook-side by the **SessionStart-missed rescue** in `bootstrap-display.sh`: see [plugin-reload-lifecycle.md](plugin-reload-lifecycle.md).

#### Declarative reload policy (proposed — not yet implemented)

Today `_reload_advice` *infers* restart-vs-reload from whether the plugin registers a `SessionStart` hook (`_plugin_ships_sessionstart_hook`). A more accurate, author-controlled version is for each plugin to **declare** its reload class in `plugin.json` (the universal manifest the engine already reads):

```json
"reloadPolicy": "restart" | "reload" | "none"
```

- `restart` — the plugin has hooks, a statusline, or another surface Claude Code loads at session start; changes need a full restart (and an IDE restart when Claude runs in one).
- `reload` — only skills/commands; `/reload-plugins` suffices.
- `none` — pure library/data (e.g. a `shared_libs` owner) consumed by freshly-spawned subprocesses; nothing Claude Code holds needs reloading.

`_reload_advice` would prefer this field and **fall back to hook-inference when absent** (so the convention is opt-in and backward compatible). It would also unlock safely nagging on *updates* (not just installs): a plugin that declares `restart` can warrant a nag when its hooks may be stale, while `none`/`reload` plugins stay quiet. The "shape of changes → required action" taxonomy authors use to choose a policy is in the repo `CLAUDE.md`. Until this lands, the hook-inference default is in effect.

## First Run Lifecycle

On a clean install (wiping `~/.claude/plugins/`), Claude Code goes through distinct phases across restarts:

### Phase 1: Marketplace Sync

**Trigger**: Claude Code starts with `additionalKnownMarketplaces` pointing to the plugins-kit repo.

**What happens**:
- Claude Code clones the marketplace repo to `plugins/marketplaces/plugins-kit/`
- Records it in `plugins/known_marketplaces.json` with source URL, install location, and `lastUpdated` timestamp
- No plugins are installed yet

**State after Phase 1**:
```
plugins/known_marketplaces.json     <- marketplace registered
plugins/marketplaces/plugins-kit/   <- full repo clone
plugins/installed_plugins.json      <- {"version": 2, "plugins": {}}
```

### Phase 2: Plugin Install + Bootstrap Hook

**Trigger**: Claude Code starts again (second run). Marketplace is known; Claude Code installs enabled plugins.

**What happens**:
1. Plugin files copied to `plugins/cache/plugins-kit/bootstrap/0.1.0/`
2. Entry written to `plugins/installed_plugins.json` (scope, installPath, version, gitCommitSha, projectPath)
3. Bootstrap plugin's SessionStart hook fires (`session-bootstrap.sh`)
4. Hook detects no Python 3 -> downloads standalone Python runtime
5. Bootstrap engine runs

**State after Phase 2**:
```
plugins/cache/plugins-kit/bootstrap/0.1.0/   <- plugin files cached
plugins/installed_plugins.json               <- plugins-kit:bootstrap entry
~/.local/share/python-standalone/            <- standalone Python runtime
```

## Messaging Protocol

An optional protocol that bootstrap scripts can use to communicate with the engine. Scripts that use the protocol get structured features (fix-all aggregation, user messaging, re-run triggers). Scripts that don't use the protocol just run and return.

The engine collects messages from all plugin scripts and emits a unified response. The output format depends on the consuming hook:

- **SessionStart** (foreground, stdout): `hookSpecificOutput` with `hookEventName: "SessionStart"` and `additionalContext` injects instructions into Claude's context. `systemMessage` shows a summary to the user.
- **UserPromptSubmit** (background, via `bootstrap_display.pending`): the same shape with `hookEventName: "UserPromptSubmit"` — `additionalContext` carries the log + fix-all directives to Claude; `systemMessage` shows the summary to the user. Note: `systemMessage` alone is user-facing only — Claude never sees it, which is why the directives always ride in `additionalContext`.

## Execution Flow

The engine accepts a `--background` flag. When set, output is written atomically to `bootstrap_display.pending` in the data directory instead of stdout. The UserPromptSubmit display hook renames `.pending` to `.displayed` after emitting (handshake protocol). Background output uses UserPromptSubmit fields: `systemMessage` (user-facing) plus `hookSpecificOutput` with `hookEventName: "UserPromptSubmit"` and `additionalContext` (Claude-facing) — on failure, `additionalContext` carries the fix-all instructions so Claude can act on them. Non-background output (stdout, consumed by the SessionStart hook itself) is identical except `hookEventName: "SessionStart"`. When there's nothing to display (silent success with `log_success_checks` off), no file is written.

1. **Auto-run phase**: Bootstrap runs on session start. For each tool check, the engine runs check -> remediate -> re-check:
   - Tool present -> log `<name>: passed`, continue
   - Tool missing, install command available -> run install silently -> re-check:
     - Now present -> log `<name>: installed`, continue (no fix-all entry)
     - Still missing -> log `<name>: FAILED - install attempted but <name> not found in PATH`, add to fix-all
   - Tool missing, no install command -> log `<name>: FAILED`, add to fix-all

   This means most first-run tool installs (e.g. `uv`) succeed silently. The user never sees a fix-all message unless the install itself fails or no install command exists.

2. **Fix-all phase**: Only reached if one or more operations remain unresolved after remediation attempts (install failed, user action required, information unknown). The engine emits:
   - **Agent message**: What needs fixing and how to fix it (e.g. "Ask the user where the `.uproject` file is, then write that information to `{path}` as the value of the `UPROJECT_LOCATION` variable")
   - **User message**: What needs fixing and an instruction to type `fix-all` to remediate

   The user saying `fix-all` signals consent for Claude to gather information and apply results. It is also **consent for elevation**: at the end of a pass (step 7b) the engine harvests every deferred `elevation` descriptor into a typed fix queue (`<data_dir>/elevate/queue.json` + a `bootstrap-fix.{sh,bat}` shim, both deleted when nothing is deferred) and appends one aggregate `elevation_script` item. The interactive re-run of the engine passes `--fix-all` (`bash <plugin_root>/hooks/sessionstart/session-bootstrap.sh --console --fix-all`), and on Windows a `--fix-all` pass with a non-empty queue launches the fix runner itself (`Start-Process -Verb RunAs -Wait` on the engine's interpreter, bounded 10-minute wait), then spawns a re-check pass (without `--fix-all`, so it never re-prompts) so the elevated items clear in the same cycle; decline/failure/timeout falls back to the run-it-yourself shim with the launch outcome. SessionStart passes never carry `--fix-all` and never launch or prompt; Ubuntu/macOS stay manual (no TTY for a foreground sudo or a secret prompt). The aggregate is fix-all eligible exactly where that launch can happen — the engine attaches a `fix_all_cmd` only on Windows outside an existing fix-all run, and `_is_auto_fixable` treats that field as the signal. Details: [remediation-reference.md](remediation-reference.md#the-fix-queue-and-its-runner).

3. **Fixed phase**: After the user performs a manual action (e.g. restarting an external application), they type `fixed`. This signals Claude to re-trigger the bootstrap script, which should complete the remaining steps without requiring a Claude Code restart. The elevation path is exempt: it never asks for `fixed`, because a non-clean result re-runs those checks every session until they pass (see [remediation-reference.md](remediation-reference.md#what-the-user-sees-when-elevation-is-the-only-issue)).

## Throttling

The one real throttle is the **per-project session cooldown** applied by the shell hook (below). There is no generic per-check content-hash or time-based throttle in the engine — every pass re-runs every declared check. The two narrow caches that do exist are unrelated to throttling passes: the **plugin-discovery cache** (`bootstrap_cache` in `config.json`, see "Engine Phases" above — remembers which installed plugins have a `bootstrap.json` so discovery skips filesystem scans) and the **shared-lib sync hash** (`sync_shared_lib` content-hashes the package source and skips the copy when unchanged).

`session-bootstrap.sh` applies **two** skip gates before launching the engine, checked in order (Layer 1 then Layer 2). Both are honored by default and both **bypass on a registry change** (`installed_plugins.json` / `known_marketplaces.json` newer than their stamp), so a published update is never skipped; `bootstrap-reset-cooldown` clears both.

### Session-id guard (shell hook, Layer 1)

When the SessionStart hook receives a `session_id` on stdin, the shell stamps it to `data/<marketplace>/bootstrap/last_session_id` and **skips** a later invocation that presents the *same* id — a cheap "don't double-fire within one session" dedup (a single SessionStart can fire more than once, and non-blocking hooks may not receive stdin). **Registry bypass:** the skip is taken only when no alert is pending **and** neither `installed_plugins.json` nor `known_marketplaces.json` is newer (`-nt`) than the guard stamp. This is load-bearing because **`claude --resume` re-presents the original session's `session_id`** — without the bypass a resumed session would skip bootstrap entirely even right after an update landed, so the new version never provisions until an unrelated fresh session. The fall-through re-stamps `last_session_id` (bumping its mtime), re-arming the guard so the next genuine same-session repeat with no new change still skips. (Mirrors Layer 2's bypass; the bypass was added after a live test showed `--resume` updates silently stalled.)

### Per-project session cooldown (shell hook, Layer 2)

Above the engine, `session-bootstrap.sh` applies a coarse **per-project cooldown**: the shell stamps `data/<marketplace>/bootstrap/cooldowns/last_run_epoch.<sha1-of-cwd>` before launching the engine, and subsequent SessionStart hooks within the 3600s window skip the entire engine invocation. A skip is silent and **does not refresh the stamp** — the stamp records when bootstrap last *actually ran*.

**Registry-change bypass.** The cooldown is bypassed (a real pass runs) when either `installed_plugins.json` or `known_marketplaces.json` is **newer** (mtime) than the cooldown stamp. Claude Code rewrites those files whenever it installs/updates/rescopes a plugin or adds/refreshes a marketplace, so a version bump always re-arms a bootstrap pass on the next session instead of being throttled out. Because skips don't refresh the stamp, the bypass stays armed across *every* restart until a pass actually re-provisions — this is what stops a freshly-published shared-lib owner from leaving consumers importing a stale `_shared_libs` copy (the version looked current while the lib stayed old). The bypass uses `-nt`, which is false when the registry file is absent, so the cooldown is honored by default. Force a pass out-of-band with `bootstrap-reset-cooldown`.

**End-of-pass restamp.** After a *clean* pass the engine refreshes the stamp itself (`_restamp_project_cooldown`). The pass may have rewritten `installed_plugins.json` (plugin installs, `ensure_registry_scope`), and the shell's mtime bypass compares those files against the stamp written *before* the engine ran — without the restamp, bootstrap's own registry writes would re-arm a full pass on every session. The restamp only refreshes an existing stamp (creating it stays the shell hook's job, so console runs and tests never plant cooldowns). On a **failed** pass the engine instead *clears* the stamp (`_clear_project_cooldown`, also done by the crash handler), so the next SessionStart retries instead of throttling on a broken state.

### Single-instance lock (`proc_lock.py`, in-process, engine-wide)

The two shell-level gates above throttle *re-launching*; neither one checks whether an engine pass is **actively running right now**. Rapid session start/exit/restart can fire several independent launchers (`session-bootstrap.sh`, the harvest, the SessionStart-missed rescue) within the same few seconds, before any of them has completed and stamped its own guard -- producing several concurrent `bootstrap_engine.py` processes (observed directly: three at once, one of which crashed mid-pass in the shared-lib sync). `main()` wraps the whole pass in `proc_lock.engine_lock(data_dir)`, a PID-lock file (`engine.lock` in the engine's data dir) acquired via an atomic exclusive create. A second process that can't acquire it stands down without running `_main()` at all.

The lock is **engine-wide, not per-project** -- concurrent passes from *different* projects still race the same shared resources (`_shared_libs` sync, the registry), so serializing globally is deliberate, not an oversight.

**Version-aware arbitration.** The lock serializes engines but does not, on its own, pick the *right* one. `session-bootstrap.sh`'s `_provision` step runs **before** the lock is reached, so a harvest-launched NEW engine can arrive at the lock seconds after a resident OLD engine already took it -- and yielding there hands the pass to the older binary. `main()` therefore checks whether **this** engine carries the update (`_carries_update`: its own version, read from its `--plugin-root`'s `plugin.json`, is strictly greater than the global `engine_ran_version` stamp). If so it re-attempts acquisition on an interval for up to `_LOCK_RETRY_SECONDS` (`_retry_engine_lock`) before standing down; an engine that is *not* newer keeps the immediate stand-down. The retry is a loop over `proc_lock.engine_lock` in `engine.py` -- `proc_lock`'s own contract stays strictly non-blocking, because every other launcher depends on that.

**Stand-down rollback.** Standing down means the launcher that spawned this process already consumed its own one-shot guard before spawning it (the shell's per-project cooldown stamp is written *before* the engine launches). Left alone, the project that lost the race would get no bootstrap pass until that cooldown naturally expires. `_stand_down_lock_contended` clears that project's cooldown stamp so the next opportunity gets a genuine retry, and logs the stand-down to `bootstrap.log` naming this engine's own version and the holder's PID.

**Stand-down re-arms the harvest.** When the standing-down engine carries an update, `_stand_down_lock_contended` also **clears** `harvest_launched_version`. The older reasoning for leaving that marker alone -- "ANY completed pass stamps `engine_ran_version`, so the marker self-clears" -- is false when the pass that completes is an *older* engine: it stamps a version still behind the installed one, `should_harvest` stays true, and the already-consumed per-installed-version marker disarms every future harvest. That is a permanent wedge no later session recovers from without a manual pass. Clearing the marker is safe because the stamp is now monotonic (below): if a *newer* engine holds the lock, it completes and stamps `>=` our own version, making `should_harvest` false -- so a cleared marker cannot produce a duplicate spawn storm. The import-retry and registry-relaunch markers are still left alone; neither is version-keyed, so neither can wedge this way.

**Stale-lock recovery.** A crashed or killed holder's lock is recovered, not permanently wedged: on contention, `_try_acquire` reads the recorded PID and checks liveness (`os.kill(pid, 0)` POSIX, `OpenProcess` Windows); a dead PID makes the lock stale and it is unlinked and re-claimed through the same exclusive-create path (never a non-exclusive overwrite, which would let two racers both believe they won). A lock whose PID is alive but whose file has aged past a generous ceiling is *also* treated as stale, guarding against the PID-reuse case (an unrelated new process recycling the dead holder's PID number) wedging the lock forever.

**Elevation is the one caller that releases early.** The `--fix-all` flow's `_spawn_recheck_pass` synchronously spawns a full second `bootstrap_engine.py` process with the *same* `--data-dir` and waits on it -- while the parent is still inside its own `engine_lock()`. Without intervention the child would see the parent's still-alive PID as the lock holder and stand down without running its post-elevation re-check. `_spawn_recheck_pass` calls `proc_lock.release_lock(data_dir)` immediately before spawning; it is safe because the parent has no more work after the child exits (the caller returns immediately), and `release_lock` only removes the lock file if it still records the caller's own PID -- it can never touch a lock some other process has since legitimately acquired.

### Stamp files (`stamps.py`)

Bootstrap keeps several small string-valued **stamp** files — the cooldown epoch (`cooldowns/last_run_epoch.<sha1-of-cwd>`), `last_version` (engine + per-plugin), and `engine_ran_version`. They used to be ad-hoc (each call site rolled its own open/read/write). `bootstrap_lib/stamps.py` consolidates them behind one API with **one atomic-write convention** (mkstemp + `os.replace`, via `atomic_write`) and **one missing-file convention** (`read` returns the caller's default; never raises). It is generic over **scope** (`global_stamp` in the data dir, `plugin_stamp` in a plugin's data dir, `project_stamp` keyed by sha1-of-cwd) and **value** (an opaque string the caller parses — epoch int, version string); it bakes in no "timestamp" semantics.

**mtime is explicit on purpose.** The cooldown stamp's mtime is load-bearing — the shell's `-nt` registry-bypass gate compares it — so a cooldown *skip* must never refresh it. The module therefore never touches mtime on `read`; only `write` advances it (`Stamp.mtime()` exposes the value, e.g. for the Python-side mirror of the gate). The Python cooldown helpers (`_clear_project_cooldown` / `_restamp_project_cooldown`) route through `project_stamp` while preserving their exact prior behavior (same path, "skip doesn't restamp").

**bash/Python path-convention boundary.** The cooldown stamp is *also* read and written by `session-bootstrap.sh` (it runs before Python is available, so it can't call this module). bash and Python share the **path convention** — `cooldowns/<name>.<sha1-of-cwd>` — not a single function. `project_stamp()`'s layout must stay in lockstep with the shell's `_COOLDOWN_FILE` construction; there is intentionally no cross-boundary shared implementation, only a shared format. The bash `-nt` gate is left as-is.

**`engine_ran_version` and the harvest.** At the end of every completed pass the engine writes its running `version` to the global `engine_ran_version` stamp — the loop guard for the single-session update protocol (see [plugin-reload-lifecycle.md](plugin-reload-lifecycle.md#single-session-update-protocol-the-harvest)). The `UserPromptSubmit` harvest (`bootstrap_lib/harvest.py`) compares it against the installed bootstrap version and, when strictly behind, launches the new engine by its real `installPath` so a published bootstrap update converges in one session instead of two restarts.

**The stamp is monotonic.** The write is `max(stored, own_version)` by semver, not an unconditional overwrite. An OLDER engine completing a pass must never regress the stamp: under rapid restarts a resident old engine can win the single-instance lock while the newer harvest-launched one stands down, and a regressing write there re-opened the update as un-run *after* the harvest marker had already been consumed. An empty or unparseable stored value counts as `(0, 0, 0)`; equal versions still rewrite (idempotent).

**`last_version` and the self-transition line.** Step 2b compares the running engine's `version` against the global `last_version` stamp. Two rules keep that line honest:

- **Direction.** Only a genuine upgrade (`version > last_version` by semver) is an action entry, `updated: X -> Y`. The reverse direction -- an older binary running after a newer one, which is normal for a dev tree or an older resident session -- emits a **verbose-only ok** entry instead: `engine X ran (a newer Y ran previously -- dev tree or older resident session)`. Equal versions emit nothing. Reporting the reverse case as `updated: 0.62.0 -> 0.61.0` described a downgrade that never happened.
- **`--console` never writes it.** Console mode returns before the `engine_ran_version` stamp but used to reach the `last_version` write, so a dev-tree `--console` run left the two stamps inconsistent and manufactured a phantom transition on the next cache-engine pass. Console runs write no state; this is that contract.

**Log-section labels name the version they mean.** Bootstrap is the one plugin whose per-plugin log section carries the **registry** version (from `plugin_info.version`) while its other sections carry the **running binary's** version -- so one pass could emit two `bootstrap@X` headers with different `X` and no way to tell them apart. When the two differ, `_plugin_log_label` labels bootstrap's own section `bootstrap@<registry-version> (engine <running-version>)`. Other plugins are unaffected.

**The stale-restart notice stops once provisioning converges.** `_bootstrap_stale_advice` suppresses the "bootstrap was updated to X; restart to load it" notice when `engine_ran_version` is already `>=` the registry version: the new engine has completed a pass (via the harvest or an earlier restart), so a restart would only reload plugin *code*. The notice is kept while `engine_ran_version` is still behind.

**The harvest logs the version actually on disk.** `launch_new_engine` launches an `installPath`, not a version number. `read_path_version` reads that path's own `.claude-plugin/plugin.json`, and when it disagrees with the registry-claimed version the harvest's log line names both -- otherwise a mismatch (a half-written cache dir, a dev-tree repoint) is invisible and the status line reports a version that never ran.

## Design Principles

**Configuration-driven, not logic-driven.** The hook contains no platform-specific conditional branches for individual tools. It detects the OS once, reads the manifest entries for that OS, and executes what's declared. All platform knowledge lives in the manifests.

**Explicit per-OS entries.** Every tool dependency declares its check and install method for each platform it supports. No defaults, no inheritance. If `curl` is needed on all platforms, it appears three times.

**Collect independent failures.** System tool checking collects all independent failures rather than failing on the first one, so the user sees everything they need to fix. Consequential failures (e.g., a command that lives in a failed PATH directory) are detected and skipped.

**Two-tier venv management.** First checks if the existing venv is functional (Tier 1: directory exists, Python runs, packages importable) without needing uv. Only falls back to `uv sync` (Tier 2) if the venv is missing or broken. This removes the hard uv dependency for sessions where the venv is already good.

**Persistent storage.** The venv and cloned git repos live in `~/.claude/plugins/data/<plugin>/` (outside the plugin cache), so they survive cache refreshes when the plugin updates. The `sync_to_data` manifest operation copies plugin source files to the data directory at the same stable paths, so scripts can reference them via `os.path.expanduser()` without embedding versioned cache paths.

**Commit pinning for git_deps.** Git dependencies can optionally specify a `commit` SHA to pin to a specific version. After cloning, the engine checks out the pinned commit. On subsequent runs, it verifies HEAD matches the expected SHA. If mismatched, it fetches and checks out the correct commit.

**Every check must log its outcome.** The engine uses two entry lists: `action_entries` (always displayed) and `ok_entries` (displayed only in verbose mode). Every check — whether built-in (tools, venv, git deps) or custom (autodetect, bootstrap scripts) — must emit exactly one entry:

- **detect → ok (no change needed)** → append to `ok_entries` (silent unless verbose)
- **detect → remediate (created, installed, updated)** → append to `action_entries` (always logged)
- **detect → fail (unresolvable)** → append to `action_entries` + add to fix-all failures

This is the fundamental logging contract. A check that performs work (creates a file, clones a repo, writes config) without emitting an action entry is a bug — the user loses visibility into what bootstrap did. A check that passes silently without emitting an ok entry is also a bug — verbose mode becomes incomplete and debugging is harder.

Autodetect functions support this by returning a dict with `{"changed": bool, "actions": [...], "ok": [...]}` instead of a plain bool. The engine routes the messages to the appropriate entry list. Bootstrap scripts route to the same lists via `ctx.log(msg)` (action — always shown) and `ctx.log_ok(msg)` (ok — verbose only); a check that performs work calls `log`, a check that confirms steady state calls `log_ok`.

**Project config phase.** When a plugin declares a `project_config` section, the engine runs it before the `config` section: it discovers or reads the per-project file (`<cwd>/.claude/<name>.yaml`), runs the optional autodetect, applies declared defaults for any still-missing field (dict-form `required_fields` only — defaults never override populated values), and emits fix-all entries (`type: project_config`) for any remaining missing fields that lack a default. Final values are synced to the data-dir `config.yaml`. If autodetect returns `None` and no file exists, the engine sets `project_detected = False`, which gates downstream project-scoped primitives (e.g. `ini_settings`) and the `config` section's `required_fields` validation so non-project sessions produce no fix-all noise. Applied defaults always produce an action entry (`project config: applied defaults [...]`) — no silent file writes.

**Remediation, not auto-fix.** When something is missing, the hook emits structured JSON with the exact install command into Claude's `additionalContext`. The user can fix it themselves or tell Claude to do it.

## Plugin Cache Lifecycle

The bootstrap engine's marketplace and plugin remediation commands (`claude plugin marketplace update`, `claude plugin update`) interact with Claude Code's plugin cache system. Key behaviors (verified against Claude Code 2.1.74):

- **Version is the sole cache key.** Cache path is `cache/<marketplace>/<plugin>/<version>/`. Same version string = same cached files, even if the marketplace repo has newer commits.
- **Auto-update runs at every session start** when `autoUpdate: true` in `known_marketplaces.json`. There is no cooldown. It only runs at session start — not mid-session.
- **Version bump required for updates.** Auto-update compares the version string, not the git SHA. A version bump in `plugin.json` (and `marketplace.json`) is required for existing users to receive updates.
- **Fresh installs use HEAD.** A fresh install copies from the marketplace's current HEAD, cached under whatever version `plugin.json` declares. Between releases, this can diverge from what existing users have cached.

See [PLUGIN-BEHAVIOR-GUIDE.md](~/.claude/docs/guides/PLUGIN-BEHAVIOR-GUIDE.md) for full verified behaviors and evidence.

## Shared Library

Python library providing check-and-remediate primitives for common operations. These are the same primitives the engine calls when processing manifest entries — scripts can call them directly for custom workflows.

### Library Design Principles

Library boundaries follow Robert C. Martin's [package cohesion principles](https://en.wikipedia.org/wiki/Package_principles):

- **Common Reuse Principle (CRP)**: If you use one module in a library, you should plausibly use them all. Don't force a plugin to depend on code it doesn't need.
- **Common Closure Principle (CCP)**: Modules that change for the same reason belong together. A bug fix or feature change should affect one library, not scatter across several.
- **Acyclic Dependencies Principle (ADP)**: Libraries must not have circular dependencies. The dependency graph is a DAG.

## Cross-Plugin Shared Libraries (`shared_libs` / `shared_lib_imports`)

A manifest-phase capability (module `bootstrap_lib/shared_lib.py`, wired into `_process_manifest` after `pypi_packages` and before the script phase) that lets one plugin reuse another's first-party Python package **without a declared plugin dependency** — the reuse-by-availability posture. It shares first-party SOURCE only via a `.pth`; third-party deps remain each importing plugin's own `pyproject.toml` concern (a static test, `tests/bootstrap/test_dependency_completeness.py`, catches omissions). Schema + author-facing semantics live in [manifest-reference.md](manifest-reference.md#shared_libs--shared_lib_imports--cross-plugin-first-party-libraries); the engine behavior:

- **Owner (`shared_libs`)**: `sync_shared_lib()` content-hashes the package source at `<plugin_root>/<src>/<name>/` and, on change, clean-re-syncs it (remove-then-copy, pruning stale modules — unlike `sync_to_data`'s merge-only copy) to the stable `~/.claude/plugins/data/plugins-kit/_shared_libs/<name>/<name>/`. Then `link_shared_lib()` writes `<name>.pth` (pointing at `_shared_libs/<name>/`) into the standalone Python and verifies `import <name>`.
- **Consumer (`shared_lib_imports`)**: `link_shared_lib()` writes the same `.pth` into this plugin's own `<plugin_data_dir>/.venv` (the venv handler ran earlier in the same manifest pass, so it exists as the target).
- **Stable, not versioned**: the `.pth` targets the version-independent `_shared_libs/<name>/`, so an owner version bump re-syncs one directory and every `.pth` keeps resolving — no per-consumer rewrite needed.
- **Eventual consistency**: a consumer may be processed before its owner in a session; a not-yet-published library is a soft skip (logged, not a failure) that self-heals next session. The runtime `bootstrap_guard` covers the installed-but-not-yet-provisioned window.
- **Logging**: per the "every check logs its outcome" rule — `log_ok` on cached/skipped (verbose-only), action log on real sync/link, failure on a post-`.pth` import check that fails.

This is distinct from the "Shared Library" section above, which describes `bootstrap_lib` itself (the engine's own code package). `bootstrap_lib` is in fact migrated ONTO this capability — it declares itself a `shared_lib` so p4-kit / git-kit / unreal-kit import it via the `.pth` instead of hand-rolled discovery — while external consumers (update06) still use the git-dependency model.

## Script (Optional)

A Python module at a conventional location in the plugin's install path. Runs after manifest processing. The script:

- Can use the shared library (already on `sys.path` via the engine) or not
- Can read static config from its own directory
- Can read/write dynamic config from its data directory (e.g. `plugins/data/plugins-kit/unreal-kit/`)
- Returns a result indicating success, or outstanding issues requiring user intervention

Scripts are for logic that can't be expressed declaratively — domain-specific discovery, conditional branching, multi-step workflows that depend on intermediate results.

## Testing

All bootstrap modules have automated tests at the repo level in `tests/bootstrap/`. Tests use pytest and run via `uv run --extra dev pytest -v` from the repo root.

**Structure**: Library modules get unit tests with direct imports. The engine gets integration tests that invoke `bootstrap_engine.py` as a subprocess (matching how the bash wrapper calls it). Shared fixtures in `tests/conftest.py` provide temporary data directories, manifest builders, and path helpers.

**Why repo-level**: The bootstrap engine is cross-cutting infrastructure that will orchestrate multiple plugins. Tests need to span plugin boundaries (e.g. verifying engine+plugin manifest interactions), which doesn't fit inside any single plugin's directory.

**Standard**: Every new library module or engine capability must have corresponding tests before the milestone is considered complete. See [MILESTONES.md](../../../../docs/planning/bootstrap/MILESTONES.md) for per-milestone test deliverables.

## Case Studies

- [update01/bootstrap](../../../../docs/bootstrap/reference/case-studies/update01-bootstrap.md) — Marketplace sync and plugin cache refresh
- [unreal-kit](../../../../docs/bootstrap/reference/case-studies/unreal-kit.md) — Game development plugin with system tools, venv, config discovery, and external app dependencies
- [p4-kit](../../../../docs/bootstrap/reference/case-studies/p4-kit.md) — Code review plugin consuming bootstrap two ways: as a SessionStart manifest target and as a runtime library dependency (`bootstrap_lib.code_review`)
