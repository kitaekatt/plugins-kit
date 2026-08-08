# Project Setup

## Automatic Setup (Bootstrap)

Setup happens automatically on every Claude Code session start via the bootstrap engine. No manual steps required.

The bootstrap engine (`bootstrap.json`) handles:

1. **Discovers `.uproject` and engine directory** — via `project_config` autodetect (walks up from CWD)
2. **Writes per-project config** — to `<project_root>/.local-data/plugins-kit/unreal-kit/config.yaml` (gitignored; legacy `.local-data/unreal-kit/config.yaml` and `.claude/unreal-kit.yaml` are auto-migrated to the new path on session start)
3. **Syncs config to data dir** — so `ue_runner.py` can resolve paths at runtime
4. **Enables `bRemoteExecution`** — in `Config/UserEngine.ini` (per-user, not checked in). Allows running scripts from terminal via UDP
5. **Enables `bIsDeveloperMode`** — in `Config/UserEngine.ini`. Enables UE to generate Python API stubs
6. **Downloads the stock API stub** -- from PyPI (`unreal-stub` package) into the machine-local plugin data directory
7. **Checks the durable enriched stub** -- records a deferred requirement when `<project>/.plugin-data/plugins-kit/unreal-kit/unreal.py` is absent or differs from the editor-generated source; bootstrap does not write durable project data

## After First Session

**Restart UE Editor** -- the `bRemoteExecution` and `bIsDeveloperMode` settings only take effect on editor startup. After Developer Mode is active, complete a full compile, then explicitly refresh the enriched stub with `python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root <project-root>`.

## Interactive Setup

If bootstrap can't auto-discover your project (e.g., CWD is not inside a UE project tree), use the interactive setup:

```bash
<skill-dir>/scripts/ue-runner.cmd --setup
```

This prompts for the `.uproject` path and configures everything interactively.

## How to Tell If Setup Is Needed

**Default assumption: setup is complete.** Only investigate if you encounter:

- Config validation errors from `ue_runner.py` (e.g., "uproject path not configured")
- `scripts/search_unreal_stub.py` reports that API search is unavailable
- Remote execution fails with "Editor not responding" and settings haven't been configured

## Troubleshooting

### `.uproject` Not Found

The autodetect walks up from CWD looking for `.uproject` files. If Claude Code was not launched from inside a UE project tree, autodetect can't discover the project. Use `scripts/ue-runner.cmd --setup` to configure manually.

### Stubs Download Failed

If PyPI is unreachable (firewall, no internet):

- Stubs are optional -- scripts will still run; only API search is unavailable.
- A new session retries the machine-local stock download.
- To create the durable enriched stub, enable Developer Mode, complete a full compile, and explicitly run `python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root <project-root>`.
