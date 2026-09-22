---
_schema_version: 1
name: ue-mcp-server
author: christina
skill-type: capability-skill
description: Use when automating the Unreal Editor via the MCP server -- spawning actors, authoring graphs, driving PIE. Do NOT use for on-disk asset inspection.
---

# Unreal MCP Server

Wraps the Unreal MCP Server so a Claude agent can drive the Editor in real time -- spawn actors, create Blueprints, author material/Blueprint graphs, build geometry, manage lighting, take screenshots, and drive PIE. Requires the Editor to be open with the MCP automation bridge plugin active.

> **This is a generic, public MIT-licensed plugin.** All patterns, examples, and documentation must be engine-generic. Do not add project-specific asset paths, class names, cheat commands, or workflows.

The fenced YAML block below is the load-bearing contract: external_capability declaration, layering manifest, capability surface (one record per MCP tool domain), capability-skill-level gotchas, and references index. Markdown sections after the YAML carry orientation prose and code recipes -- consult them after the YAML.

```yaml
capability_skill:
  _schema_version: "1"
  identity: Unreal MCP Server automation for driving the Unreal Editor in real time -- spawn and manipulate actors, author Blueprint and material graphs, manage assets, drive PIE, and inspect runtime state via MCP tool calls.
  scope:
    covers:
      - actor manipulation in open levels (spawn, transform, attach, find)
      - Blueprint, material, behavior tree, and Niagara graph authoring
      - asset CRUD (import, rename, move, delete, create materials)
      - level operations (load, save, sub-levels, World Partition)
      - editor controls (PIE drive, screenshots, console commands, camera)
      - runtime inspection of in-memory actors and components
    excludes:
      - reading or extracting on-disk asset data without the Editor open (use ue-python-api)
      - Blueprint logic-flow analysis from .uasset on disk (use a blueprint-details skill)
      - source-only C++ analysis
  external_capability:
    kind: mcp_server
    name: Unreal MCP Server
    description: MCP tool server exposing Unreal Editor automation surfaces. Each MCP tool corresponds to a domain (control_actor, manage_blueprint, manage_asset, etc.) and accepts an `action` parameter plus domain-specific arguments. The MCP server relays requests to an Editor plugin via TCP, which executes them and returns results. Requires the Editor to be open with the automation-bridge plugin active.
  layering:
    claude_md: []
    skill_md:
      - orientation prose -- what the skill is and when to reach for it vs ue-python-api
      - prerequisites -- Node.js, npm install, .mcp.json approval, Editor running with bridge plugin
      - tool selection guide -- the 30-domain table mapping work-target to MCP tool
      - capability surface -- one record per MCP tool domain
      - capability-skill-level gotchas -- save-after-mutate, validate-before-state-change, P4-tracked-asset save trap
      - basic usage patterns -- single tool call examples and the McpClient batch library
      - cross-cutting patterns -- inspect-before-modify, finding actors, console commands during PIE
    references:
      - tool-catalog.md -- complete tool catalog with all actions and parameter reference per domain
      - workflows.md -- multi-step recipes (create Blueprint from scratch, build a level, material setup)
  capabilities:
    - id: control_actor
      keywords: [actor, spawn, delete, transform, attach, find actor, level placement]
      user_objective: Spawn, delete, transform, attach, or find actors in the currently open level.
      operation: "MCP tool `control_actor` with `action: spawn|delete|transform|attach|find_by_tag|find_by_class|find_by_name|list`"
      tool: control_actor
      reference_section: tool-catalog.md
    - id: control_editor
      keywords: [PIE, play, stop, screenshot, console command, camera, editor controls]
      user_objective: Drive the Editor itself -- start/stop PIE, take screenshots, set camera, run console commands, save all.
      operation: "MCP tool `control_editor` with `action: play|stop_pie|screenshot|set_camera|console_command|save_all`"
      tool: control_editor
      reference_section: tool-catalog.md
      gotchas:
        - Prefer the MCP `screenshot` action over `unreal.AutomationLibrary.take_high_res_screenshot()`. The Python API call blocks after the first invocation in a single Python session -- the first call works, the second hangs the editor. Each MCP `screenshot` call is a fresh request to the editor process so the deadlock does not accumulate.
        - Prefer the MCP `screenshot` action over `unreal.KismetRenderingLibrary.export_render_target()`. The Python API call silently produces no file in the UE Python environment -- it returns success and writes nothing to disk.
        - Editor screenshots fail when PIE is running. Stop PIE before any batch screenshot run, or the screenshot completes silently with no file on disk.
        - Editor screenshots also fail after the editor loses foreground focus. Set `t.IdleWhenNotForeground 0` at the start of every batch run via `mcp.console_command("t.IdleWhenNotForeground 0")` so the editor keeps rendering when minimized.
    - id: manage_blueprint
      keywords: [blueprint, BP, create blueprint, add component, author graph, BP nodes]
      user_objective: Create Blueprints, add components, and author Blueprint graphs node-by-node.
      operation: "MCP tool `manage_blueprint` with `action: create|add_component|create_node|connect_pins|...`"
      tool: manage_blueprint
      reference_section: tool-catalog.md
      gotchas:
        - Blueprint event overrides for inherited BlueprintImplementableEvents are not supported -- add_event, K2Node_Event with eventType override, etc. all fail silently. Provide manual Editor instructions when overriding parent events.
    - id: manage_asset
      keywords: [asset, import, rename, move, delete, create material, list assets]
      user_objective: List, import, rename, move, delete assets; create material instances.
      operation: "MCP tool `manage_asset` with `action: list|import|rename|move|delete|create_material`"
      tool: manage_asset
      reference_section: tool-catalog.md
      gotchas:
        - On import, the source-file path is recorded as AssetImportData -- imports from `tmp/` or Downloads break Reimport for other developers. Move source into the project's tracked source folder before importing.
    - id: manage_level
      keywords: [level, map, load, save, create level, sub-level, World Partition]
      user_objective: Load, save, or create levels; manage sub-levels and World Partition.
      operation: "MCP tool `manage_level` with `action: load|save|create|...`"
      tool: manage_level
      reference_section: tool-catalog.md
    - id: manage_lighting
      keywords: [light, lighting, build lighting, shadows, GI, lightmap]
      user_objective: Spawn lights, build lighting, configure shadows and global illumination.
      operation: "MCP tool `manage_lighting` with `action: spawn_light|build|configure_shadows|...`"
      tool: manage_lighting
      reference_section: tool-catalog.md
    - id: manage_geometry
      keywords: [geometry, primitive, mesh, boolean, deformation, brush]
      user_objective: Create primitive geometry, apply boolean operations, deform meshes.
      operation: "MCP tool `manage_geometry` with `action: create_primitive|boolean|deform|...`"
      tool: manage_geometry
      reference_section: tool-catalog.md
    - id: manage_material_authoring
      keywords: [material, material graph, material instance, material function, shader]
      user_objective: Author material graphs, create instances and material functions.
      operation: "MCP tool `manage_material_authoring` with `action: create|add_node|connect|create_instance|...`"
      tool: manage_material_authoring
      reference_section: tool-catalog.md
    - id: manage_effect
      keywords: [Niagara, effect, particle, VFX, debug shape]
      user_objective: Spawn or create Niagara systems and debug shapes.
      operation: "MCP tool `manage_effect` with `action: spawn|create_system|debug_shape|...`"
      tool: manage_effect
      reference_section: tool-catalog.md
    - id: animation_physics
      keywords: [animation, anim BP, anim blueprint, montage, ragdoll, physics asset]
      user_objective: Create animation blueprints, play montages, configure ragdoll and physics.
      operation: "MCP tool `animation_physics` with `action: create_anim_bp|play_montage|ragdoll|...`"
      tool: animation_physics
      reference_section: tool-catalog.md
    - id: manage_sequence
      keywords: [sequencer, level sequence, track, keyframe, cinematic]
      user_objective: Create level sequences, add tracks, and place keyframes.
      operation: "MCP tool `manage_sequence` with `action: create|add_track|add_keyframe|...`"
      tool: manage_sequence
      reference_section: tool-catalog.md
    - id: inspect
      keywords: [inspect, get property, set property, list components, runtime state]
      user_objective: Inspect actor and object properties at runtime, get/set property values, list components.
      operation: "MCP tool `inspect` with `action: inspect_object|get_property|set_property|get_components|...`"
      tool: inspect
      reference_section: tool-catalog.md
    - id: manage_performance
      keywords: [performance, memory, profile, LOD, Nanite, optimization]
      user_objective: Run memory reports, profile builds, configure LODs and Nanite.
      operation: "MCP tool `manage_performance` with `action: memory_report|profile|configure_lod|...`"
      tool: manage_performance
      reference_section: tool-catalog.md
    - id: manage_audio
      keywords: [audio, sound, sound cue, play sound]
      user_objective: Configure sound cues and play sounds.
      operation: "MCP tool `manage_audio` with `action: create_cue|play_sound|...`"
      tool: manage_audio
      reference_section: tool-catalog.md
    - id: manage_navigation
      keywords: [navigation, NavMesh, AI navigation]
      user_objective: Configure the NavMesh.
      operation: "MCP tool `manage_navigation` with `action: configure|build|...`"
      tool: manage_navigation
      reference_section: tool-catalog.md
    - id: manage_skeleton
      keywords: [skeleton, bones, sockets, physics asset, morph target]
      user_objective: Manage skeleton bones, sockets, physics assets, and morph targets.
      operation: "MCP tool `manage_skeleton` with `action: add_bone|add_socket|configure_physics|...`"
      tool: manage_skeleton
      reference_section: tool-catalog.md
    - id: manage_texture
      keywords: [texture, procedural texture, compression, texture settings]
      user_objective: Generate procedural textures and configure compression.
      operation: "MCP tool `manage_texture` with `action: create_procedural|configure_compression|...`"
      tool: manage_texture
      reference_section: tool-catalog.md
    - id: manage_character
      keywords: [character, character blueprint, movement, character camera]
      user_objective: Configure character blueprints, movement components, and character cameras.
      operation: "MCP tool `manage_character` with `action: configure_movement|setup_camera|...`"
      tool: manage_character
      reference_section: tool-catalog.md
    - id: manage_combat
      keywords: [combat, weapon, projectile, damage type]
      user_objective: Configure weapons, projectiles, and damage types.
      operation: "MCP tool `manage_combat` with `action: create_weapon|create_projectile|...`"
      tool: manage_combat
      reference_section: tool-catalog.md
    - id: manage_input
      keywords: [input, input action, mapping context, enhanced input]
      user_objective: Configure Enhanced Input actions and mapping contexts.
      operation: "MCP tool `manage_input` with `action: create_action|create_mapping_context|...`"
      tool: manage_input
      reference_section: tool-catalog.md
    - id: manage_behavior_tree
      keywords: [behavior tree, BT, AI, behavior tree node]
      user_objective: Add and connect behavior tree nodes.
      operation: "MCP tool `manage_behavior_tree` with `action: add_node|connect|...`"
      tool: manage_behavior_tree
      reference_section: tool-catalog.md
    - id: manage_widget_authoring
      keywords: [widget, UMG, UI, layout panel, widget blueprint]
      user_objective: Create widget blueprints, layout panels, and UI elements.
      operation: "MCP tool `manage_widget_authoring` with `action: create|add_panel|add_element|...`"
      tool: manage_widget_authoring
      reference_section: tool-catalog.md
      gotchas:
        - Widget event overrides for inherited BlueprintImplementableEvents (e.g. BP_Render in a subclass) are not supported -- the override approaches fail silently. Provide manual Editor instructions instead.
    - id: manage_gas
      keywords: [GAS, gameplay ability system, ability, gameplay effect, attribute, gameplay cue]
      user_objective: Configure abilities, gameplay effects, attributes, and cues for the Gameplay Ability System.
      operation: "MCP tool `manage_gas` with `action: create_ability|create_effect|create_attribute|...`"
      tool: manage_gas
      reference_section: tool-catalog.md
    - id: manage_splines
      keywords: [spline, spline actor, spline component]
      user_objective: Create and manipulate spline actors and components.
      operation: "MCP tool `manage_splines` with `action: create|add_point|set_tangent|...`"
      tool: manage_splines
      reference_section: tool-catalog.md
    - id: manage_volumes
      keywords: [volume, blocking volume, trigger volume, post-process volume]
      user_objective: Place and configure volume actors (blocking, trigger, post-process, etc.).
      operation: "MCP tool `manage_volumes` with `action: create|configure|...`"
      tool: manage_volumes
      reference_section: tool-catalog.md
    - id: build_environment
      keywords: [landscape, foliage, procedural terrain, environment]
      user_objective: Build landscapes, paint foliage, and configure procedural terrain.
      operation: "MCP tool `build_environment` with `action: create_landscape|paint_foliage|configure_terrain|...`"
      tool: build_environment
      reference_section: tool-catalog.md
    - id: manage_game_framework
      keywords: [game mode, game state, player controller, pawn, game framework]
      user_objective: Configure game modes, game states, player controllers, and related framework classes.
      operation: "MCP tool `manage_game_framework` with `action: configure_game_mode|configure_player_controller|...`"
      tool: manage_game_framework
      reference_section: tool-catalog.md
    - id: manage_level_structure
      keywords: [sub-level, level streaming, persistent level]
      user_objective: Manage sub-levels and level streaming.
      operation: "MCP tool `manage_level_structure` with `action: add_sub_level|configure_streaming|...`"
      tool: manage_level_structure
      reference_section: tool-catalog.md
    - id: manage_networking
      keywords: [networking, replication, replicate, network role]
      user_objective: Configure replication on actors and components.
      operation: "MCP tool `manage_networking` with `action: configure_replication|...`"
      tool: manage_networking
      reference_section: tool-catalog.md
    - id: system_control
      keywords: [console command, project settings, HUD, automation tests, system]
      user_objective: Run console commands, configure project settings, manage HUD, and drive automation tests.
      operation: "MCP tool `system_control` with `action: console_command|project_setting|hud|run_test|...`"
      tool: system_control
      reference_section: tool-catalog.md
  gotchas:
    - Save after mutating assets. MCP tools operate on the Editor's in-memory object graph -- changes are not written to disk until you call `control_editor` -> `save_all`. After any creation or modification, call save_all and verify the .uasset exists on disk before proceeding. Read-only tools (inspect, control_editor, manage_performance, system_control) are exempt.
    - Source-control trap when assets are tracked. `save_all` writes over read-only files without source-control awareness. The file changes on disk but the source-control system does not see the file as opened, so a subsequent submit will not include the change. Open the file for edit in source control before the MCP mutation, or open + reopen retroactively after save_all.
    - Validate before changing editor state. Stopping PIE, restarting PIE, and loading levels are disruptive -- they reset player state, drop server connections, and lose in-progress work. Always query the current state first (inspect, find_by_class, or a project-side status helper) and only act if the state actually needs to change.
    - The MCP response always reports `success: true` when a console command is dispatched. It does NOT confirm the command actually executed on the server. Verify by observing the resulting game state.
    - Setup requires Node.js 18+ on PATH and an `npm ci` in the MCP server directory before the .mcp.json server can be approved. If the MCP server shows as failed in Claude Code, check `node --version` first; missing Node is the most common cause.
  references:
    - id: tool_catalog
      path: references/tool-catalog.md
      keywords: [all tools, all actions, parameter reference, what can it do, full list, tool catalog]
      summary: Action summary for the MCP tool domains and common actions. Consult the connected server for parameter details.
    - id: workflows
      path: references/workflows.md
      keywords: [recipe, step-by-step, create blueprint from scratch, build a level, material setup, multi-step workflow]
      summary: Multi-step recipes that combine multiple tool domains to accomplish common authoring tasks.
```

