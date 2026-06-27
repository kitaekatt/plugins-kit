# Plugin reload / restart lifecycle — when a change goes live

When you edit or update a plugin, *what* you changed decides whether the running
Claude session already has it, needs `/reload-plugins`, or needs a full restart.
This reference is the measured ground truth (don't trust folklore like "hooks
always need a restart" — it's wrong as a blanket rule). Load it when reasoning
about the reload/restart nag (engine `_reload_advice`, Step 4d) or when telling a
user what to do after a publish.

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
  new version. (With the cooldown registry-change bypass, that one restart's
  bootstrap pass actually runs instead of being throttled.) Most marketplace
  plugins here carry a SessionStart hook (bootstrap, and anything depending on it),
  so a restart is the simple, correct default after a real update.

## How the nag (Step 4d) uses this

`_reload_advice` fires for any plugin that **entered the registry during the pass**
— detected by a registry before/after diff (`_resolve_newly_installed`), so it
covers a layered `plugins:` install, a per-plugin install, and a script install
alike (an earlier version keyed on Step 4b's `new_plugins` and missed layered
installs — caught by the cache-kit end-to-end test). It branches on whether that
plugin registers a **SessionStart** hook (`_plugin_ships_sessionstart_hook`):

- **SessionStart hook present** → advise **restart** (only a fresh session re-fires it).
- **otherwise** (only skills/commands/event-hooks) → advise **`/reload-plugins`**.

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
   runs return earlier and never stamp.
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

**The one inherent caveat (documented, NOT solved):** the *running* bootstrap must
already contain the harvest hook to harvest a newer version. The version that
**first** ships this protocol therefore cannot harvest itself — that single
transition still needs the old two-restart path. Single-session convergence
applies to every update **after** this protocol ships. (There is no way around
this: a session is already running the pre-harvest `UserPromptSubmit` hook code,
which has no harvest logic to run.)

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
