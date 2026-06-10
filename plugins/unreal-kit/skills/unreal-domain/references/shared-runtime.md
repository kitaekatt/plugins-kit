# Shared UE runtime substrate

The three unreal-kit skills (`ue-python-api`, `ue-mcp-server`, `fix-up-redirectors`) sit on one shared
runtime and split across **two execution channels**. This doc is the domain-level orientation; each member
skill carries its own deep procedures.

## Shared substrate (all three skills)

- **The unreal-kit venv** — bootstrap provisions `~/.claude/plugins/data/plugins-kit/unreal-kit/.venv`. Every
  skill's host-side Python runs there (host scripts re-exec into it via `bootstrap_guard.reexec_under_plugin_venv`).
  `bootstrap_guard.py` / `path_repair.py` are vendored byte-for-byte from `bootstrap_lib` (guard + PATH repair);
  `bootstrap.py` is unreal-kit's own UE-side dependency bootstrapper (not vendored — it installs packages into
  UE's embedded Python via unreal-pip).
- **UE project/engine discovery** — `lib/ue_discovery.py`, `lib/ue_env.py`, `lib/ue_ini.py` locate the project
  root, the engine install, and read `.ini` config. All three skills resolve "which UE / which project" through
  these.

## The two execution channels

| Channel | How | Editor must be… | Used by |
|---|---|---|---|
| **Host-side Python** (`ue_runner.py`) | Auto-detects whether the Editor is open. Editor open → **remote** (script over UDP multicast via `upyrc`). Editor closed → **commandlet** (headless UE process). | open *or* closed | `ue-python-api` (data read/write/extract), `fix-up-redirectors` (Phase-1 discovery) |
| **In-Editor MCP** (`lib/ue_mcp_client/`) | Talks to the MCP server running *inside* a live Editor. Imperative editor control — spawn actors, author graphs, drive PIE. | open, with the MCP server running | `ue-mcp-server` |

**Picking the channel = picking the skill:**
- Read / write / extract asset or property **data** → host-side Python → `ue-python-api`.
- **Drive the live Editor** (spawn, author graphs, PIE) → in-Editor MCP → `ue-mcp-server`.
- Clean up **ObjectRedirectors** in a P4-backed project → host-side Python + P4 → `fix-up-redirectors`.

## Invariants

- **Engine-generic.** unreal-kit is a public MIT plugin — no project-specific asset paths, class names, or
  workflows in any skill or this doc.
- **Vendored `path_repair.py`** in `lib/` is byte-identical to `bootstrap_lib`'s canonical; edits must mirror
  (see `lib/CLAUDE.md`).