## When to Use This (vs. Python API)

Use the MCP server when you need to **do things** in the Editor:
- Create or modify content (actors, Blueprints, materials, geometry, levels)
- Manipulate a scene (transforms, attachments, visibility, lighting)
- Author graphs node-by-node (Blueprint, material, behavior tree, Niagara)
- Drive the Editor (play/stop PIE, screenshots, console commands)
- Manage asset files (import, rename, move, delete)

Use the Python API (`ue-python-api`) instead when you need to **read or query** project data without the Editor open (asset properties, reference graphs, bulk registry queries).

## Prerequisites

The Editor must be running with the MCP automation-bridge plugin active. If MCP server tools are failing, ask the user to open the Editor.

**First-time setup (one-time per machine):**
1. Install **Node.js 18+** and ensure `node` is on PATH.
2. Run `npm ci` in the MCP server source directory shipped with your project.
3. Restart your terminal, then restart Claude Code.
4. When prompted, **approve** the MCP server entry from the project's `.mcp.json`.
5. Open the Unreal Editor with the automation-bridge plugin enabled.

**If the MCP server shows as failed:** the project `.mcp.json` requires `node` on PATH. If `node --version` does not work in your terminal, Node.js is not installed or the terminal needs a restart.

## Precondition - bootstrap must have provisioned unreal-kit

