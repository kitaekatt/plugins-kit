---
_schema_version: 1
name: claude-explorer
author: christina
skill-type: technique-skill
description: Use when generating or browsing the claude-explorer view -- an HTML view of ~/.claude/ + the current project. Do NOT use for audits or skill authoring.
disable-model-invocation: true
user-invocable: true
argument-hint: "[crawl | serve | run] [--project PATH] [--port N] [--no-open]"
---

# Claude Explorer

Browsable HTML view of the user's Claude filesystem: `~/.claude/` plus the current project (CWD). Containers (marketplaces, plugins, skills, reference docs, scripts, CLAUDE.md) open in place to show their contents. Leaf primitives (md, json, scripts) deep-render inline on click. Omarchy-style aesthetic (dark Catppuccin Mocha, monospace, keyboard-first). Read-only v1.

## Architecture

Two-phase, decoupled:

1. **Crawl** -- a Python pass walks the roots, applies deterministic summary projections per primitive / composition, and writes a single index JSON. Idempotent; safe to re-run. Future: LLM summaries via `claude -p --model haiku-4-5` cached by content hash for fields that benefit from understanding (this hook is reserved; not wired in v1).
2. **Serve** -- a local HTTP server (`127.0.0.1:8923` by default, stdlib `http.server`) serves the SPA, the index JSON, and a path-guarded `/file?path=...` endpoint that fetches source on demand. Browser-side TypeScript / vanilla JS (inline in the served HTML; no build step) consumes the index and lazy-loads file contents when the user deep-renders a primitive.

The refresh button in the header triggers `GET /refresh`, which re-runs the crawl synchronously and the page reloads. The crawl is also auto-invoked when the skill is run via the default `run` subcommand.

## Framework

This skill operationalizes the **claude_explorer** viewer-kind under the shared audit framework. The shared glossary (`subject`, `primitive`, `composition`, `discovery`, `audit-kind`, `viewer-kind`, `scaffolding`, `summary projection`) is canonical at `plugins/skills-kit/skills/md-audit/references/audit-framework.md`, with the data model at `audit-framework.yaml`.

In framework terms:

- **Subject:** two roots -- the **claude-user-dir** at `~/.claude/` and the **project** at CWD.
- **Subject_type:** multi-root single-pass.
- **Discovery:** marker-driven; activates `marketplace` rules at `.claude-plugin/marketplace.json`, `plugin` at `.claude-plugin/plugin.json`, `skill` at `SKILL.md`, falls back to `directory`.
- **Compositions traversed:** `claude_user_dir`, `project`, `marketplace`, `plugin`, `skill`, `directory`.
- **Primitives consumed:** `skill_md`, `claude_md`, `reference_doc`, `plain_md`, `plugin_manifest`, `marketplace_manifest`, `bootstrap_manifest`, `script`, plus opaque `json` / `yaml` leaves.
- **Viewer scaffolding:** `scripts/claude_explorer.py` (single self-contained Python script: crawl + serve + embedded SPA).

## References (load on demand)

- `references/projections.md` -- per-container summary projections, per-primitive summary + deep renderers, markdown-rendering rules, layered-personalization tables. Load when implementing or extending the generator's rendering rules.
- `references/interactivity.md` -- Omarchy-style aesthetic notes, the planned action-menu shape, file-queue + UserPromptSubmit drain protocol for v2 invoke-from-browser. Load when designing UI or implementing the interaction layer.

## Invocation

Use prototypes' own bootstrap-provisioned venv (stable path, resolves from any cwd):

```bash
# macOS / Linux
~/.claude/plugins/data/plugins-kit/prototypes/.venv/bin/python \
  "${CLAUDE_PLUGIN_ROOT}/skills/claude-explorer/scripts/claude_explorer.py" run

# Windows
~/.claude/plugins/data/plugins-kit/prototypes/.venv/Scripts/python.exe \
  "${CLAUDE_PLUGIN_ROOT}/skills/claude-explorer/scripts/claude_explorer.py" run
```

Degraded mode: the script is stdlib-only except for PyYAML, and falls back to regex frontmatter parsing when `yaml` is missing -- so if the prototypes venv has not been provisioned yet, plain `python3` (or `uv run python`) still works with slightly coarser YAML summaries.

Subcommands:

- `run` (default) -- crawl, then serve. Opens the browser unless `--no-open`.
- `crawl` -- one-shot crawl; writes the index and exits. Useful for cache warm-up.
- `serve` -- start the HTTP server without re-crawling. Use when the index is already current.

Flags:

- `--project PATH` -- project root (defaults to CWD).
- `--port N` -- port to bind on `127.0.0.1` (default 8923).
- `--no-open` -- don't auto-open the browser.

Cache and output paths:

- Index JSON: `~/.claude/.local-data/prototypes/claude-explorer/index.json`.
- Per-source cache (reserved for LLM summaries): `~/.claude/.local-data/prototypes/claude-explorer/cache/`.

## Security

