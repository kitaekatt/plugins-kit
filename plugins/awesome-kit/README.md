# awesome-kit

See every marketplace, plugin, and skill you have installed at a glance --
one command renders them as a single self-contained HTML poster.

## What it does

`/plugin-ecosystem` generates a self-contained 16:9 HTML poster of your
installed Claude Code plugin ecosystem: one column per marketplace, one
clickable card per plugin, and a side panel listing each plugin's skills.
The output is a single HTML file with no external dependencies -- this
repo's own landing page (`index.html`) is its output, built with
`--marketplace plugins-kit --output ./index.html`.

Also in the box, one line each:

- `/html-pdf` -- convert an HTML file to a PDF via headless Chromium
  (single-page or `--a4` paginated).
- A task-folder system (the `task` skill) -- file-backed task folders
  with a CLI for create/list/work/close/archive/move.
- `orchestrate` -- delegate significant work to background agents so the
  main context holds conclusions rather than work product. Its model
  tiers, dispatch backends (the Agent tool, the Codex CLI, or one you
  add), and usage-capacity reporting are configuration: a generator
  script renders the policy for the machine it runs on, layering
  `skills/orchestrate/defaults/orchestration.yaml` under a per-user and a
  per-project override. See
  [skills/orchestrate/references/configuration.md](skills/orchestrate/references/configuration.md).
- `verbose-updates` -- a smaller supporting skill.

## Poster personalization

Display copy layers through optional `poster.yaml` files at the
operator, marketplace, plugin, and skill levels, so authors can write
poster-facing blurbs without touching runtime skill descriptions.
Honestly: a marketplace does not appear at all unless it ships a
marketplace-level `poster.yaml` (opt-in gate), and plugins without their
own overlay render minimally from plugin.json and SKILL.md metadata --
the poster is only as good as the metadata its authors provide.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install awesome-kit@plugins-kit
```

## Try this first

```
/plugin-ecosystem
```

Writes `~/.claude/plugin-ecosystem.html` and opens it in your browser.
Useful flags: `--marketplace <name>` to filter, `--no-open`,
`--output <path>`, `--defaults` (show project defaults instead of your
live toggles).

## When not to use it

If you just want a text list of installed plugins, `/plugin` already
shows it; the poster is for browsing and sharing, not administration.
