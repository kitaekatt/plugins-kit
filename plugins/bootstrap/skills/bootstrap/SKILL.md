---
_schema_version: 1
name: bootstrap
author: christina
skill-type: reference-skill
description: Use when interpreting SessionStart bootstrap messages or configuring user/project dependency manifests. Do NOT use for non-bootstrap debugging.
---

# Bootstrap

Reference for the bootstrap engine's behavior, message types, configuration files, and remediable conditions. The contract data below is the load-bearing surface; deeper detail lives in the references list.

```yaml
reference_skill:
  _schema_version: "1"
  identity: Reference for the bootstrap engine's behavior, message types, configuration files, and remediable conditions.
  scope:
    covers:
      - SessionStart bootstrap message interpretation
      - bootstrap.json schema and merge semantics
      - env.json personalization schema, machine registry, and the env gate
      - remediable condition categories
      - configuration-file layering
      - the auto-remediate / fix-all flow
    excludes:
      - non-bootstrap plugin debugging
      - plugin authoring beyond bootstrap config
  facts:
    - id: message_outcomes
      summary: Bootstrap produces four message outcomes on session start.
      keywords: [silent pass, silent install, silent skip, fix-all, healthy state, no output, message types, outcomes, session start outcomes]
      detail: |
        | Outcome        | What happened                                | User sees                       |
        |----------------|----------------------------------------------|---------------------------------|
        | silent pass    | All checks passed or cache hit               | Nothing                         |
        | silent install | Tool missing, installed, re-check passed     | Nothing (logged internally)    |
        | silent skip    | First session on fresh machine               | Nothing (engine runs next)      |
        | fix-all        | User action required                         | Remediation message + prompt    |
      gotchas:
        - Healthy bootstrap is invisible -- no output means everything checked clean, not that bootstrap is broken. Verify by checking each plugin's log at ~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log. If the log doesn't exist, bootstrap never reached that plugin.
    - id: update_lifecycle
      summary: A published bootstrap update converges in ONE session via the harvest; provisioning is done when engine_ran_version == the installed version, and a restart is needed only to load new plugin CODE. Both session-bootstrap.sh skip gates bypass on a registry change, so `claude --resume` re-runs after an update. A SessionStart that never fired at all (fresh machine mid-sync) is caught by the UserPromptSubmit rescue.
      keywords: [update, harvest, single-session, engine_ran_version, installed_plugins.json, claude --resume, restart needed, cooldown status, session-id guard, two skip gates, anomaly, provisioning done, advise user, rescue, sessionstart missed, fresh machine, notice, mid-session install, plugin install no restart, venv not provisioned, reload-plugins, plugins_state_hash, registry relaunch]
      detail: |
        How to read an update and advise the user:
        - installed version = installed_plugins.json plugins["bootstrap@<mkt>"].version (what loads next
          session). Registry v2 keeps that file EMPTY for marketplace installs; the installed version is then
          the highest version dir under ~/.claude/plugins/cache/<mkt>/bootstrap/ (engine discovery and the
          harvest both fall back to that cache scan since 0.47.0).
        - engine_ran_version (stamp in the bootstrap data dir) = the version that last COMPLETED a pass.
        - Convergence: on the next session start the new version is fetched; the SessionStart hook ran the
          OLD engine, then the harvest (UserPromptSubmit) launches the NEW engine in-session when
          installed > engine_ran_version. Provisioning is DONE once engine_ran_version == installed.
        - Restart: needed ONLY to load new plugin CODE (hooks/skills); NOT for provisioning. The "it will
          load next time you restart" notice is from the OLD engine pre-harvest and is moot once
          engine_ran_version caught up. Reload/restart advisories are informational NOTICES (no relay
          directive; the user decides when to restart) -- never present them as urgent actions.
        - claude --resume works: both skip gates (Layer-1 session-id guard, Layer-2 cooldown) bypass when a
          registry file is newer than their stamp; bootstrap-reset-cooldown clears both.
        - SessionStart never fired at all (fresh machine: Claude Code was still syncing the marketplace when
          SessionStart fired, so the hook wasn't registered): the UserPromptSubmit rescue detects that no
          sessions/<session_id> entry-time marker exists for the prompt's session and launches
          session-bootstrap.sh itself -- detached, one launch per session (atomic lock), gates intact.
        - A plugin installed/uninstalled MID-SESSION (`/plugin` + /reload-plugins) is provisioned WITHOUT a
          restart: the UserPromptSubmit mid-session install relaunch compares a content hash of the registry
          plugins map + settings.json enabledPlugins (plugins_state_hash, absorbed by the engine at pass
          completion) and relaunches session-bootstrap.sh once per change. Commands of the just-installed
          plugin work a prompt or two later; a command fired instantly after install may race the detached
          pass -- retry, don't restart.
        Full operational guide (state files, healthy flow, anomaly checklist): references/plugin-reload-lifecycle.md.
      gotchas:
        - ANOMALY -- engine_ran_version staying BEHIND installed after a restart + a couple prompts means the
          new engine isn't running; surface it for investigation (check bootstrap.log for a bootstrap@<new>
          run header and a "harvest" line). Do NOT report success; a silent harvest no-op is how a real bug presented.
        - A bootstrap-mechanism fix cannot adopt itself via that mechanism (the first working-harvest version
          can't harvest itself); that one transition needs bootstrap-reset-cooldown + an extra restart.
        - Where the broken mechanism is DELIVERY itself (update, harvest, registry record selection, install
          scope), no bootstrap release can reach an affected machine -- the fix arrives only via the path that
          is broken there, so publishing it converges nobody and strands every later bootstrap fix behind the
          same wedge. Ask "would this fix have to be installed by the thing it repairs?"; if yes it belongs in
          the separate dependency-free bootstrap-stuck-fix plugin (no prior version to be wedged on, runs
          current code on its first session). Still fix the root cause in bootstrap for machines not yet stuck
          -- just do not mistake that for remediating the ones that are.
    - id: manual_convergence
      summary: >-
        After a plugin download or version change, run bootstrap MANUALLY to converge
        provisioning -- a restart is an optimization taken when convenient, never the
        remediation.
      keywords: [do I need to restart, restart required, restart not required, converge by hand, manual bootstrap run, bootstrap-reset-cooldown, session-bootstrap.sh, run bootstrap manually, after plugin update, after plugin install, no restart needed, restart vs manual run, three layers, code loading vs provisioning]
      detail: |
        Three separable layers, only the last of which a restart actually serves (see
        update_lifecycle for the state-file mechanics behind each):
        1. Download/activation (new plugin files onto disk) -- `claude plugin
           marketplace update <mkt>` + `claude plugin update`. No restart.
        2. Provisioning (bootstrap applying the manifest -- ini writes, venvs, PATH,
           config merges) -- run `scripts/bootstrap-reset-cooldown.sh` then invoke
           `hooks/sessionstart/session-bootstrap.sh` directly. No restart.
        3. Code loading (new hooks/skills REGISTERING in the current session) -- the
           only residue a manual run cannot converge; this is what /reload-plugins or a
           restart is for.
        Converging layers 1-2 by hand means the next restart -- whenever it happens
        naturally, for unrelated reasons -- starts from an already-converged machine:
        a clean session, not a remediation session. Restarts stop being a step in a
        procedure.
        Corollary: where the needed thing IS in layer 3, specialized duplicate code is
        acceptable -- a skill may invoke the underlying script BY PATH rather than
        waiting for its own registration. Slightly redundant, and it turns a required
        restart into a no-op.
      gotchas:
        - Distinguish "restart to load new code" (layer 3, legitimate) from "restart so
          the default workflow will remediate something" (layers 1-2, wrong -- run
          bootstrap manually instead).
        - Advising a restart as the fix for an unconverged manifest is the anti-pattern
          this fact exists to block; the advisory is only ever a notice about layer 3,
          never a remediation step for layers 1-2.
    - id: remediation_phases
      summary: The engine remediates silently first; only escalates to fix-all when user action is required.
      keywords: [auto-remediation, fix-all, two-phase, silent install, remediation flow, autodetect, default values]
      detail: |
        Phase 1 (silent): tool installs (run install command, re-check); config autodetect (plugin manifest's "autodetect" script discovers and fills required fields, e.g. scanning CWD for a .uproject); default values from manifest "default" fields.
        Phase 2 (fix-all): aggregates remaining failures into a single fix-all message. additionalContext (seen by the agent) carries numbered remediation steps; systemMessage (seen by the user) carries the bootstrap log of what was checked and what failed. User types "fix-all" to run the auto-remediation; the failing checks re-run every session until clean, so no separate confirmation is needed after manual fixes.
      example:
        input: A plugin declares uproject and engine_dir as required fields, plus an autodetect script that scans CWD.
        output: Engine copies the default config (empty values), calls autodetect, fills both fields, validates required-field presence, no fix-all needed. If autodetect only finds uproject, engine_dir becomes a fix-all item.
      gotchas:
        - Autodetect runs before required-field validation. A plugin's autodetect script can fill required fields silently; if autodetect partially succeeds, the remaining fields surface as fix-all items.
        - fix-all re-runs the engine after remediation. If issues persist after fix-all, the cause is likely outside the engine's known remediation paths.
    - id: condition_categories
      summary: Eleven categories of remediable condition the engine knows how to address.
      keywords: [tool, PATH, venv, npm, node, node_modules, package.json, git dependency, JSON config, INI settings, PyPI package, marketplace, plugin, user config, condition categories, remediation]
      detail: |
        | Category       | Examples                              | Remediation                              |
        |----------------|---------------------------------------|------------------------------------------|
        | Tool           | uv, git, gh CLI not installed         | Platform-specific install + re-check     |
        | PATH           | ~/.local/bin not in PATH              | Modify persistent PATH config            |
        | Venv           | Python venv missing or broken         | uv sync from pyproject.toml              |
        | Node modules   | Project node_modules missing or stale | npm ci (or npm install) from package.json; no fallback on npm ci out-of-sync refusal |
        | Git dependency | Repo not cloned, wrong branch/commit  | clone once; pinned commits re-checkout; no steady-state pull |
        | JSON config    | File lacks expected entries           | Merge missing entries into target JSON   |
        | INI settings   | Application config setting not set    | Write setting to config/ini file         |
        | PyPI package   | Extracted file missing locally        | Download from PyPI and extract           |
        | Marketplace    | Not registered, stale, or pinned at wrong commit | claude plugin marketplace add/update; pinned: checkout pin SHA |
        | Plugin         | Not installed, out of date, wrong scope | Install / update / reinstall            |
        | User config    | API keys, paths missing               | Ask user via fix-all flow                |
    - id: config_layers
      summary: bootstrap.json supports a 4-layer override hierarchy.
      keywords: [bootstrap.json, layered config, project local, user level, override, merge semantics, priority, gitignored]
      detail: |
        Priority 4 (highest) -> <project>/.claude/bootstrap.local.json (gitignored)
        Priority 3 -> <project>/.claude/bootstrap.json (committed)
        Priority 2 -> ~/.claude/bootstrap.local.json (per-machine)
        Priority 1 (lowest) -> ~/.claude/bootstrap.json (per-user)
      example:
        input: User has uv globally and node per-project.
        output: User-level declares {tools:[uv]}; project-level declares {tools:[node]}; final merged set is both. Same identity in multiple layers means higher-priority fields win.
      gotchas:
        - bootstrap.local.json files are gitignored; per-machine overrides do not propagate to teammates.
        - Layer order matters. Higher-priority layers win on conflict; arrays union by identity key, objects deep-merge, scalars override. A user-level entry can be silently shadowed by a project-level entry with the same identity.
    - id: marketplace_pinning
      summary: A marketplace entry's pin field freezes the ENTIRE marketplace repo at a git committish -- one pin holds every plugin, shared lib, and dependency edge at a tested snapshot until the pin is dropped.
      keywords: [pin, version pin, pin plugins, pin marketplace, freeze versions, stop updates, snapshot, unpin, drop the pin, re-pin, autoUpdate, marketplace_pins.json, detached HEAD, known-good versions]
      detail: |
        Declare in a layered manifest (recommended: ~/.claude/bootstrap.json, or
        bootstrap.local.json for per-machine):

          { "marketplaces": [ { "name": "plugins-kit", "pin": "f7f6276a" } ] }

        While pinned: the clone at ~/.claude/plugins/marketplaces/<name> is checked out
        (detached) at the pin, autoUpdate is forced false in known_marketplaces.json (prior
        value recorded in ~/.claude/plugins/data/plugins-kit/bootstrap/marketplace_pins.json),
        and stale-update paths are skipped. Because `claude plugin update` and Claude Code's
        auto-updater both read versions from that clone, the one pin freezes every plugin --
        shared-lib owners and consumers can never skew (the reason repo-level pinning was
        chosen over per-plugin pins).

        Workflow: pin to a known-good SHA -> work undisturbed -> delete the pin field ->
        bootstrap restores the default branch + recorded autoUpdate and updates the
        marketplace -> test -> re-pin at the new SHA.
      gotchas:
        - Editing a layered bootstrap.json does NOT auto-bypass the per-project cooldown; after changing a pin, run bootstrap-reset-cooldown.sh (or wait out the window) for it to take effect.
        - A pin freezes FUTURE drift but never downgrades a plugin already past the snapshot (the version check is directional); you get a verbose ahead-of-pin notice, not a rollback.
        - pin takes precedence over alwaysUpdate (a one-line warning is emitted when both are set), and a min_version constraint the pinned snapshot cannot satisfy fails with a message naming the pin.
        - The first session after setting a pin can race Claude Code's auto-updater once (it may pull before bootstrap re-pins); self-heals on the next pass.
    - id: plugin_autoupdate_propagation
      summary: >-
        Two different flags govern updates -- bootstrap's `alwaysUpdate` refreshes the
        marketplace CLONE; Claude Code's `autoUpdate` bumps installed PLUGIN versions.
        A marketplace needs `autoUpdate: true` for its plugins to actually move;
        `alwaysUpdate` alone leaves installed versions stuck.
      keywords: [plugin not updating, stuck version, version not updating, autoUpdate, alwaysUpdate, extraKnownMarketplaces, known_marketplaces.json, plugin update, /plugin update, plugin marketplace update, publish not applying, installed_plugins.json, plugin-versions.sh, consumer update, auto-update plugins, marketplace not refreshing, restart not updating, reload-plugins]
      detail: |
        Two independent mechanisms, often confused -- a plugin can be published and
        still never reach a machine because the wrong one is set:

        - `alwaysUpdate` (a bootstrap.json `marketplaces[]` entry, engine-side): every
          session bootstrap `git`-refreshes the marketplace CLONE at
          ~/.claude/plugins/marketplaces/<name>. This freshens the LISTING
          (marketplace.json) only -- it does NOT bump installed plugin versions.
        - `autoUpdate: true` (a `known_marketplaces.json` field, Claude-Code-side):
          CC's own auto-updater, at session start, refreshes the clone AND bumps any
          installed plugin behind the listing -- rewriting installed_plugins.json and
          moving the plugin's cache version dir. THIS is what moves installed versions.

        So for a marketplace's plugins to auto-update, the marketplace needs
        `autoUpdate: true`. The clean, source-controlled place to set it is an
        `extraKnownMarketplaces` block in a project (or user) settings.json -- mirror an
        existing entry. A bootstrap.json `marketplaces` entry with only `alwaysUpdate`
        keeps the clone fresh but the plugins stay pinned -- the common "I declared the
        marketplace but my plugin won't update" trap.

        Publish != consumer activation. Publishing (version bump + push to the cache
        source branch) makes a version AVAILABLE on the remote. A consumer machine
        ACTIVATES it via CC autoUpdate (if `autoUpdate: true`) or a manual
        `/plugin marketplace update` + `/plugin update`. A plain restart does nothing
        when the marketplace lacks autoUpdate; `/reload-plugins` NEVER updates versions
        (it reloads registration only -- see plugin_reload_lifecycle).

        Seed-only convergence timing: a newly-added `autoUpdate` (or a freshly-published
        version) takes effect the session AFTER it is planted, because CC's auto-updater
        runs at startup BEFORE project SessionStart hooks -- the flag must be present
        before the updater reads it. Then the installed_plugins.json version bump
        auto-bypasses the per-project cooldown (see cooldown_registry_invalidation in the
        repo CLAUDE.md), so bootstrap runs the NEW installer in that same session. Net:
        hands-off, a restart or two to fully converge -- not instant.
      gotchas:
        - Diagnose with `bash scripts/plugin-versions.sh` (local / marketplace / installed / cached per plugin). installed_plugins.json holds the ACTIVATED version + installPath; a known_marketplaces.json entry with a stale `lastUpdated` and no `autoUpdate` is the tell that its plugins will never move.
        - Never hand-edit the plugin cache, installed_plugins.json, or settings to force a version -- bootstrap/CC re-sync from the activated version and revert it (and "Never copy files directly into the plugin cache" per the repo CLAUDE.md). Set `autoUpdate` (or run `/plugin update`) and let the update path do it.
        - "`pin` forces `autoUpdate: false` while set (see marketplace_pinning) -- a pinned marketplace will not auto-update its plugins by design."
        - A project settings.json `extraKnownMarketplaces` entry only fires in sessions for THAT project, but it seeds the GLOBAL known_marketplaces.json -- so once any such session runs, that machine auto-updates the marketplace everywhere.
    - id: deferred_requirements
      summary: >-
        A precondition only SOME capability needs is RECORDED, not escalated: `ctx.add_deferred_requirement`
        writes it to the plugin's deferred_requirements.json and produces no fix-all entry, no elevation
        task, and no session-start prompt. The action that needs it preflights, reads the recorded
        prepared statement, and asks the user then.
      keywords: [deferred requirement, add_deferred_requirement, defer until needed, do not escalate, no fix-all, point of need, ask late, credential prompt, API key nag, optional precondition, deferred_requirements.json, escalate vs defer, session-start noise]
      detail: |
        The rule: escalate a precondition only if a developer who never invokes the capability
        would still notice it was unmet. Otherwise defer it. A missing build tool, a broken venv,
        an unresolvable pin -> `add_failure`. A paid API credential, an opt-in specialty plugin,
        a GPU-only SDK -> `add_deferred_requirement`.
        Signature: `add_deferred_requirement(name, *, user_msg, agent_msg, satisfied_by=None)`.
        The engine writes every record to `<plugin data dir>/deferred_requirements.json`, rewriting
        it in full each pass and REMOVING it when the plugin defers nothing -- so a satisfied
        requirement clears itself with no separate clear step. That file is the handoff: the
        point-of-need code presents its `agent_msg` verbatim instead of carrying a paraphrase that
        can drift. Full pattern and wiring: references/deferred-requirements.md.
      gotchas:
        - Log a deferred requirement with `log_ok`, never `log` -- it is the EXPECTED state on a machine that does not use the capability, so `log` puts the session-start nag back by a different door.
        - "In a stdlib-only custom_bootstrap.py, guard the call: `getattr(ctx, \"add_deferred_requirement\", None)`. Fall back to SILENCE on an older engine, never to add_failure -- that fallback resurrects the prompt the deferral exists to remove."
        - A script that raises part-way deliberately does NOT rewrite the record; the previous pass's is better evidence than a truncated one. Do not add a write to the exception path.
        - Action-triggered plugin install (action_triggered_install) is the INSTANCE of this pattern where the missing precondition is a plugin -- not a separate mechanism.
    - id: action_triggered_install
      summary: >-
        Specialty plugins are declared with `install` set to `"manual"` and installed at the moment
        of need -- the skill that requires the plugin preflights for it, ASKS the user, then runs
        `claude plugin install <plugin>@<marketplace>`; the mid-session relaunch provisions it with
        no restart.
      keywords: [action-triggered install, opt-in plugin, install manual, specialty plugin, preflight, shared lib missing, ModuleNotFoundError, ask before install, claude plugin install, no restart, mid-session install, skill requirement, optional dependency, per-developer opt-in, do not auto-install for the team]
      detail: |
        Use when a plugin is needed by ONE skill / action rather than by the project as a whole
        (an optional live backend, a credentialed or paid integration, an admin utility). Three
        parts, all required:
        1. DECLARE, don't install -- a `plugins[]` entry with `install: "manual"`. The plugin is
           a named, version-managed marketplace member; the engine never installs/enables/scopes
           it, but does keep an already-installed copy fresh via `claude plugin update`.
        2. PREFLIGHT at the point of need -- test the published shared-lib dir
           (~/.claude/plugins/data/<mkt>/_shared_libs/<lib>/) or do a LAZY guarded import inside
           the code path. On a miss, name the capability and the owning plugin and ASK the user;
           offer the no-install fallback (mock backend, dry run) in the same breath.
        3. INSTALL on consent -- `claude plugin install <plugin>@<marketplace>`, then RETRY the
           action a prompt or two later. No restart: the UserPromptSubmit mid-session relaunch
           (plugins_state_hash) provisions the new plugin in the same session.
        Contrast with `install: "auto"`, which is for genuine dependencies -- a top-level import,
        a shared lib the plugin cannot load without, anything a developer who never runs the
        action would still notice missing. Full pattern, wiring steps, and the auto-vs-manual
        table: references/action-triggered-install.md.
      gotchas:
        - "`enabled` and `scope` are IGNORED for `install: \"manual\"` entries and `min_version` is honored only for `auto` -- a manual entry cannot force enablement, scope, or a version floor. Preflight for the capability, not the version."
        - Preflight with a LAZY import inside the call site. A top-level import of the optional lib turns a missing plugin into an unconditional module-load failure, breaking the code that was supposed to detect and report it.
        - Advise "retry in a moment", never "restart". An action fired instantly after the install can race the detached provisioning pass; that is a retry, not a restart.
    - id: env_manifest
      summary: env.json is bootstrap.json's identity-bearing sibling -- a separate manifest, same engine and pass, processed after bootstrap.json and before plugin manifests. It requires a `machines` registry (unknown machine = hard error), keys entries by os/hosts, and is GATED by an env_state.json stamp so it runs only when its merged content changed / last pass failed / engine bumped / reset. Carries the five declarative personalization features + the env_checks check/fix contract.
      keywords: [env.json, env.local.json, personalization, machines registry, machine identity, unknown machine, os cross-check, hosts filter, env gate, env_state.json, env-reset-cooldown, symlinks, shell_rc, macos_defaults, macos_hotkeys, login_items, env_checks, contract script, out-of-band drift]
      detail: |
        Homes (4 layers, lowest first): ~/.claude/env.json (primary tracked home,
        claude-settings repo), ~/.claude/env.local.json, <project>/.claude/env.json,
        <project>/.claude/env.local.json. Same merge discipline as bootstrap.json;
        identity keys differ (symlinks/shell_rc/login_items/env_checks by `name`,
        macos_defaults by domain+key, macos_hotkeys by `id`). `machines` is a
        hostname-keyed dict; the current host resolves exact-match-then-short-form,
        `os` is cross-checked against detect_os(), and every `hosts` filter must name
        a registered machine.

        The gate (env_state.json = merged-manifest sha256 + engine version + last
        result): the phase runs on first-run / hash-change / non-clean-last / engine-bump
        / stamp older than 24h (periodic re-check TTL, stamp mtime) / reset, else logs
        one "env: up to date" line. env-reset-cooldown.sh deletes the stamp AND clears
        the project bootstrap cooldown (which gates the whole pass).
        DRIFT TRADEOFF: the hostname is NOT in the stamp, so out-of-band drift (a
        hand-edited rc line, a deleted symlink, a machine rename with unchanged manifest,
        a remote repo drifting ahead of a repo-sync'd clone) is not auto-healed until an
        edit / failure / engine upgrade / reset -- but the 24h TTL bounds the window to
        about a day.

        env.json has NO env_vars section and NO PATH edits (both are bootstrap.json's
        alone). All env.json failures are manual-attention -- the engine already ran the
        fix. Full schema: references/manifest-reference.md (the env.json section).
      gotchas:
        - Unknown machine, os mismatch, hosts-filter typo, and a missing `machines`
          registry are all hard errors (one descriptive fix-all item each); bootstrap.json
          provisioning is unaffected -- personalization refuses to guess.
        - env_checks check/fix values are opaque shell strings resolved BY THE SHELL
          (tilde-anchored), not plugin-rooted paths like the bootstrap.json `script` phase.
          A check that cannot run (timeout/no shell) is a persistent failure and the fix is
          never attempted; the re-check is authoritative with no trust exceptions.
    - id: merge_semantics
      summary: Layered configs merge by identity key for arrays, deep-merge for objects, override for scalars.
      keywords: [merge semantics, union, identity key, deep merge, path entries, scalar override]
      detail: |
        - Arrays (tools, plugins, marketplaces): unioned by identity key (name, ref). Same identity in multiple layers is DEEP-merged -- a user override for `tools[name=jq].download[macos-arm64].url` keeps every other download key and the sha256 intact. Higher priority wins at any leaf.
        - Objects (venv, config): deep-merged; higher priority wins for conflicts.
        - path_entries: string-list union, deduplicated, order preserved.
        - Scalars: higher priority wins.
        Layered configs are merged before plugin bootstrap.json files are processed.
  groupings:
    - name: engine_behavior
      keywords: [engine, session start, processing order, messages, remediation flow, update, harvest, restart, claude --resume]
      fact_ids: [message_outcomes, update_lifecycle, manual_convergence, remediation_phases]
    - name: config_files
      keywords: [bootstrap.json, env.json, manifest, layers, merge, override, pin, auto-update, autoUpdate, plugin not updating, machines registry, env gate, personalization, install manual, opt-in plugin, action-triggered install]
      fact_ids: [config_layers, env_manifest, marketplace_pinning, plugin_autoupdate_propagation, action_triggered_install, merge_semantics]
    - name: catalogues
      keywords: [conditions, categories, remediation table]
      fact_ids: [condition_categories]
  references:
    - id: engine_internals
      path: references/engine-internals.md
      keywords: [engine, internals, processing order, self-setup, manifest phase, script phase, messaging protocol, execution flow, throttling, first run, clean install, phases, design principles, shared library, hybrid model, agent_skills_link, agent skills link, codex skills, .agents, .agents/skills, agents directory]
      summary: Engine internals deep-dive.
    - id: manifest_reference
      path: references/manifest-reference.md
      keywords: [bootstrap.json, env.json, manifest, schema, fields, variable expansion, layered config, merge semantics, identity keys, example, marketplace pin, pin field, unpin workflow, machines registry, env gate, env_checks, symlinks, shell_rc, macos_defaults, macos_hotkeys, login_items, personalization, agent_skills_link, codex, .agents/skills]
      summary: bootstrap.json manifest field reference (incl. the marketplace pin field, the unpin workflow, and the agent_skills_link Codex-discovery opt-out) PLUS the sibling env.json personalization manifest (machines registry, env gate, the five declarative features, and the env_checks contract).
    - id: remediation_reference
      path: references/remediation-reference.md
      keywords: [condition, remediation, check method, tool missing, venv broken, marketplace, plugin scope, fix-all, blocking, manual operation, pinned wrong commit, pin removed, unresolvable pin, agent_skills_link, agent skills link, codex, .agents, symlink, junction, p4ignore, info/exclude]
      summary: Per-condition remediation reference (incl. the marketplace pin conditions and the agent_skills_link Git/P4/Windows-junction failure modes).
    - id: deferred_requirements_ref
      path: references/deferred-requirements.md
      keywords: [deferred requirement, add_deferred_requirement, defer until needed, escalate vs defer, no fix-all, session-start nag, API key prompt, optional precondition, deferred_requirements.json, point of need, ask late, custom_bootstrap author guide]
      summary: >-
        The deferred-requirement pattern -- detect early, ask late. The escalate-vs-defer rule,
        the ctx.add_deferred_requirement API and its on-disk record, how point-of-need code
        consumes it, and the plugin-author wiring steps.
    - id: durable_project_data_ref
      path: references/durable-project-data.md
      keywords: [durable project data, plugin data dir, plugin_data_dir, .plugin-data, .local-data, tracked generated artifact, version control, project VCS, explicit refresh, size, churn]
      summary: >-
        The plugin-author pattern for choosing durable versus ephemeral project data,
        resolving the namespaced path and override, and keeping SessionStart read-only.
    - id: action_triggered_install_ref
      path: references/action-triggered-install.md
      keywords: [action-triggered install, opt-in by encountering need, install manual, specialty plugin, preflight, guarded import, shared lib path check, ask before install, claude plugin install, mid-session install no restart, skill requirement wiring, auto vs manual, optional dependency]
      summary: >-
        The action-triggered plugin-install pattern -- declare the plugin manual-install, preflight
        in the action that needs it, ask the user, install mid-session (no restart); plus the
        skill-author wiring steps and the auto-vs-manual decision table.
    - id: plugin_setup_pattern
      path: references/plugin-setup-pattern.md
      keywords: [setup pattern, config setup, setup.py, interactive setup, --check, --describe, --apply, --init-defaults, missing config, API keys]
      summary: Plugin setup-pattern recipe.
    - id: dependency_philosophy
      path: references/dependency-philosophy.md
      keywords: [philosophy, principles, local-first, ~/.local, repair_path, find-or-download, absolute path, target architecture, full execution chain, installed_but_path_stale, why not PATH, design intent]
      summary: Bootstrap's dependency-management philosophy and target architecture.
    - id: plugin_reload_lifecycle
      path: references/plugin-reload-lifecycle.md
      keywords: [reload-plugins, restart, restart IDE, hook reload, registration, SessionStart re-fire, plugin update, when to reload, when to restart, script content live, cache version dir, reload advisory, _reload_advice, harvest, single-session update, engine_ran_version, claude --resume, advise update, cooldown status, anomaly, two skip gates]
      summary: Measured rule for when a plugin change is live vs needs /reload-plugins vs needs a restart (the three layers code/registration/firing); informs the Step 4d reload nag. ALSO the operational guide for advising on a bootstrap update -- the single-session harvest, the two skip gates + --resume, which state files to read, and the anomaly checklist.
    - id: library_consumption_ref
      path: references/library-consumption.md
      keywords: [shared library, shared_libs, shared_lib_imports, standalone python, foreign interpreter, project venv, sitecustomize, PYTHONPATH shim, off-fleet pip install, vendored copy, version declaration, min_version, capability probe, non-plugin consumer]
      summary: >-
        Every supported way a consumer can reach a published shared library --
        plugin (shared_lib_imports), the standalone Python, or a project on its
        own interpreter -- versus the one unsupported way (off-fleet pip
        install), and the cross-cutting rule that no mode supports version
        declaration.
```
