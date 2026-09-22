# MCP Tool Catalog

This document is a concise action summary for the Unreal MCP Server domains
that this skill routes to. It is not an authoritative schema. The connected
server's advertised schema and the versioned server source define accepted
parameters and response fields.

This is a reference companion to `SKILL.md`. The capability surface lists tool
domains and links here for action summaries. Use the connected server schema
or versioned server source for parameter and response details; unsupported
calls must be refused visibly.

## How to read this file

Each section below covers one MCP tool domain and names common actions. The
summaries do not claim to list every action or parameter.

When this file is silent on a parameter, the canonical answer is the MCP server source -- request the user open the relevant Editor source if needed.

## control_actor

Spawn, delete, transform, attach, and find actors in the currently open level.

Common actions:
- `spawn` -- spawn an actor of the given class at a location/rotation.
- `delete` -- delete an actor by reference.
- `transform` -- set actor location, rotation, or scale.
- `attach` -- parent one actor to another.
- `find_by_tag` / `find_by_class` / `find_by_name` -- query actors in the level.
- `list` -- list all actors in the level.

## control_editor

Drive the Editor itself: PIE control, screenshots, camera, console commands, save_all.

Common actions:
- `play` -- start PIE.
- `stop_pie` -- stop PIE.
- `screenshot` -- take an Editor screenshot. (PIE must be stopped, see SKILL.md gotchas.)
- `set_camera` -- position the editor viewport camera.
- `console_command` -- run a console command (routes through the PIE PlayerController when PIE is active).
- `save_all` -- write all dirty assets to disk.

## manage_blueprint

Create Blueprints, add components, and author Blueprint graphs node-by-node.

Common actions:
- `create` -- create a new Blueprint asset.
- `add_component` -- add a component to a Blueprint.
- `create_node` -- add a node to a Blueprint graph.
- `connect_pins` -- wire two pins together.

Known limitation: cannot override inherited BlueprintImplementableEvents.

## manage_asset

List, import, rename, move, delete assets; create material instances.

Common actions:
- `list` / `import` / `rename` / `move` / `delete`
- `create_material` -- create a new material asset.

Source-import gotcha: AssetImportData records the source path used at import time; importing from `tmp/` or Downloads breaks Reimport for other developers.

## manage_level

Load, save, create levels; manage sub-levels and World Partition.

## manage_lighting

Spawn lights, build lighting, configure shadows and global illumination.

## manage_geometry

Create primitives, apply boolean operations, deform meshes.

## manage_material_authoring

Author material graphs, create instances and material functions.

## manage_effect

Spawn or create Niagara systems and debug shapes.

## animation_physics

Create animation blueprints, play montages, configure ragdoll and physics.

## manage_sequence

Create level sequences, add tracks, place keyframes.

## inspect

Inspect actor and object properties at runtime.

Common actions:
- `inspect_object` -- get all readable properties of an object by full path.
- `get_property` / `set_property` -- targeted property R/W.
- `get_components` -- list components on an actor.

## manage_performance

Memory reports, profiling, LOD configuration, Nanite settings.

## manage_audio

Sound cues, play sounds.

## manage_navigation

NavMesh configuration.

## manage_skeleton

Bones, sockets, physics assets, morph targets.

## manage_texture

Procedural textures, compression settings.

## manage_character

Character blueprints, movement, character cameras.

## manage_combat

Weapons, projectiles, damage types.

## manage_input

Enhanced Input actions and mapping contexts.

## manage_behavior_tree

Add and connect behavior tree nodes.

## manage_widget_authoring

Create widget blueprints, layout panels, UI elements.

Known limitation: cannot override inherited BlueprintImplementableEvents.

## manage_gas

Abilities, gameplay effects, attributes, cues.

## manage_splines

Spline actors and manipulation.

## manage_volumes

Volume actors (blocking, trigger, post-process, etc.).

## build_environment

Landscapes, foliage, procedural terrain.

## manage_game_framework

Game modes, game states, player controllers.

## manage_level_structure

Sub-levels, level streaming.

## manage_networking

Replication setup.

## system_control

Console commands, project settings, HUD, automation tests.
