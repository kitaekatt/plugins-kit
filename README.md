# plugins-kit

[![tests](https://github.com/kitaekatt/plugins-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/kitaekatt/plugins-kit/actions/workflows/tests.yml)

Individual plugins for [Claude Code](https://code.claude.com). **The deliverable is the
plugin, not the marketplace** — `plugins-kit` is the shared home and dependency substrate
the plugins ride on.

Browse the full catalog on the [marketplace landing page](https://kitaekatt.github.io/plugins-kit/).

## Installing

These plugins install through Claude Code's plugin marketplace mechanism. Add the
marketplace, then install the plugins you want:

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install bootstrap
/plugin install p4-kit        # or unreal-kit, git-kit, skills-kit, ...
```

Most plugins depend on **bootstrap** and declare it as a dependency, so installing a
plugin pulls bootstrap in automatically. To update later:

```
/plugin marketplace update
/plugin update
```

## Your first session

On the first session start after installing any plugin, the **bootstrap** plugin
provisions its dependencies: it downloads `uv`, creates a per-plugin Python venv, and
installs any system tools the plugin declares (such as `gh`). Prerequisite: `bash` on
your PATH — on Windows, that means Git Bash.

Healthy bootstrap is **silent** — no output at session start means every check passed.
To verify a plugin was actually provisioned, read its log at
`~/.claude/plugins/data/plugins-kit/<plugin>/bootstrap.log`.

## What I'm building

A handful of focused plugins that extend Claude Code with skills, slash commands, and
hooks. The headliners:

- **bootstrap** — a dependency / provisioning engine every plugin rides on. Declare your
  tools, venvs, git dependencies, marketplaces, and per-user config in a `bootstrap.json`,
  and bootstrap brings the environment into that state automatically at session start. No
  manual `pip` / `venv` / clone steps — healthy bootstrap is silent.
- **p4-kit** — a Perforce-based code reviewer: multi-agent review of pending changelists
  (`/p4-code-review`), with parallel Claude reviewers plus per-issue validators.
- **unreal-kit** — skills and tools that let Claude function as an Unreal developer: a
  Python API for asset inspection and data extraction, an MCP server for driving the
  editor, and redirector cleanup.

## All plugins

| Plugin | What it does |
|--------|--------------|
| **bootstrap** | Dependency/provisioning engine — tools, venvs, git deps, marketplaces, and config from a `bootstrap.json`. Foundation for everything else. |
| **p4-kit** | Multi-agent code review of pending Perforce changelists (`/p4-code-review`). |
| **unreal-kit** | Unreal Engine automation — Python asset API, MCP editor control, redirector cleanup. |
| **git-kit** | Git + GitHub multi-agent code review (`/git-code-review`) plus `gh` CLI bootstrap. |
| **skills-kit** | Authoring and auditing skills + `CLAUDE.md` files — a verb × artifact matrix (`/md-authoring`, `/md-audit`, cohesion principles). |
| **awesome-kit** | Cross-domain skills: a shared communication framework, `/plugin-ecosystem`, `/html-pdf`. |
| **openrouter-kit** | OpenRouter API key management + a shared model registry. |
| **claude-ui-kit** | Status line with context-window and rate-limit threshold colors, plus `/statusline`. |
| **cache-kit** | Cache-usage reporting — per-request and session-level cache hit analysis from transcripts. |
| **hue-kit** | Philips Hue layered-scene framework — bridge sync, editable YAML scenes, meta-group solver, HTML report. |
| **bootstrap-stuck-fix** | Temporary remediation shim — repairs a wedged bootstrap registry record that pins a machine on an old version. |
| **prototypes** | Experimental skills awaiting graduation into their own plugins. |

## How it fits together

Every plugin that ships Python or external tools rides on **bootstrap**. At session start
bootstrap reads each enabled plugin's `bootstrap.json` and ensures the system tools,
per-plugin venvs, git dependencies, and per-user config are in the state that plugin
needs. That's why the rest of the plugins can stay focused on their domain instead of
re-solving environment setup: declare what you need, and the environment is there.

## Repository

<https://github.com/kitaekatt/plugins-kit>
