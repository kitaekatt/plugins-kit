---
_schema_version: 1
name: plugin-ecosystem
author: christina
description: Use when generating or refreshing the Claude Code plugin ecosystem poster (16:9 HTML browser of marketplaces and plugins). Do NOT use for skill authoring.
---

## Skill Purpose

Generate `~/.claude/plugin-ecosystem.html` -- a self-contained 16:9 poster that visualizes the user's installed Claude Code plugin ecosystem. Each marketplace gets a column; each plugin a clickable card; clicking opens a side panel with the plugin's value-prop and skill list. The script overwrites the same file on every run and opens it in the browser.

## Framework

This skill operationalizes the **plugin_ecosystem** viewer-kind under the shared audit framework. The shared glossary (`subject`, `primitive`, `composition`, `discovery`, `viewer-kind`, `summary projection`, layered personalization, self-parameterizing overrides) is canonical at `plugins/skills-kit/skills/md-domain/references/audit-framework.md`. The sibling viewer `prototypes:claude-explorer` operationalizes a deeper viewer-kind over the same substrate (drills into each skill instead of stopping at skill-name level, and supports leaf-primitive deep-rendering).

In framework terms, plugin-ecosystem is:

- **Subject:** `marketplace` composition; **subject_type:** corpus (every installed marketplace under `~/.claude/plugins/marketplaces/`).
- **Compositions traversed:** `marketplace ⊃ plugin ⊃ skill` (stops at skill-name; for deeper drill use `claude-explorer`).
- **Primitives consumed:** `marketplace_manifest` (gate), `plugin_manifest` (card data), `skill_md` frontmatter (skill list), plus per-level `poster.yaml` overrides.
- **Discovery gate:** a marketplace participates only if it ships `.claude-plugin/poster.yaml`.
- **Layered personalization:** four `poster.yaml` layers (operator / marketplace / plugin / skill -- see "Data Model" below).
- **Viewer scaffolding:** `scripts/generate.py` (stdlib-only, single self-contained HTML).

## When to Use

