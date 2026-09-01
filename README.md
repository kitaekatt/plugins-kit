# plugins-kit

[![tests](https://github.com/kitaekatt/plugins-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/kitaekatt/plugins-kit/actions/workflows/tests.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/kitaekatt/plugins-kit)

A marketplace of plugins for [Claude Code](https://code.claude.com), built on a
shared dependency-provisioning engine. Each plugin extends Claude Code with
skills, slash commands, and hooks; a foundation plugin, **bootstrap**, provisions
the Python environments, system tools, and configuration every other plugin
needs, silently, at session start.

The plugins are the deliverable; the marketplace is the shared home and
dependency substrate they ride on. Browse the full catalog on the
[marketplace landing page](https://kitaekatt.github.io/plugins-kit/).

## Architecture

The marketplace follows a foundation-and-extension model. One plugin,
**bootstrap**, is the dependency-and-provisioning engine; every plugin that
ships Python or external tools declares what it needs and rides on it. This is
what lets these plugins ship real scripts, not just prompts: you cannot assume
anything about a user's machine, so without a provisioning layer a plugin with
Python tooling breaks on the first machine that lacks it.

At session start a `SessionStart` hook runs fast skip-gate checks, then
dispatches the engine to a background subshell so the session is never blocked;
results are reported on the next prompt via a `UserPromptSubmit` hook. The
engine deep-merges every enabled plugin's `bootstrap.json` manifest into one
provisioning pass and brings three categories into the declared state:

- **Tools** -- system binaries (`uv`, `git`, `gh`, `jq`), installed to
  `~/.local/bin` with no admin rights and only User-scope PATH changes.
- **Venvs** -- a per-plugin Python virtualenv under
  `~/.claude/plugins/data/<marketplace>/<plugin>/.venv/`, so plugins never share
  or pollute a global interpreter.
- **Shared libs** -- Python modules exported to peer plugins via `.pth` files,
  so common code (for example the multi-agent code-review library both p4-kit
  and git-kit build on) lives once and is imported, not duplicated.

Checks that fail and can be fixed automatically are; those needing your
authorization are queued and surfaced as a single "fix-all" prompt. Plugin
locations resolve from `installed_plugins.json`, with a cache-scan fallback for
newer Claude Code registries that leave that file empty. Platform differences
(Windows vs. Unix) are handled centrally.

Healthy bootstrap is silent: no output at session start means every check
passed. To confirm a plugin was provisioned, read its log at
`~/.claude/plugins/data/plugins-kit/<plugin>/bootstrap.log`.

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

> Prerequisite: `bash` on your PATH. On Windows that means Git Bash.

## Plugins

The marketplace ships the following plugins. bootstrap is the foundation; the
rest declare it as a dependency, so installing any of them pulls it in.

| Plugin | Category | What it does |
|--------|----------|--------------|
| **bootstrap** | Foundation | Dependency/provisioning engine -- tools, venvs, git deps, marketplaces, and config from a `bootstrap.json`. |
| **p4-kit** | Code review | Multi-agent review of pending Perforce changelists (`/p4-code-review`). |
| **git-kit** | Code review | Git + GitHub multi-agent code review (`/git-code-review`) plus `gh` bootstrap. |
| **skills-kit** | Authoring | Authoring/auditing skills and `CLAUDE.md` -- verb x artifact matrix (`/md-domain`). |
| **unreal-kit** | Automation | Unreal Engine automation -- Python asset API, MCP editor control, redirector cleanup. |
| **hue-kit** | Automation | Philips Hue layered-scene framework -- bridge sync, YAML scenes, meta-group solver. |
| **awesome-kit** | Utility | Cross-domain skills -- communication framework, `/plugin-ecosystem`, background-agent orchestration, task tracking. |
| **pdf-kit** | Utility | Convert an HTML file to a PDF via headless Chromium (`/html-pdf`). |
| **llm-scripting-kit** | Utility | LLM key resolution, shared model registry, and named OpenAI-compatible endpoints (OpenRouter is the default). |
| **claude-ui-kit** | Utility | Status line with context-window and rate-limit threshold colors, plus `/statusline`. |
| **cache-kit** | Utility | Cache-usage reporting from transcripts -- per-request and session-level hit analysis. |
| **bootstrap-stuck-fix** | Maintenance | Temporary shim repairing a wedged bootstrap registry record. |
| **prototypes** | Incubation | Experimental skills awaiting graduation into their own plugins. |

Three of these form one authoring -> auditing -> review path over the same
standards: **skills-kit** authors and audits the `CLAUDE.md` and `SKILL.md`
files that encode a project's conventions, and **git-kit** and **p4-kit**
review each change against those same `CLAUDE.md` files (quoting the rule
verbatim before flagging a violation). Authoring establishes the standard,
auditing brings a whole file into compliance, and review keeps every change
compliant.

## Testing and CI

Each plugin has a matching test directory under `tests/` (mirroring the plugin
layout), alongside repo-script tests that guard the publish and manifest
tooling. The suite runs in GitHub Actions on every push (the badge above). Run a
targeted subset with:

    uv run --extra dev pytest tests/bootstrap/ -v

## Security

These plugins run trusted code on your machine with your user privileges.
[`SECURITY.md`](SECURITY.md) inventories exactly what each plugin -- bootstrap
especially -- reads, writes, downloads, and executes, including the User-scope
(never system/admin) PATH changes bootstrap makes, so you can decide whether to
trust them before installing.

## Repository

<https://github.com/kitaekatt/plugins-kit>
