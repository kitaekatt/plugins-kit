# prototypes

Experimental skills awaiting graduation. Skills live here while they prove
themselves; a skill graduates to its own plugin only after its graduation
gates (explicit, per-skill criteria that must hold and stay tested) are met.

## Current inventory

- **claude-explorer** -- a self-contained, localhost-only web view of your
  Claude filesystem: `~/.claude/` plus the current project. Marketplaces,
  plugins, skills, reference docs, and CLAUDE.md files render in a browser
  as openable containers; leaf files (markdown, JSON, scripts) deep-render
  inline on click. Two phases: a Python crawl writes a JSON index, then a
  stdlib HTTP server serves the embedded single-page app on
  `127.0.0.1:8923`. Read-only.

  Security posture: binds `127.0.0.1` only, rejects requests whose `Host`
  header is not localhost (DNS-rebinding guard), and path-guards the
  `/file?path=...` endpoint so it only serves files under `~/.claude/` or
  the project root. Its graduation gates are exactly these properties
  staying enforced and tested.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install prototypes
```

Invoke claude-explorer via its skill, or run the script directly:

```
~/.claude/plugins/data/plugins-kit/prototypes/.venv/bin/python \
  <plugin-root>/skills/claude-explorer/scripts/claude_explorer.py run
```

(`Scripts/python.exe` on Windows. Falls back to plain `python3` in a
degraded mode -- the script is stdlib-only except optional PyYAML.)

Expect churn: skills here may change shape or be removed without the
compatibility care a graduated plugin gets.
