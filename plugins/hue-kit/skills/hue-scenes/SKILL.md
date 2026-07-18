---
_schema_version: 1
name: hue-scenes
author: christina
skill-type: technique-skill
description: Use when reading, analysing, authoring, or syncing Philips Hue scenes with a bridge via the hue-kit CLI -- the layered (painter's-algorithm) scene model. Do NOT use for non-Hue lighting, or for activating/triggering scenes at runtime.
---

# Hue Scenes (layered model)

Read, analyse, author, and sync Philips Hue **scenes** with a bridge using the
`hue-kit` CLI. A scene is modelled as a default of OFF plus an ordered stack of
**layers**, each painting one **meta-group** (a named light-subset) a colour +
brightness; the topmost layer covering a light wins. A solver computes the
smallest group vocabulary that expresses every scene (a certified minimum). The
YAML config is the source of truth; the bridge is written only on `apply --yes`.

## Technique

The load-bearing contract; the markdown below is reference detail.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Read, analyse, author, and bi-directionally sync Philips Hue scenes with a bridge via the hue-kit CLI, using the layered meta-group model.
  scope:
    covers:
      - reading live scenes from the bridge and solving the minimal meta-group vocabulary
      - materialising the current bridge configuration to editable YAML
      - authoring scene look/colour/brightness changes as YAML layer edits
      - writing YAML scene definitions back to the bridge (definition-only; nothing actuates)
      - rendering the self-contained HTML report
    excludes:
      - activating / triggering / turning on scenes or lights at runtime (this edits DEFINITIONS)
      - non-Hue lighting ecosystems (LIFX, Nanoleaf, etc.)
      - inspecting or modifying the bootstrap engine
  techniques:
    - id: setup-on-bridge
      name: Set up the framework on a new bridge
      keywords: [new bridge, first time, set up hue, bootstrap scenes, minimal groups, meta-groups]
      goal: Go from a fresh bridge to editable YAML + a rendered report.
      steps:
        - n: 1
          action: Confirm HUE_BRIDGE_IP and HUE_APP_KEY (or HUE_KEY_FILE) are set (README "Point it at your bridge"). Run `hue-kit report` -- read-only; confirms reachability and prints the certified-minimum group family + each scene as a layer stack.
          on_failure: "no application key" -> help the user create one (README curl); connection error -> check HUE_BRIDGE_IP.
        - n: 2
          action: Run `hue-kit groups` to write a starter scene-groups.yaml with placeholder names (G1.., ALL). Help the user RENAME the groups to meaningful names; optionally add a `templates:` block naming each stack sequence.
        - n: 3
          action: Run `hue-kit export` to materialise scene-designs.yaml from live colours + the registry. It verifies the family expresses AND bakes every scene before writing.
        - n: 4
          action: Run `hue-kit render` to produce index.html; open it for the user.
    - id: change-a-scene
      name: Change a scene's look from a conversation
      keywords: [make warmer, dim, brighter, change colour, edit scene, recolour, adjust brightness]
      goal: Apply a requested look change safely via YAML, dry-run, then write.
      steps:
        - n: 1
          action: Edit the YAML, not the bridge. A scene's look -> its `layers:` in scene-designs.yaml (colour is `xy:[x,y]` authoritative + a `# hsl(...)` note; `ct:<mirek>` = tunable white; `bri` = percent). The vocabulary -> scene-groups.yaml.
        - n: 2
          action: Run `hue-kit validate` to show the exact per-light diff vs the bridge.
        - n: 3
          action: Run `hue-kit apply` (DRY-RUN) and show the user what would change.
        - n: 4
          action: On the user's OK, run `hue-kit apply --yes`. It backs each scene up to tmp/, writes only beyond-tolerance lights, and verifies by re-read.
        - n: 5
          action: If scenes changed materially, re-run `hue-kit report` -- if the vocabulary is no longer minimal or a template's stack order flipped, update scene-groups.yaml.
      gotchas:
        - Only `apply --yes` writes to the bridge; everything else is read-only. Always dry-run first.
        - Edits change scene DEFINITIONS -- visible on next activation, nothing actuates live. This skill does not turn lights on.
        - Colour is xy-authoritative; editing the `# hsl(...)` note does nothing -- edit `xy`.
        - Revert path: tmp/scene-backup-*-layered-*.json (per-scene backups apply writes before each change).
```

## When to invoke

- The user wants to set up scene management on their Hue bridge.
- The user asks to change a scene's colour/brightness ("make Reading warmer",
  "dim the bar in Movie night").
- The user wants to see or regenerate the HTML report, or export their current
  bridge configuration to YAML.

Do NOT use to turn scenes/lights on or off at runtime (this edits definitions),
for non-Hue ecosystems, or for the bootstrap engine (`/bootstrap`).

## The CLI

Shims at `bin/hue-kit` (Unix) / `bin/hue-kit.cmd` (Windows); Claude Code adds
`bin/` to PATH, so `hue-kit` works from any cwd. Or run
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/hue_kit_cli.py <verb>`.

| Command | What it does |
|---------|--------------|
| `report` | Read the bridge, solve the minimal group family, print each scene as a layer stack. Read-only. |
| `groups [PATH]` | Write a starter scene-groups.yaml (placeholder names to rename). |
| `export` | Materialise scene-designs.yaml from live scenes + the registry. |
| `render [PATH]` | Render the HTML report (default index.html). |
| `validate` | Diff your YAML against the bridge, per light. Read-only. |
| `apply [--yes] [--scene NAME]` | Write the YAML to the bridge. Dry-run unless `--yes`. |
| `init [DIR]` | Copy the example YAML + HTML into DIR to overwrite with your own. |

Working files (`scene-groups.yaml`, `scene-designs.yaml`, `index.html`) default
to the current directory; pass `--dir PATH` to relocate, or set
`HUE_GROUPS_FILE` / `HUE_DESIGNS_FILE`.

## Full model reference

`${CLAUDE_PLUGIN_ROOT}/scene-layers.md` is the complete layered model + verb
spec; the plugin `CLAUDE.md` has the safety rules and maintenance notes. The
`examples/` directory holds a worked example (42 lights, 12 scenes) --
`examples/index.html` is the rendered report to show off the format.