Before invoking the plugin venv interpreter (`~/.claude/plugins/data/plugins-kit/unreal-kit/.venv/Scripts/python.exe`) to run any `ue_mcp_client` batch script, confirm `~/.claude/plugins/data/plugins-kit/unreal-kit/bootstrap.log` exists. If it does not, the venv interpreter path won't exist either (and `websocket-client`, which the MCP client imports, won't be installed) and the command fails opaquely. Tell the user "the bootstrap plugin hasn't provisioned unreal-kit -- install/enable plugins-kit:bootstrap and start a new session" and stop.

## How It Works

Each MCP tool corresponds to a domain (e.g. `control_actor`, `manage_blueprint`). You call the tool with an `action` parameter and domain-specific arguments. The MCP server relays the request to the Editor plugin via TCP, which executes it and returns results.

## Tool Selection Guide

Pick the tool based on **what you are working with**:

| Domain | MCP Tool | What it does |
|--------|----------|--------------|
| Actors in a level | `control_actor` | Spawn, delete, transform, attach, find actors |
| Editor controls | `control_editor` | Play/stop PIE, screenshots, camera, console commands |
| Blueprints | `manage_blueprint` | Create BPs, add components, author BP graphs |
| Assets | `manage_asset` | List, import, rename, delete, create materials |
| Levels | `manage_level` | Load, save, create levels; World Partition |
| Lighting | `manage_lighting` | Spawn lights, build lighting, configure shadows/GI |
| Geometry & mesh | `manage_geometry` | Create primitives, boolean ops, mesh deformation |
| Materials | `manage_material_authoring` | Full material graph authoring, instances, functions |
| Effects (Niagara) | `manage_effect` | Spawn/create Niagara systems, debug shapes |
| Animation | `animation_physics` | Create anim BPs, play montages, ragdoll |
| Sequencer | `manage_sequence` | Create sequences, add tracks, keyframes |
| Object inspection | `inspect` | Inspect properties, get/set values, list objects |
| Performance | `manage_performance` | Memory reports, profiling, LOD, Nanite config |
| Audio | `manage_audio` | Sound cues, play sounds |
| Navigation | `manage_navigation` | NavMesh configuration |
| Skeletons | `manage_skeleton` | Bones, sockets, physics assets, morph targets |
| Textures | `manage_texture` | Procedural textures, compression settings |
| Characters | `manage_character` | Character blueprints, movement, camera setup |
| Combat | `manage_combat` | Weapons, projectiles, damage types |
| Input | `manage_input` | Input actions, mapping contexts |
| Behavior Trees | `manage_behavior_tree` | Add/connect BT nodes |
| Widgets (UMG) | `manage_widget_authoring` | Create widgets, layout panels, UI elements |
| GAS | `manage_gas` | Abilities, effects, attributes, cues |
| Splines | `manage_splines` | Spline actors and manipulation |
| Volumes | `manage_volumes` | Volume actors (blocking, trigger, etc.) |
| Environment | `build_environment` | Landscapes, foliage, procedural terrain |
| Game framework | `manage_game_framework` | Game modes, game states, player controllers |
| Level structure | `manage_level_structure` | Sub-levels, level streaming |
| Networking | `manage_networking` | Replication setup |
| System | `system_control` | Console commands, project settings, HUD, tests |