- "Show me the plugin ecosystem"
- "Regenerate / refresh the plugin poster"
- "Show the project defaults" / "what would a fresh user see" -> pass `--defaults` (see Invocation Keywords)
- "Make the poster reflect SpryFox defaults" (use the `states:` override block in the user config)
- "Set the poster title to X" / "set the tagline to Y" (edit the user config)
- "Add `<marketplace>` to the poster" (author a `.claude-plugin/poster.yaml` in that marketplace's repo)

## Invocation Keywords

When the user invokes the skill with an argument, map the keyword to the right CLI flag:

| User says... | Pass to script |
|--------------|---------------|
| `default`, `defaults`, "project defaults", "as a new user would see it", "ignore my settings" | `--defaults` |
| `no-open`, "don't open" | `--no-open` |
| `<name> marketplace`, "just the X marketplace", "only spryfox-plugins", "filter to plugins-kit", "for marketplace X" | `--marketplace <name>` (repeat or comma-separate for multiple) |

`--defaults` sources the on/off badge straight from project `bootstrap.json` declarations and ignores the operator's live `settings.json` toggles. Use it to depict "how this project ships" regardless of who's running the skill.

`--marketplace NAME` restricts the poster to one (or several) opted-in marketplaces. When exactly one marketplace remains, the column grid collapses to a single centered column -- ideal for generating a per-marketplace `index.html` landing page. Combine with `--title`, `--output`, and `--no-open` for headless index-page builds:

```bash
generate.py --marketplace plugins-kit --title "plugins-kit marketplace" \
            --output ./index.html --no-open
```

## How to Invoke

```bash
uv run python "${CLAUDE_PLUGIN_ROOT}/skills/plugin-ecosystem/scripts/generate.py"
```

The script is stdlib-only; `uv run python` is the repo-standard cross-platform invocation (resolves a working interpreter on macOS, Windows, and Linux).

Optional flags:
- `--project PATH` -- project root (defaults to cwd). Determines which `bootstrap.json` and `settings.json` are read for live state.
- `--config PATH` -- user-level config YAML (default: `~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml`).
- `--output PATH` -- HTML output path (default: `~/.claude/plugin-ecosystem.html`).
- `--title TEXT` -- one-shot title override (the config YAML is the persistent home).
- `--no-open` -- write the file without opening it in the browser.

Stdlib only; the generated HTML is a single self-contained file.

## Data Model

The poster pulls from four sources, each owned by a different party:

| Layer | Owner | Where it lives |
|-------|-------|----------------|
| Marketplace column subtitle + opt-in | Marketplace maintainer | `<marketplace-repo>/.claude-plugin/poster.yaml`, read locally from `~/.claude/plugins/marketplaces/<name>/.claude-plugin/poster.yaml` |
| Plugin name / description / razor | Plugin author | `<plugin>/.claude-plugin/plugin.json` (the optional `razor` field is the side-panel blurb) |
| Plugin display overrides (card description, razor, per-skill blurbs) | Plugin author | `<plugin>/.claude-plugin/poster.yaml` (alongside plugin.json). All fields optional. Lets the plugin author write poster-facing copy without changing each skill's activation `description:`. |
| Skill name / description / author | Skill author | `<skill>/SKILL.md` YAML frontmatter. The poster falls back to `description:` when the plugin's `poster.yaml` doesn't override it. `author:` renders as "by {author}" beside the skill name. |
| Title / tagline / per-plugin state overrides | Poster author | `~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml` |
| Live on/off state | Project / user | `enabledPlugins` merged across project + user `settings.json`, falling back to project `bootstrap.json` |

### Marketplace opt-in (the gate)

A marketplace appears in the poster **only** if its repo ships `.claude-plugin/poster.yaml`. Marketplaces without one are excluded entirely, even if their plugins are installed. This keeps random third-party marketplaces from polluting the poster -- only marketplaces that have authored their poster identity participate.

The `poster.yaml` schema (all fields optional):

```yaml
subtitle: "Christina's open source plugin repository"
url: "https://github.com/example/marketplace"
states:
  bootstrap: required   # marketplace-author declaration; see "State precedence" below
```

`states:` is keyed by short plugin name (no `<marketplace>:` prefix -- it is already scoped to this marketplace). Values: `on`, `off`, `opt-in`, `required`. Use `required` for plugins that are structurally non-optional (other plugins in the marketplace won't work without them). `required` renders with a distinct purple badge and sorts above `on` within the column.

To add a marketplace: create that file in the marketplace repo, commit + push, then on the user's machine the next bootstrap pull syncs it into `~/.claude/plugins/marketplaces/`.

### State precedence

For each plugin, the badge is computed in this order (first match wins):

1. `states:` map in the user config YAML (poster author's depiction override), keyed by `<marketplace>:<plugin>` or just `<plugin>`. Values: `on`, `off`, `opt-in`, `required`.
2. `states:` map in the **marketplace's** `poster.yaml` (marketplace owner's declaration), keyed by short plugin name. This is where `required` normally lives -- the marketplace asserts structural facts about its own plugins.
3. `enabledPlugins` in project `<cwd>/.claude/settings.local.json`, then `<cwd>/.claude/settings.json`, then `~/.claude/settings.json`. `true` -> on, `false` -> off.
4. Project `<cwd>/.claude/bootstrap.json` declaration. `enabled: true` -> on, `install: manual` -> opt-in, anything else declared -> off.
5. Default -> "unmanaged" (installed but neither enabled nor declared).

**SpryFox-defaults poster recipe**: drop a `states:` map into the user config that mirrors what the SpryFox `bootstrap.json` declares. Re-run -- the badges reflect SpryFox defaults regardless of the user's personal overrides.

### User config YAML

`~/.claude/.local-data/awesome-kit/plugin-ecosystem-poster.yaml`:

```yaml
title: "Spirit Crossing Claude Plugin Ecosystem"
tagline: "Use /plugin to change your claude-code plugins, you decide what's active!"
states:
  spryfox-plugins:designer: on
  spryfox-plugins:claude-admin: opt-in
```

All keys optional. Defaults: title = "Claude Plugin Ecosystem", tagline = "" (no text), states = {} (use live values).

## When the User Asks To Customize

| User asks... | What to do |
|--------------|-----------|
| "Change the title to X" | Edit `title:` in the user config YAML, re-run skill |
| "Add a tagline that says Y" | Edit `tagline:` in the user config YAML, re-run skill |
| "Show this plugin as on/off" | Add `<marketplace>:<plugin>: on` (or off / opt-in) to `states:` in the user config |
| "Add `<marketplace>` to the poster" | Create `.claude-plugin/poster.yaml` in that marketplace's repo with at least a `subtitle:`, then commit + push |
| "Change the subtitle for `<marketplace>`" | Edit `.claude-plugin/poster.yaml` in that marketplace's repo (NOT the user config) |
| "Make the poster reflect SpryFox defaults" | Populate `states:` in the user config to match what SpryFox `bootstrap.json` declares for each plugin |

User config = poster author's knobs. Marketplace `poster.yaml` = marketplace maintainer's knobs. Plugin `poster.yaml` = plugin author's knobs. Don't conflate.

### Per-plugin poster.yaml schema

```yaml
# Lives at <plugin>/.claude-plugin/poster.yaml. All fields optional.
description: "card-line override (falls back to plugin.json description)"
razor: "side-panel razor override (falls back to plugin.json razor)"
hidden: true   # omit the plugin from the poster entirely (published but not
               # poster-worthy, e.g. a temporary remediation plugin)
skills:
  <skill-name>: "side-panel description override (falls back to SKILL.md description)"
```

## Anti-Patterns

- **Hand-editing the generated HTML** -- it is overwritten on every run.
- **Hard-coding marketplace subtitles in `generate.py`** -- always live in the marketplace's `poster.yaml`.
- **Putting per-marketplace knobs in the user config** -- those belong to the marketplace owner, not the poster author.
- **Using `states:` to mask incorrect live state** -- if `enabledPlugins` says the wrong thing, fix `settings.json`, not the override map. Use overrides for "what would this look like under a different config" exploration.
