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
    - id: remediation_phases
      summary: The engine remediates silently first; only escalates to fix-all when user action is required.
      keywords: [auto-remediation, fix-all, two-phase, silent install, remediation flow, autodetect, default values]
      detail: |
        Phase 1 (silent): tool installs (run install command, re-check); config autodetect (plugin manifest's "autodetect" script discovers and fills required fields, e.g. scanning CWD for a .uproject); default values from manifest "default" fields.
        Phase 2 (fix-all): aggregates remaining failures into a single fix-all message. additionalContext (seen by the agent) carries numbered remediation steps; systemMessage (seen by the user) carries the bootstrap log of what was checked and what failed. User types "fix-all" or "fixed" to re-run bootstrap after remediation.
      example:
        input: A plugin declares uproject and engine_dir as required fields, plus an autodetect script that scans CWD.
        output: Engine copies the default config (empty values), calls autodetect, fills both fields, validates required-field presence, no fix-all needed. If autodetect only finds uproject, engine_dir becomes a fix-all item.
      gotchas:
        - Autodetect runs before required-field validation. A plugin's autodetect script can fill required fields silently; if autodetect partially succeeds, the remaining fields surface as fix-all items.
        - fix-all re-runs the engine after remediation. If issues persist after fix-all, the cause is likely outside the engine's known remediation paths.
    - id: condition_categories
      summary: Ten categories of remediable condition the engine knows how to address.
      keywords: [tool, PATH, venv, git dependency, JSON config, INI settings, PyPI package, marketplace, plugin, user config, condition categories, remediation]
      detail: |
        | Category       | Examples                              | Remediation                              |
        |----------------|---------------------------------------|------------------------------------------|
        | Tool           | uv, git, gh CLI not installed         | Platform-specific install + re-check     |
        | PATH           | ~/.local/bin not in PATH              | Modify persistent PATH config            |
        | Venv           | Python venv missing or broken         | uv sync from pyproject.toml              |
        | Git dependency | Repo not cloned, wrong branch/commit  | clone once; pinned commits re-checkout; no steady-state pull |
        | JSON config    | File lacks expected entries           | Merge missing entries into target JSON   |
        | INI settings   | Application config setting not set    | Write setting to config/ini file         |
        | PyPI package   | Extracted file missing locally        | Download from PyPI and extract           |
        | Marketplace    | Not registered or stale               | claude plugin marketplace add/update     |
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
    - id: merge_semantics
      summary: Layered configs merge by identity key for arrays, deep-merge for objects, override for scalars.
      keywords: [merge semantics, union, identity key, deep merge, path entries, scalar override]
      detail: |
        - Arrays (tools, plugins, marketplaces): unioned by identity key (name, ref). Same identity in multiple layers is DEEP-merged — a user override for `tools[name=jq].download[macos-arm64].url` keeps every other download key and the sha256 intact. Higher priority wins at any leaf.
        - Objects (venv, config): deep-merged; higher priority wins for conflicts.
        - path_entries: string-list union, deduplicated, order preserved.
        - Scalars: higher priority wins.
        Layered configs are merged before plugin bootstrap.json files are processed.
  groupings:
    - name: engine_behavior
      keywords: [engine, session start, processing order, messages, remediation flow]
      fact_ids: [message_outcomes, remediation_phases]
    - name: config_files
      keywords: [bootstrap.json, manifest, layers, merge, override]
      fact_ids: [config_layers, merge_semantics]
    - name: catalogues
      keywords: [conditions, categories, remediation table]
      fact_ids: [condition_categories]
  references:
    - id: engine_internals
      path: references/engine-internals.md
      keywords: [engine, internals, processing order, self-setup, manifest phase, script phase, messaging protocol, execution flow, throttling, first run, clean install, phases, design principles, shared library, hybrid model]
      summary: Engine internals deep-dive.
    - id: manifest_reference
      path: references/manifest-reference.md
      keywords: [bootstrap.json, manifest, schema, fields, variable expansion, layered config, merge semantics, identity keys, example]
      summary: bootstrap.json manifest field reference.
    - id: remediation_reference
      path: references/remediation-reference.md
      keywords: [condition, remediation, check method, tool missing, venv broken, marketplace, plugin scope, fix-all, blocking, manual operation]
      summary: Per-condition remediation reference.
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
      keywords: [reload-plugins, restart, restart IDE, hook reload, registration, SessionStart re-fire, plugin update, when to reload, when to restart, script content live, cache version dir, reload advisory, _reload_advice]
      summary: Measured rule for when a plugin change is live vs needs /reload-plugins vs needs a restart (the three layers code/registration/firing); informs the Step 4d reload nag.
```