## Basic Usage Pattern

Every tool call follows the same shape: tool name + `action` + parameters.

```
Tool: control_actor
action: spawn
classPath: PointLight
actorName: MyLight
location: { x: 0, y: 0, z: 200 }
```

```
Tool: manage_blueprint
action: create
name: BP_MyActor
parentClass: Actor
folder: /Game/Blueprints
```

```
Tool: control_editor
action: screenshot
filename: my_screenshot
```

## Cross-cutting Patterns

### Save After Mutating Assets

MCP tools operate on the Editor's in-memory object graph. Changes are not written to disk until you explicitly save. After any tool call that creates or modifies an asset:

1. Call `control_editor` -> `save_all`.
2. Verify the `.uasset` exists on disk (file check).

Only proceed with further operations on the asset after both steps succeed. If the file does not exist on disk, the creation failed silently.

**Exempt tools** (read-only or transient -- no save needed): `inspect`, `control_editor`, `manage_performance`, `system_control`.

**Source-control trap:** when assets are tracked in a source-control system (Perforce, Git+LFS, etc.), `save_all` writes over read-only `.uasset` files without source-control awareness. The file changes on disk but the source-control system does not see the file as opened, and a subsequent submit will not include the change. Open the file for edit in source control before the MCP mutation, or open + reopen retroactively after `save_all`. Verify with the source-control "opened" command before declaring the edit complete.

