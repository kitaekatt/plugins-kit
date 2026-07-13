# skill-audit usage: report subcommands

Full usage reference for the `roster` and `hierarchy` subcommands of the skill-audit member (reached via `/md-audit skill`), and their underlying script `scripts/report.py`. Loaded when the agent needs the precise argument set, location semantics, output-shape contract, or implied-frontmatter rules.

For the single-file audit operation (the namesake `/md-audit skill <path>` / `list` / `<numbers>` flows), invoke `/md-audit skill`; the placement framework lives at `cohesion-principles (in skills-kit)`.

## Invocation

Reached through the `/md-audit skill` front door (the member's standalone slash command was collapsed). The first argument after `/md-audit skill` selects which operation to run; the two report subcommands are:

```
/md-audit skill roster                     # markdown to <project-root>/tmp/skill-roster.md (default)
/md-audit skill roster tmp/skills.md       # markdown to that path
/md-audit skill roster -                   # markdown body printed to stdout (rendered in chat)
/md-audit skill hierarchy                  # interactive HTML to <project-root>/tmp/skill-hierarchy.html
/md-audit skill hierarchy tmp/x.html       # HTML to that path
/md-audit skill hierarchy -                # HTML printed to stdout
```

Direct script invocation (under the plugin's uv venv):

```
uv run python "${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/scripts/report.py" \
    [roster|hierarchy] [out|-] [--cwd <dir>]
```

The HTML renderer (also runnable directly for dev iteration; supplies the backend for `/md-audit skill hierarchy`):

```
uv run python "${CLAUDE_PLUGIN_ROOT}/skills/skill-audit/scripts/skill_hierarchy_report.py" \
    [--project-root PATH] [--out PATH] [--installed-plugins PATH] [--user-skills PATH]
```

### Positional and flag reference

- `report_type` (positional) -- `roster` (markdown) or `hierarchy` (HTML). Omit to print the usage block and exit.
- `out` (positional, optional) -- write the report to `<out>`. Default: `<project-root>/tmp/skill-roster.md` for `roster`, `<project-root>/tmp/skill-hierarchy.html` for `hierarchy`. Pass `-` to write the body to stdout instead.
- `--cwd <dir>` -- treat `<dir>` as the project root for the Project tier (default: process cwd). The `tmp/` default path is computed relative to this.

On every file-write invocation the script echoes the resolved absolute path so callers can relay it to the user.

Exit codes: `0` on success (including the no-args usage block); non-zero on argument-parse errors (e.g. an unrecognized `report_type`). The script does not fail on per-file parse problems -- it skips unreadable or malformed SKILL.md files silently and reports what it could parse.

## Hierarchy report

`/md-audit skill hierarchy` delegates to the `render_html(corpus)` function in the sibling `skill_hierarchy_report.py` module. Output is a single self-contained HTML file (no external assets, no JavaScript):

- A three-level `<details>`/`<summary>` hierarchy: All -> User/Project/Plugins -> per-marketplace plugin tables. The top-level `All` is open by default; everything below collapses.
- Each table's columns are the union of every frontmatter key in that section's skills, with `name` first and `description` last; ultra-wide-monitor friendly (no width cap).
- The `skill-type` cell carries a hover tooltip describing the type's purpose, audit criterion, prohibited patterns, required frontmatter, and required contract-block fields.
- Plugins with no skills are dropped; marketplaces with no skill-bearing plugins are dropped.

The hierarchy report does NOT use the per-type implied-frontmatter convention described below -- that is a roster-only feature, intentional because the hierarchy already shows every frontmatter key as its own column.

## Locations resolved

Both subcommands enumerate three sets of roots, in this fixed order:

1. **User**: `~/.claude/skills/**/SKILL.md`. Personal global skills installed under the user's home directory.
2. **Project**: `<cwd>/.claude/skills/**/SKILL.md`. Skills checked into the current project. Resolved against `--cwd` (default: process cwd). When invoked from outside a project tree this section is empty.
3. **Plugin: \<name\>** (one per active install). Source of truth: `~/.claude/plugins/installed_plugins.json`. For each plugin entry the script reads the active install's `installPath/skills/**/SKILL.md`. Multiple-version cache directories under `~/.claude/plugins/cache/` are NOT scanned directly; only the version listed as installed is reported, so stale on-disk versions do not pollute the report.

The plugin section header is `Plugin: <plugin_name> (<marketplace>, v<version>)`. `<marketplace>` is taken from the manifest key suffix (`<plugin>@<marketplace>`).

## Roster grouping

Within each location:

- Skills are grouped by `skill-type`, with technique-skill further qualified as `(user-only)` or `(auto)`.
- Type ordering is alphabetical (`capability-skill`, `discipline-skill`, `domain-skill`, `pattern-skill`, `reference-skill`, `technique-skill (auto)`, `technique-skill (user-only)`).
- Within a type, skills are sorted alphabetically by frontmatter `name`.

## Skill-type detection

For each SKILL.md the script reads:

- The YAML frontmatter (delimited by `---` lines).
- The first ```` ```yaml ```` fenced block in the body, which carries the type contract per the skill-authoring framework.

The skill type is taken from frontmatter `skill-type:` if present; otherwise inferred from the contract block's root key (`technique_skill`, `domain_skill`, etc.). The technique-skill variant is taken from `technique_skill.trigger_model` in the body block: `user-only` -> user-only variant, anything else -> auto.

If neither source declares a type, the type is reported as `(unknown)`.

## Implied frontmatter -- the no-duplication contract (roster only)

Each `(skill-type, variant)` group declares the frontmatter values implied by the contract once at the top of the group. Per-skill rows then suppress any flag whose actual value matches the implied value, and surface only flags that DIFFER.

Implication map:

| skill-type / variant         | disable-model-invocation | user-invocable |
|------------------------------|--------------------------|----------------|
| technique-skill (user-only)  | true (implied)           | true (implied) |
| technique-skill (auto)       | false                    | false          |
| reference-skill              | false                    | false          |
| pattern-skill                | false                    | false          |
| discipline-skill             | false                    | false          |
| domain-skill                 | false                    | false          |
| capability-skill             | false                    | false          |

Only entries marked "implied" are echoed in the group header. A user-only technique-skill therefore reads:

```
### technique-skill (user-only)

_Implied frontmatter: `disable-model-invocation: true`, `user-invocable: true`_
```

A skill row in that group will list `disable-model-invocation` only when it is set to false (overriding the contract), and likewise for `user-invocable`. A skill in a non-user-only group will list either flag only when it is set to true.

This is the "don't duplicate information" rule: the contract states the default; the per-skill row states only what departs from it. The hierarchy view does not apply this rule -- it shows every frontmatter key as a column instead.

## Per-skill row format (roster)

```
- **<name>** [author: <author>] -- <description>
  - <non-implied flag>, <non-implied flag>
```

- `[author: ...]` is omitted when the skill has no `author:` frontmatter line.
- The trailing `-- <description>` is omitted when frontmatter has no `description:` field.
- The flag-line beneath the row is omitted when no flags differ from the implied set.

## Worked example (roster)

A user-only technique-skill group with a clean contract:

```
### technique-skill (user-only)

_Implied frontmatter: `disable-model-invocation: true`, `user-invocable: true`_

- **skill-audit** [author: christina] -- Use when md-audit dispatches a SKILL.md audit ...
- **claude-md-audit** [author: christina] -- Use when md-audit dispatches a CLAUDE.md audit ...
```

Same group with one skill that is unusually NOT user-invocable:

```
- **internal-only-tool** [author: bob] -- Internal tool, model-only
  - `user-invocable: false`
```

The flag is surfaced because it differs from the implied `user-invocable: true`.

## Gotchas

- The script reports based on `installed_plugins.json`. If a plugin was just edited on disk but the manifest has not been refreshed, the new version's SKILL.md may not appear under the Plugin tier. The plugin-version banner echoed before the report is the authoritative signal of which skills-kit version actually ran.
- `(unknown)` skill-type usually means the SKILL.md is missing both a frontmatter `skill-type:` line and a recognized contract root in its body YAML. Treat it as a hint to inspect the file with `/md-audit skill <path>`.
- Project tier honours `--cwd`. When this skill is invoked from a different working directory the report will reflect that directory's `.claude/skills/`, not the directory the agent typically runs in.
- The scripts read files only. They do not edit SKILL.md frontmatter, do not call P4 or git, and do not write outside the resolved `out` path.
