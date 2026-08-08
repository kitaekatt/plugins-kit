# unreal-kit

Claude runs Python inside Unreal -- editor open or not, auto-detected.

## What it does

The core is `ue_runner.py` (the `ue-python-api` skill): it runs a Python
script against your UE project from the terminal. If the Editor is open with
remote execution enabled, the script goes over UDP to the running Editor
(~2 seconds); if the Editor is closed, the runner falls back to a headless
commandlet that loads the project without the UI (slow, roughly 30-120s).
Scripts write results as YAML to `<Project>/Saved/PythonOutput/`, which the
runner picks up and returns.

That covers asset inspection, batch property edits, reference-graph walks,
DataTable and animation queries, and data extraction -- all scriptable and
CI-able, no human in the Editor required.

Running Python inside Unreal is not this plugin's invention: the in-editor
interpreter is Epic's own PythonScriptPlugin, and the UDP remote-execution
path is the third-party `upyrc` library. What unreal-kit ships is the
effective-use layer on top of them -- the settings bootstrap writes to turn
the API on (`bRemoteExecution`, Developer Mode), the `ue_runner`
orchestration (auto-detecting a running Editor for the ~2s UDP path vs a
headless commandlet when it is closed, re-execing under the plugin venv, and
returning results as YAML), and the encoded expertise (searchable API stubs and
the gotcha corpus below). It claims exactly that, not the Python bridge
itself.

## Secondary capabilities

- **ue-mcp-server** -- live Editor authoring via MCP: spawning actors,
  creating Blueprints, authoring material graphs, driving PIE, screenshots.
  Honest caveat: this skill *drives* an external Unreal MCP automation
  server; **this plugin does not ship that server**. You set it up
  separately (Node.js, the server source, a `.mcp.json` entry, and the
  Editor-side automation-bridge plugin) per the skill's prerequisites
  section.
- **fix-up-redirectors** -- ObjectRedirector cleanup for Perforce-backed UE
  projects.

## Gotchas already handled

The skill encodes the UE Python landmines so the agent avoids them: the
`unreal.Rotator` positional-argument order swap, cached actor bounds after
posing, `focus_actor` framing from bind-pose bounds, save traps on
Perforce-tracked assets, protected Slate struct fields, and more.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install unreal-kit
```

The `bootstrap` plugin is installed automatically as a dependency and
provisions unreal-kit on the first session start: the Python venv, host-side
deps (upyrc, pyyaml), API stubs, and per-project config. Silent when healthy.

## Prerequisites

- **A UE project and engine install.** You need a `.uproject` and a local
  engine. Bootstrap autodetects both by walking up from the directory Claude
  Code was launched in; if that fails, run
  `skills/ue-python-api/scripts/ue-runner.cmd --setup` to configure paths
  manually.
- **Remote execution ini flags** for the fast path. Bootstrap writes
  `bRemoteExecution=True` (and `bIsDeveloperMode=True` for stub generation)
  into `<Project>/Config/UserEngine.ini` -- per-user, not checked in. The
  Editor must be restarted once after these land. Without them, everything
  still works via the slower commandlet mode.
- **Developer Mode** (optional) -- with it enabled and after a full compile, UE
  generates an enriched project-specific Python stub. Bootstrap only checks
  whether the durable copy is absent or stale; refresh it explicitly from the
  consuming project root with
  `python ${CLAUDE_PLUGIN_ROOT}/scripts/refresh_unreal_stub.py --project-root .`.
  The command announces the destination before writing under
  `.plugin-data/plugins-kit/unreal-kit/`.
- **Platform:** developed and used on Windows. Bootstrap's venv layer is
  cross-platform, but parts of the UE-side tooling (unreal-pip's subprocess
  handling) are currently Windows-specific -- treat non-Windows as untested.

## When not to use it

- **Non-Python Editor work** -- C++ changes or hand-authoring Blueprint
  logic. The Python API can inspect Blueprints but this is not a Blueprint
  editing tool.
- **Projects where you cannot modify project settings** -- the fast remote
  path needs the `UserEngine.ini` flags above; if even a per-user ini edit
  is off-limits, you are restricted to commandlet mode at best.