### Validate Before Changing Editor State

Never change editor state without first checking whether the change is needed. MCP actions like stopping PIE, restarting PIE, or loading levels are disruptive -- they reset player state, drop server connections, and lose in-progress work. Always query the current state first and only act if the state actually needs to change.

**Anti-pattern:** Blindly restarting PIE to "reset" something.

```
# BAD -- restarts PIE without checking if it is already running
control_editor -> stop_pie
control_editor -> play

# GOOD -- check first, only restart if needed
inspect -> find_by_class with the world or PIE controller
# only stop/start if the inspection shows a problem
```

**Anti-pattern:** Stopping PIE to run a console command that works during PIE.

```
# BAD -- stops PIE, losing the session, just to run a command
control_editor -> stop_pie
control_editor -> play
control_editor -> console_command: SomeCommand

# GOOD -- run the command in the running session
control_editor -> console_command: SomeCommand
```

**Anti-pattern:** Asking the user to confirm state you can check yourself.

```
# BAD -- asking the user if the editor is closed
"Can you close the editor so I can run the setup?"

# GOOD -- check, then act or wait
inspect or a project-side status helper
# if running, tell the user it needs to be closed
# if closed, proceed immediately
```

**General rule:** treat every state-changing MCP call as potentially destructive. Query first (`inspect`, `find_by_class`, project-side status helpers), then act only on what the query tells you.