The `/file?path=...` endpoint guards against path traversal: requested paths must resolve under `~/.claude/` or the project root (the server's `allowed_roots`, bound per server instance). The server binds to `127.0.0.1` only and rejects any request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding guard -- without it a hostile web page could point its own hostname at 127.0.0.1 and read `~/.claude/` same-origin). Files over 5 MB are rejected. Run only on a trusted local machine.

### Graduation gates

Security properties that must hold (and stay tested in `tests/prototypes/`) before this skill graduates to its own plugin: 127.0.0.1-only bind; Host-header allowlist; path-traversal guard on `/file`; allowed roots scoped to the server instance (never module-global accumulation); no new endpoints that write to disk without a fresh security review.

## Workflow

Run as a single response.

1. Resolve project root (`--project` or CWD).
2. Pick the interpreter: use the prototypes venv (`~/.claude/plugins/data/plugins-kit/prototypes/.venv/bin/python`, `Scripts\python.exe` on Windows) when it exists; otherwise fall back to `uv run python` (degraded mode -- regex frontmatter parsing, see Invocation). Then run `claude_explorer.py run` (crawl + serve). The server blocks; the agent should invoke it via `run_in_background` so control returns to the user once the browser opens.
3. Report the URL (`http://127.0.0.1:8923/`), the index location, and one-line counts (roots, compositions, primitives).

## Anti-patterns

- **Loading source into the summary projection.** Summaries are short; source belongs behind a click in the deep renderer.
- **Putting plugin-author overrides in the operator config.** Per-plugin display copy belongs in that plugin's `claude-explorer.yaml`, not the user-level config.
- **Running the server unbound.** Always bind `127.0.0.1`; never `0.0.0.0`. The `/file` endpoint reads local files.
- **Skipping the path-traversal guard.** Any extension to the file endpoint must preserve the `allowed_roots` containment check and the Host-header allowlist.

## Contract

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: user-only
  identity: "Render a self-contained HTML browser of the user's Claude filesystem (~/.claude/ + the current project) via a Python crawl that produces a JSON index plus a local HTTP server that serves the embedded SPA and a path-guarded file-fetch endpoint."
  references:
    - "skills-kit:md-audit/references/audit-framework.md (canonical glossary)"
    - "skills-kit:md-audit/references/audit-framework.yaml (audit-kind + viewer-kind registry)"
    - "references/projections.md (per-node summary + deep-render rules)"
    - "references/interactivity.md (aesthetic + planned action layer)"
  scope:
    covers:
      - "crawling ~/.claude/ + project, applying deterministic per-primitive summary projections, and writing a single JSON index"
      - "serving a local HTTP SPA bound to 127.0.0.1 that renders the index and lazy-loads file contents on deep-render click"
      - "a refresh button that triggers a re-crawl and reloads the page"
      - "deep-rendering md to HTML, json to key-value table, scripts inside <pre>"
    excludes:
      - "auditing skills or references (use /skill-audit or /references-audit)"
      - "skill authoring (use skills-kit:skill-authoring)"
      - "the plugin-ecosystem poster (use awesome-kit:plugin-ecosystem -- different reading task at marketplace-corpus scope)"
      - "invocation of actions from the browser (planned for v2; see references/interactivity.md)"
  techniques:
    - id: run_claude_explorer
      name: Run the claude-explorer browser
      keywords: [claude explorer, claude filesystem, html browser, hierarchical viewer, openable containers, omarchy aesthetic, refresh button]
      goal: "Start the local server, crawl the filesystem, render the SPA, and open the browser. The user browses; clicks open containers and deep-render leaves."
      preconditions:
        - "~/.claude/ exists (the user has run Claude Code at least once)."
        - "Python available; PyYAML via prototypes' own venv (optional -- falls back to regex frontmatter parsing without it)."
      steps:
        - n: 1
          action: "Resolve project root (--project PATH, defaults to CWD)."
        - n: 2
          action: "Pick the interpreter: prototypes' own venv python (~/.claude/plugins/data/plugins-kit/prototypes/.venv/bin/python; Scripts/python.exe on Windows) when provisioned, else `uv run python` in degraded mode. Invoke claude_explorer.py run via run_in_background. The script crawls, then binds 127.0.0.1:8923, then auto-opens the browser."
        - n: 3
          action: "Report URL + index path + counts back to the user. Hand control to them."
      gotchas:
        - "The server is blocking; the agent must run it in the background or the agent's turn will not return."
        - "/file?path=... is path-traversal guarded and every request is Host-header checked; any extension must preserve the allowed_roots containment check and the host allowlist."
        - "Refresh re-runs crawl synchronously; large filesystems take a few seconds."
        - "LLM summaries via claude -p are reserved but not wired in v1. Deterministic projections only."
  anti_patterns:
    - id: source_dumping
      name: Dumping primitive source into the summary projection
      keywords: [full source, no projection, html bloat]
      why_it_seems_right: "More detail in the summary saves a click."
      why_it_is_wrong: "Summaries get unnavigable. The deep renderer behind a click is the right place for source; the summary stays short."
      alternative: "Each summary projection is short (one frontmatter block or one heading + first lines). Deep renderer surfaces source on explicit click and stays inline."
    - id: server_bound_externally
      name: Binding the server to 0.0.0.0 or a public interface
      keywords: [server binding, network exposure, security]
      why_it_seems_right: "Binding to 0.0.0.0 lets the user view from another device on the network."
      why_it_is_wrong: "The /file endpoint reads local files. Exposing the server beyond localhost is a local-file-disclosure vector. Path-traversal guard helps but is not a substitute for binding scope."
      alternative: "Always bind 127.0.0.1. For multi-device access, the user tunnels (ssh -L) explicitly."
```

## Cross-references

- Sibling viewer: `awesome-kit:plugin-ecosystem` (marketplace-corpus poster; shallower drill, wider corpus).
- Shared substrate: `skills-kit:md-audit/references/audit-framework.md` and `audit-framework.yaml`.
- Interactivity research (background-agent report): `tmp/content-explorer-interactive-research.md`.
