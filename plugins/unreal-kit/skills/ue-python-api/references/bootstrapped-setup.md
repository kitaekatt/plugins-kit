# Bootstrapped Setup

The bootstrap plugin automatically handles all setup on session start. This document describes what is configured and how to troubleshoot if something breaks.

## What Bootstrap Configures

| Item | What | Where |
|------|------|-------|
| Project config | `.uproject` path and engine directory | `<project_root>/.local-data/plugins-kit/unreal-kit/config.yaml` (per-project; bootstrap auto-migrates from legacy `.local-data/unreal-kit/config.yaml` / `.claude/unreal-kit.yaml`) |
| Remote execution | `bRemoteExecution=True` | `<Project>/Config/UserEngine.ini` |
| Developer mode | `bIsDeveloperMode=True` | `<Project>/Config/UserEngine.ini` |
| Host Python deps | `upyrc`, `pyyaml` | Plugin venv (managed by bootstrap) |
| Stock API stub | Generic `unreal.py` from PyPI | `~/.claude/plugins/data/plugins-kit/unreal-kit/stubs/unreal.py` (machine-local) |
| Enriched API stub check | Read-only presence/freshness check | `<project>/.plugin-data/plugins-kit/unreal-kit/unreal.py` (durable project data) |

## Troubleshooting

These issues should be rare since bootstrap runs automatically. Check if something went wrong during session startup.

### Config resolution order

`lib/ue_runner_config.py::load_config` resolves config in this order: explicit
`config_path` (isolates: replaces the global and project layers) > per-project
config (`<project_root>/.local-data/plugins-kit/unreal-kit/config.yaml`; legacy
`.local-data/unreal-kit/config.yaml` and `.claude/unreal-kit.yaml` are still
read) > global config (`~/.claude/plugins/data/plugins-kit/unreal-kit/config.yaml`) >
skill config (`ue_runner_config.yaml`) > shipped defaults (`defaults/config.yaml`) >
hardcoded defaults.

### Config not found

If `ue_runner.py` reports "uproject path not configured":
- Bootstrap may have failed to auto-detect the project. Check bootstrap output at session start.
- Run `"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" "${CLAUDE_PLUGIN_ROOT}/skills/ue-python-api/scripts/ue_runner.py" --setup` to interactively pick the `.uproject` and write the per-project config. That is `--setup`'s only job -- ini settings and host deps stay bootstrap's (it does not duplicate the rows above).
- Or manually create `<project_root>/.local-data/plugins-kit/unreal-kit/config.yaml` with `uproject` and `engine_dir` fields. (The legacy `.local-data/unreal-kit/config.yaml` and `.claude/unreal-kit.yaml` paths are still read if present, but create new files at `.local-data/plugins-kit/unreal-kit/config.yaml`.)
- Global config: `~/.claude/plugins/data/plugins-kit/unreal-kit/config.yaml` is deep-merged beneath any per-project config (see Config resolution order above), so fields it sets apply unless the project config overrides them.

### Remote execution not working

If remote execution fails with "Editor not responding":
- Verify `bRemoteExecution=True` is set in `<Project>/Config/UserEngine.ini`
- The Editor must be restarted after ini changes take effect
- Commandlet fallback will be used automatically -- no action needed

### Stubs missing

Run `"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" ${CLAUDE_PLUGIN_ROOT}/scripts/search_unreal_stub.py "<pattern>" --project-root <project-root>`. It prefers the durable enriched stub, then the machine-local stock stub.

If neither exists:
- API search says plainly that it is unavailable; scripts still run.
- Start a new Claude Code session to let bootstrap retry the stock PyPI download. Check network/firewall issues if it remains missing.
- For the enriched stub, enable Developer Mode, complete a full compile, then run `"${BOOTSTRAP_PROJECT_PYTHON:-${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}}" ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root <project-root>`. This explicit action announces and writes the durable destination; bootstrap never writes it.