### Inspect Before Modify

Use `inspect` -> `inspect_object` to see an actor's properties before modifying them, and `get_components` to see what components it has.

### Console Commands and Cheats During PIE

Use `control_editor` with `action: console_command` to run any console command. When PIE is active, commands route through the PIE PlayerController, so `Exec` UFUNCTIONs on the cheat manager (or any other PlayerController exec function) work.

```
Tool: control_editor
action: console_command
command: <YourConsoleCommand> [args]
```

The MCP response always reports `success: true` if the command was dispatched. It does NOT confirm the command actually executed on the server. Verify by observing the resulting game state.

## Python Client Library (for scripted batch operations)

When you need to send MCP commands from a Python script (batch processing, automation), use the `ue_mcp_client` library instead of making individual MCP tool calls.

**API:**
```python
from ue_mcp_client import McpClient, McpError

with McpClient() as mcp:
    # Generic request: tool name + payload dict
    mcp.send_request("control_editor", {"action": "set_camera", "location": {...}, "rotation": {...}})
    mcp.send_request("control_actor", {"action": "spawn", "classPath": "PointLight", ...})

    # Convenience methods
    mcp.console_command("t.IdleWhenNotForeground 0")
    mcp.screenshot("my_screenshot")          # saves to <Project>/Saved/Screenshots/
    mcp.spawn("PointLight", actor_name="MyLight", location={"x":0,"y":0,"z":200})
    mcp.find_by_class("PointLight")
    mcp.inspect_object("/Game/Maps/Level.Level:PersistentLevel.Actor_0")
    mcp.save_all()
    mcp.batch_console_commands(["cmd1", "cmd2"])
```

Connection and request budgets are separate. The connection budget covers
the WebSocket connect, `bridge_hello`/`bridge_ack` handshake, and cleanup;
each request has its own action budget and a total cap that includes progress
extensions. The defaults are 5 seconds for connection, 30 seconds per action,
and 300 seconds as the total request cap. For a longer action, configure both
request values together before dispatching:

```python
with McpClient(timeout_s=120, request_cap_s=600) as mcp:
    mcp.save_all()
```

The cap must be at least as large as the action budget. Batch commands keep
the action budget per command; they do not turn the whole batch into one
request.

**When to use this vs MCP tool calls:**
- **MCP tool calls**: interactive work, one-off operations, when Claude is directly controlling the editor.
- **Python McpClient**: batch operations (processing 100+ items), scripts that run unattended, when you need a loop with conditional logic.

## Actor Class Aliases

`control_actor` -> `spawn` supports friendly aliases for common classes:
- `PointLight`, `SpotLight`, `DirectionalLight`, `RectLight` -> light actors
- `Camera` -> CineCameraActor
- `SplineActor` -> Actor with SplineComponent auto-added

## Finding Actors

Multiple search strategies via `control_actor`:
- `find_by_tag` -- find actors with a specific tag
- `find_by_class` -- find actors of a class
- `find_by_name` -- find actors by name pattern
- `list` -- list all actors in the level

## Known Limitations

**Blueprint event overrides are not supported.** `manage_blueprint` and `manage_widget_authoring` cannot override inherited `BlueprintImplementableEvent`s from a parent class (e.g. overriding `BP_Render` in a widget subclass). The `add_event`, `create_node` with `K2Node_Event`, and `eventType: "override"` approaches all fail silently. When a task requires overriding a parent event, skip MCP attempts and provide manual Editor instructions immediately.
