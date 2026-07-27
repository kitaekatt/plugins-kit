# Plugin reload / restart lifecycle — when a change goes live

When you edit or update a plugin, *what* you changed decides whether the running
Claude session already has it, needs `/reload-plugins`, or needs a full restart.
This reference is the measured ground truth (don't trust folklore like "hooks
always need a restart" — it's wrong as a blanket rule). Load it when reasoning
about the reload/restart notice (engine `_reload_advice`, Step 4d) or when telling
a user what to do after a publish.

## The three layers

A plugin contributes three kinds of thing, and they go live differently:

1. **Code / script content** — the bytes a registered command runs: the bootstrap
   engine, a hook's `.sh`, a skill's `.py`. A hook is registered as a *command*
   (e.g. `bash ${CLAUDE_PLUGIN_ROOT}/hooks/.../foo.sh`); the script behind that
   command is read **fresh from disk on every invocation**. Editing it is live on
   the next run — **no `/reload-plugins`, no restart** — as long as the registered
   command and its resolved path are unchanged.

2. **Registration** — the `hooks.json` event→command map, which skills/commands
   exist, the description triggers. Claude Code loads this when it loads the
   plugin. **`/reload-plugins` re-reads it in-session** — a changed hook command
   went live mid-session with no restart.

3. **Firing / lifecycle** — a reloaded registration only matters when the hook
   next *fires*. Event hooks (`UserPromptSubmit`, `PreToolUse`, …) fire on their
   next event, so they're live right after `/reload-plugins`. **`SessionStart`
   only fires when a session starts** — `/reload-plugins` reloads its registration
   but does **not** re-fire it. So a SessionStart-driven plugin only (re)runs on a
   **new session** (restart).

## Measured findings

Probed 2026-05-31 on Claude Code (CLI) via a disposable `--plugin-dir` plugin
whose `UserPromptSubmit` hook logged its compiled `version` and its launch `reg`
arg to a file, across edit → `/reload-plugins` → restart:

| Change | Observed | Conclusion |
|---|---|---|
| Hook **script** edited (`version` 1→2) | next prompt logged `version=2` (fresh pid each time) | script content is read live; no reload/restart |
| `hooks.json` **command** changed (added `reg=REGB`), **no** reload | `reg=none` (old registration still active) | registration changes are not auto-live |
| …then `/reload-plugins` | `reg=REGB`, **same session**, no restart | `/reload-plugins` reloads the registration in-session |
| restart | (baseline — every session loads all registrations + fires SessionStart) | restart carries everything |

Probed 2026-07-20 (macOS, synthetic SessionStart hooks in a scratch project +
timed `claude -p` against a no-hook baseline): **Claude Code blocks session
readiness on the SessionStart hook's completion**, and completion means
*process exit AND stdout-pipe EOF* — a background child that merely inherits
the hook's stdout (`sleep 6 &`) delayed the session exactly like foreground
`sleep 6`, while a child with stdin/stdout/stderr redirected
(`(sleep 20 >/dev/null 2>&1 &)`) added zero. This is why `session-bootstrap.sh`
keeps only "read stdin + gates + emit JSON" on the foreground path (~tens of
ms) and runs all provisioning — Python ensure/download, Windows registry PATH
writes, engine launch — inside a `_provision` subshell dispatched
`</dev/null >/dev/null 2>&1 &` (contract pinned by
`tests/bootstrap/test_sessionstart_detach.py`). The historical "can't type for
5–10 s at startup" complaint was foreground provisioning work in this hook;
user-facing output is unaffected because it flows through
`bootstrap_display.pending` → the UserPromptSubmit display hook, never through
SessionStart stdout (which VS Code doesn't show anyway).

## Documented behavior (official docs)

The probe above matches Claude Code's documented contract (https://code.claude.com/docs/en/discover-plugins, .../plugins-reference):

- **autoUpdate caches at session start, then notifies.** When autoUpdate is on and a new version exists, Claude Code refreshes the marketplace and updates the cache **at the start of the next session**, then shows a *"run `/reload-plugins`"* notification. The session that triggered the update is still running the old version until you reload.
- **`/reload-plugins` switches to the new version in-session.** *"When a plugin updates mid-session, hook commands, monitors, MCP servers, and LSP servers keep using the previous version's path. Run `/reload-plugins` to switch hooks, MCP servers, and LSP servers to the new path; **monitors require a session restart**."* So `${CLAUDE_PLUGIN_ROOT}` re-resolves to the new cache version dir for hooks/MCP/LSP on reload — confirming our probe — but **a plugin monitor needs a full restart**.
- **`/reload-plugins` reloads from the cache, not the marketplace.** It does not re-fetch; getting a new version into the cache is autoUpdate's (or `claude plugin update`'s) job. Installing/enabling/disabling a plugin mid-session is also picked up by `/reload-plugins`.
- **Version is the cache key**; cache lands at `~/.claude/plugins/cache/<mkt>/<plugin>/<version>/`, recorded in `installed_plugins.json`.

Caveat: docs describe the contract; a specific Claude Code build may differ. Our probe (above) independently confirmed the in-session registration/path switch on this machine.

## Practical rule

- **Dev loop** (`--plugin-dir`, editing files in place): edits to engine/skill/hook
  **code** are live on next use. Changes to **registration** (a new/edited
  `hooks.json`, a new command/skill) need **`/reload-plugins`**. A **restart** is
  only required to **re-fire a `SessionStart` hook** (e.g. to re-run bootstrap's
  provisioning pass).

- **Real plugin update** (a published version bump pulled via `/plugin update` or
  autoUpdate): the cache **version directory changes**, so the command paths the
  registration resolves to (`${CLAUDE_PLUGIN_ROOT}`) move. The reliable action is
  **restart Claude (or restart your IDE)** — it re-resolves install paths, reloads
  every registration, and re-fires SessionStart so bootstrap re-provisions for the
  new version. (Both `session-bootstrap.sh` guards — the Layer-1 session-id guard
  and the Layer-2 per-project cooldown — bypass when a registry file
  [`installed_plugins.json` / `known_marketplaces.json`] is newer than their
  stamp, so the pass actually runs instead of being skipped. This is what lets a
  `claude --resume` — which re-presents the *original* session_id — re-provision
  after an update instead of being guard-skipped; without the Layer-1 bypass a
  resumed session skipped bootstrap entirely until an unrelated fresh session.)
  Most marketplace plugins here carry a SessionStart hook (bootstrap, and anything
  depending on it), so a restart is the simple, correct default after a real update.

## How the notice (Step 4d) uses this

`_reload_advice` fires for any plugin that **entered the registry during the pass**
— detected by a registry before/after diff (`_resolve_newly_installed`), so it
covers a layered `plugins:` install, a per-plugin install, and a script install
alike (an earlier version keyed on Step 4b's `new_plugins` and missed layered
installs — caught by the cache-kit end-to-end test). It branches on whether that
plugin registers a **SessionStart** hook (`_plugin_ships_sessionstart_hook`):

- **SessionStart hook present** → note it loads on the next **restart** (only a
  fresh session re-fires it).
- **otherwise** (only skills/commands/event-hooks) → note **`/reload-plugins`**
  activates it in-session.

**These are informational notices, not action-required items.** They ride in the
normal display output (label `<mkt>:bootstrap@<v> notice`) and are visible in both
`systemMessage` and `additionalContext`, but `emit_success_response` deliberately
adds **no relay directive** telling the session's Claude to surface them: an
"ACTION REQUIRED — surface this now" preamble (removed 2026-07-16) made Claude
present a routine update notice as urgent, when whether and *when* to restart is
the user's call. The same applies to the bootstrap self-staleness notice
(`_bootstrap_stale_advice`).

## Single-session update protocol (the harvest)

The one limitation the nag above can't *fix*, only *report*: a SessionStart hook
re-fires only on a fresh session. So when autoUpdate fetches a newer **bootstrap**
mid-session — the new code lands in the cache and `installed_plugins.json` is
repointed — the SessionStart hook already ran the **old** engine, and the new
engine never executes until a restart. Historically this meant a published
bootstrap update needed **two** session restarts (run #1 runs the old engine; the
fetch lands afterward; run #2 finally runs the new engine).

The **harvest** closes that to one session by exploiting the asymmetry in the
layers above: `UserPromptSubmit` *does* re-fire within a session. So bootstrap's
`UserPromptSubmit` hook harvests the already-fetched new engine on the next prompt.

**Mechanism** (`bootstrap_lib/harvest.py`, driven by
`hooks/userpromptsubmit/bootstrap-display.sh`):

1. **Engine stamps its version on completion.** At the end of every completed
   `engine._main` pass, the engine writes its own running `version` to a global
   stamp `engine_ran_version` ("the bootstrap engine version that last actually
   executed a pass"). A crash skips it (not a completed pass); `--console` debug
   runs return earlier and never stamp. The write is **monotonic** -- `max(stored,
   own)` by semver -- so an older engine that wins the single-instance lock and
   completes a pass can never regress the stamp and re-open a landed update as
   un-run.
2. **Harvest reads two values per prompt.** On each `UserPromptSubmit`, the helper
   reads the installed bootstrap `version` + `installPath` from
   `installed_plugins.json` (entry key `bootstrap@<marketplace>`) and the
   `engine_ran_version` stamp. Common (no-update) cost is two reads + a semver
   compare — near zero.
3. **If installed > ran**, the new code is on disk but its engine never ran this
   session → launch the **new** engine **by its real `installPath`** (NOT
   `${CLAUDE_PLUGIN_ROOT}`, which is bound to the *old* version dir this session),
   detached, in the same background/pending-file output mode SessionStart uses
   (output surfaces via `bootstrap_display.pending` on the following prompt). The
   launch clears the per-project cooldown first so the forced pass isn't
   throttled, and invokes the new `session-bootstrap.sh` with empty stdin (its
   session-id guard is then inert).
4. **Loop guard.** The harvested engine stamps `engine_ran_version = <its own
   version>` on completion, so `installed == ran` afterward and it can never
   re-trigger itself. A per-installed-version marker (`harvest_launched_version`)
   additionally caps relaunches at one while the engine converges (guards against
   several quick prompts spawning concurrent passes).
5. **Arbitration under contention.** The harvest-launched engine still has to win
   the single-instance lock, and `session-bootstrap.sh`'s provisioning step runs
   *before* the lock -- so it can arrive seconds after a resident OLD engine took
   it. An engine that carries the update retries acquisition for ~10s before
   yielding; if it still has to stand down, it **clears
   `harvest_launched_version`** so the next prompt's harvest retries rather than
   staying disarmed forever. See engine-internals.md, "Single-instance lock".

**The one inherent caveat (documented, NOT solved):** the *running* bootstrap must
already contain the harvest hook to harvest a newer version. The version that
**first** ships this protocol therefore cannot harvest itself — that single
transition still needs the old two-restart path. Single-session convergence
applies to every update **after** this protocol ships. (There is no way around
this: a session is already running the pre-harvest `UserPromptSubmit` hook code,
which has no harvest logic to run.)

## SessionStart-missed rescue (fresh-machine first session)

The harvest handles "SessionStart ran the *old* engine"; the rescue handles
"SessionStart never ran **at all**". Observed live (2026-07-16): on a fresh
machine (or after deleting `~/.claude/plugins/`), Claude Code is still seeding
the marketplace from `extraKnownMarketplaces` and populating the cache when
SessionStart fires — bootstrap's SessionStart hook isn't registered yet, so no
provisioning pass runs that session, even though `/plugin` shows everything
installed and skills/UserPromptSubmit hooks load fine moments later.

**Mechanism** (pure bash in `bootstrap-display.sh` — on a fresh machine Python
does not exist yet; `session-bootstrap.sh` is what installs it):

1. `session-bootstrap.sh` touches a per-session marker `sessions/<session_id>`
   at **entry** — before its gates, so even a gate-skipped invocation records
   "a pass was invoked for this session" within milliseconds of firing.
   (Deliberately NOT the Layer-1 `last_session_id` stamp: that is a single
   global slot — a second concurrent session overwrites it, which would
   ping-pong rescues between sessions forever — and `bootstrap-reset-cooldown`
   deletes it, which must re-arm the *next* SessionStart, not fire a
   mid-session pass.)
2. The `UserPromptSubmit` display hook extracts `session_id` from its stdin hook
   JSON (byte-identical extraction pipeline in both scripts, pinned by a drift
   test). A missing marker for this session means no SessionStart pass was
   invoked → arm the rescue.
3. The detached subshell (the prompt is never delayed) sleeps
   `BOOTSTRAP_RESCUE_DELAY` (default 2s) and **stands down** if (a) the marker
   appeared — a genuinely-firing SessionStart pass claimed the session
   (fast-start / `claude -p` race) — or (b) **any** per-project cooldown stamp is
   fresh (<120s) — a pass stamped it at entry moments ago, covering a
   SessionStart pass that received no stdin and so wrote no marker. Both
   stand-down paths run the **harvest** this prompt's foreground skipped, so a
   single-prompt session still converges a pending update.
4. Otherwise it takes an atomic **one-launch-per-session lock** (noclobber
   create of `sessions/rescue_launched.<session_id>`) — at most one rescue
   launch per session, ever — appends a `sessionstart-rescue:` audit line to
   `bootstrap.log`, and launches `session-bootstrap.sh` **with the hook JSON
   piped in**, so the pass writes this session's marker and its normal gates
   (session guard, per-project cooldown, registry bypass) apply unchanged.
5. The foreground harvest is skipped on a prompt where the rescue armed (the
   subshell either launches the full pass or runs the harvest itself — never
   two engine launches from one prompt). Real passes prune week-old markers.

## Mid-session install relaunch (plugin installed without a restart)

The harvest handles "a newer **bootstrap** landed mid-session"; the rescue
handles "SessionStart never fired"; this third trigger handles "**some other
plugin** was installed/uninstalled/updated mid-session". Observed live
(hue-kit, 2026-07): `/plugin install` + `/reload-plugins` loads the new
plugin's skills, but its **venv is never provisioned** — SessionStart already
ran and won't re-fire — so every command of the new plugin fails until a full
restart. (`/reload-plugins` reloads registration only; it fires no hook.)

**Mechanism** (`harvest.run_registry_relaunch` + `bootstrap_lib/plugins_snapshot.py`,
same `UserPromptSubmit` driver; runs only on prompts where neither harvest
trigger fired, so a prompt launches at most one pass):

1. **The engine absorbs a plugin-set snapshot at pass completion** — a global
   stamp `plugins_state_hash`: a content hash of `installed_plugins.json`'s
   `plugins` map **plus** `settings.json`'s `enabledPlugins`. Both signals are
   needed (verified empirically 2026-07-20 by uninstall/reinstall under a live
   session): a populated registry v2 rewrites `installed_plugins.json` on
   install, but the registry-v2-**empty** variant only writes `enabledPlugins`.
   Content-hash, not mtime — `settings.json`'s mtime churns for unrelated
   reasons (statusline rewrite, model change). Stamping at *completion* is what
   keeps bootstrap-authored registry writes during a pass from self-triggering,
   and it seeds the stamp on the first pass after this version ships (an
   unseeded stamp never triggers).
2. **Each prompt compares live state against the stamp.** Unchanged → nothing
   (one stamp read + two small JSON reads). Changed → write a once-per-change
   dedup marker (`plugins_relaunch_hash`, cleared by the engine on completion),
   then relaunch `session-bootstrap.sh` — the newest on-disk engine per the
   registry/cache, falling back to the running plugin root — detached, empty
   stdin, cooldown cleared first (same launch discipline as the harvest). The
   forced pass provisions the new plugin's venv/config in-session.
3. **Net user experience:** install a plugin, run `/reload-plugins` as the
   install flow suggests, and by the prompt after next its commands work — no
   restart. A pass takes a few seconds detached; a command fired instantly
   after install may still race it (retry, don't restart). Uninstalls and
   enable/disable flips also re-arm a pass; its checks simply pass clean.

## Advising on a bootstrap update (read the state, tell the user, spot anomalies)

After a bootstrap version is published, this is how to read a consumer machine's
state and tell the user exactly what (if anything) they must do — and how to
recognize when something went wrong instead of asserting success.

### State to read

Under `~/.claude/plugins/data/<marketplace>/bootstrap/` unless noted:

- `~/.claude/plugins/installed_plugins.json` → `plugins["bootstrap@<mkt>"]` `version`
  + `installPath`: the **installed/activated** version Claude Code loads next session.
  **Registry v2 caveat:** newer Claude Code keeps this file at `{"plugins": {}}` for
  marketplace installs; the installed version is then the highest version dir under
  `~/.claude/plugins/cache/<mkt>/bootstrap/` (the harvest and the engine's plugin
  discovery both fall back to that cache scan since 0.47.0).
- `engine_ran_version` → the bootstrap version whose engine **last completed a pass**.
- `harvest_launched_version` → the version the harvest last launched (dedup marker).
- `last_session_id` → the Layer-1 session-id guard stamp.
- `sessions/<session_id>` → entry-time per-session markers (the
  SessionStart-missed rescue's detection signal) plus
  `sessions/rescue_launched.<session_id>` one-launch locks; pruned after 7 days.
- `cooldowns/last_run_epoch.<sha1-of-cwd>` → the Layer-2 per-project cooldown stamp.
- `bootstrap.log` → per-run headers `--- bootstrap@<version> <ISO-ts> ---` and harvest
  audit lines (`--- bootstrap harvest … --- / harvest: launched bootstrap <v> engine`).
- The SessionStart/UserPromptSubmit display label (`<mkt>:bootstrap@<version> -> …`)
  names the **running** engine version for that output.

### Healthy convergence flow

1. **Publish** (version bump + master). Nothing happens on consumers until a session start.
2. **Next session start:** Claude Code's autoUpdate **and/or** bootstrap's own pass
   (`marketplaces.alwaysUpdate` refreshes the clone; the `plugins:[bootstrap]` entry
   runs `claude plugin update`) fetch the new version → `installed_plugins.json`
   repoints, new code lands in the cache. But the SessionStart hook already ran the
   **old** engine (its command path was bound to the old `installPath` at session load).
3. **Convergence to the new engine** happens one of two ways:
   - **Harvest (automatic, same session):** the next `UserPromptSubmit` sees
     `installed > engine_ran_version` and launches the new engine in-session; when it
     completes, `engine_ran_version == installed`. **No second restart needed for
     provisioning.** Requires the *running* version to carry a **working** harvest
     (≥ 0.25.0 — the harvest first shipped in 0.22.0 but silently no-op'd until the
     0.25.0 script-path fix; the guard bypass it relies on shipped in 0.24.0).
   - **Next restart:** SessionStart re-resolves to the new `installPath` and runs the
     new engine directly.

### What the user must actually do

- **Provisioning** (tools, venvs, shared-libs, config) is complete the moment
  `engine_ran_version == installed` — whether the harvest or a restart got it there.
  No further action for provisioning.
- **A restart is required only to load new plugin *code*** (new hook/skill/command
  bytes) into the running session — Claude Code binds those at session load. For an
  engine-only / provisioning change, the harvest already did the work and a restart
  is **optional**.
- **The "it will load next time you restart" notice is emitted by the *old* engine**
  *before* the harvest runs. If `engine_ran_version` has since reached the installed
  version, provisioning is done and the notice is **moot** — a restart then only
  refreshes loaded code. The engine suppresses the notice itself once
  `engine_ran_version >= ` the registry version, so a converged update stops
  nagging from later old-binary sessions; if you still see it, `engine_ran_version`
  is genuinely behind. Say so rather than parroting "you must restart." (The notice
  is informational by design — no relay directive; the user decides when to restart.)
- **`claude --resume` works after an update:** both guards bypass on a newer registry
  file, so the resumed pass runs (it does **not** silently skip), and the harvest
  converges it. **No manual cooldown clear is needed in the normal case.**
- **Manual override:** `bash plugins/bootstrap/scripts/bootstrap-reset-cooldown.sh`
  clears **both** the per-project cooldown **and** the session-id guard
  (`--status` reports them without writing; `--all` covers every project). Needed
  only when adopting a fix that can't deploy itself (the harvest's inherent caveat —
  a bootstrap-mechanism fix can't use that same mechanism to adopt itself) or when
  bootstrap genuinely appears stuck.

### Anomalies — stop, investigate, surface to the user

These mean the normal flow did **not** happen. Raise them; do not report success:

- **`engine_ran_version` stays *behind* `installed`** after a restart **and** a prompt
  or two → the new engine isn't running. Check `bootstrap.log` for a `bootstrap@<new>`
  run and a `bootstrap harvest` line; check `harvest_launched_version`. A silent no-op
  here is a real bug — this is exactly how the 0.25.0 harvest-script-path bug presented
  (the harvest fired on every prompt but threw on import and was swallowed). Also read
  the `bootstrap lock` stand-down lines: they name the standing-down engine's own
  version and the lock holder's PID, which is how you tell "lost the race repeatedly"
  apart from "never launched."
- **The hook label keeps showing the *old* version** after the update should have
  landed → the new engine hasn't executed. On `--resume`, suspect a guard stamp newer
  than the registry write (so the bypass didn't fire). A `bootstrap@<registry-version>
  (engine <running-version>)` log header is **not** this anomaly -- it is the label
  deliberately naming both versions when a pass's registry and binary versions differ.
- **`installed` itself never advances** after a publish → the *fetch* didn't happen
  (autoUpdate off? marketplace clone stale? offline?). Check the clone HEAD vs
  `origin/master` and `known_marketplaces.json` `autoUpdate`.
- **`harvest_launched_version == installed` but no `bootstrap@<installed>` run in the
  log** → the harvest launched but the engine pass itself failed; read the log/stderr.

## Open / untested (don't over-claim)

- **New-plugin registration via `/reload-plugins`.** We measured a *changed command
  on an existing binding*, not a *brand-new* plugin's first registration. The nag
  conservatively prefers restart when a new plugin ships a SessionStart hook.
- **Real cache-version update via `/reload-plugins`.** `--plugin-dir` doesn't
  version, so whether `/reload-plugins` re-resolves a new cache version dir is
  unverified. Restart is the safe default for real updates (above).
- **IDE behavior.** Measured in the CLI. Claude in an IDE may need the **IDE**
  restarted, not just the Claude session — so the nag always offers the IDE-restart
  option alongside.

To re-measure: re-create the probe (a `--plugin-dir` plugin with a re-triggerable
hook that logs a version+arg marker to a file) and walk edit → `/reload-plugins`
→ restart, reading the log between steps.
