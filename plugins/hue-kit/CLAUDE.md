# CLAUDE.md -- hue-kit plugin

Guidance for an AI agent working in this plugin. hue-kit models Philips Hue
scenes with the **LAYERED** (painter's-algorithm) model and syncs them with a
bridge. A user may say **"set this up on my bridge"** or **"make Reading
warmer"** -- this file says how. `scene-layers.md` is the full model + verb spec;
`README.md` is the human quickstart.

## What this is

A scene = a default of OFF plus an ordered stack of **layers**, each painting one
**meta-group** (a named light-subset) a colour + brightness; the **topmost**
layer covering a light wins. A lower layer's group may be a superset that higher
layers overpaint, so complement groups vanish. A solver computes the **smallest**
group vocabulary that expresses every scene (a certified minimum).

## Layout

- `scripts/scene-layers.py` -- THE tool: the solver + the bi-directional sync
  (report / export / validate / apply). Driven via the CLI below.
- `scripts/scene-meta-groups.py` -- a READ-ONLY primitives library imported by
  scene-layers.py (bridge I/O, colour math, the HTML renderer). Not run directly.
- `scripts/hue_kit_cli.py` -- the `hue-kit` verb front-end (report / groups /
  export / render / validate / apply / init). Re-execs under the plugin venv via
  `bootstrap_guard.py` (vendored, stdlib-only; canonical copy in git-kit).
- `bin/hue-kit`, `bin/hue-kit.cmd` -- PATH shims (Claude Code adds `bin/` to PATH).
- `examples/scene-groups.yaml`, `examples/scene-designs.yaml`, `examples/index.html`
  -- the author's home (42 lights, 12 scenes). **Example data**; a user
  regenerates their own or overwrites via `hue-kit init`.

## The CLI

`hue-kit <verb>` (from PATH), or
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/hue_kit_cli.py <verb>`. Working files
(`scene-groups.yaml`, `scene-designs.yaml`, `index.html`) default to the current
directory; pass `--dir PATH` to relocate. Verbs map to scene-layers.py flags:
`report` (default report), `groups` (`--export-groups`), `export`
(`--export-designs`), `render` (`--html`), `validate` (`--validate-design`),
`apply [--yes]` (`--apply`).

## First: environment

The user provides the bridge connection (README "Point it at your bridge"):
`HUE_BRIDGE_IP` and either `HUE_APP_KEY` or `HUE_KEY_FILE`. Never commit a user's
key. If `HUE_APP_KEY` is unset the tool falls back to `HUE_KEY_FILE` then
`secrets/hue-bridge-key.txt`.

## Setting it up on a NEW bridge (in order)

1. `hue-kit report` -- read-only; confirms the bridge is reachable and prints the
   certified-minimum group family + each scene as a layer stack.
2. `hue-kit groups` -- writes a starter `scene-groups.yaml` with placeholder
   names (`G1..`, `ALL`). **Then help the user rename** the groups to meaningful
   names (optionally add a `templates:` block naming each stack sequence).
3. `hue-kit export` -- materialises `scene-designs.yaml` from their live scene
   colours + the registry. Verifies the family expresses AND bakes every scene.
4. `hue-kit render` -- renders `index.html`.

## Making scene changes from a conversation

The core loop. When the user asks for a change ("make Reading warmer", "dim the
bar in Movie night"):

1. **Edit the YAML**, not the bridge directly:
   - a scene's look -> edit its `layers:` in `scene-designs.yaml`. Colour is
     `xy: [x, y]` (authoritative, exact Hue gamut) with a `# hsl(...)` note;
     `ct: <mirek>` is tunable white; `bri` is percent. To shift a hue, edit the
     `xy` (regenerate the `# hsl` note on the next `export`).
   - the vocabulary (add/rename/re-scope a group) -> edit `scene-groups.yaml`.
2. `hue-kit validate` -- show the user the exact per-light diff vs the bridge.
3. `hue-kit apply` -- DRY-RUN. Show what would change.
4. `hue-kit apply --yes` -- write it. Backs each scene up to `tmp/` first, writes
   only beyond-tolerance lights, verifies by re-read.

## Safety rules

- **Only `apply --yes` writes to the bridge.** Everything else is read-only.
  Always dry-run and show the diff before `--yes`.
- Scene edits change *definitions* only -- visible on the scene's next
  activation, nothing actuates live.
- `apply` writes per-scene JSON backups to `tmp/scene-backup-*-layered-*.json`
  (the revert path).
- Colour is **xy-authoritative**; do not hand-edit the `# hsl(...)` annotations
  expecting them to take effect -- edit `xy`.
- After changing scenes, re-run `hue-kit report` -- if the group vocabulary is no
  longer minimal, or a template's stack order flipped (the solver orders layers
  by brightness), update `scene-groups.yaml` (see scene-layers.md "Template names").

## Maintenance notes

- `scene-meta-groups.py` is loaded by PATH (via `importlib`), so its hyphenated
  filename is intentional -- do not rename it or scene-layers.py without updating
  the loader.
- `bootstrap_guard.py` is a **vendored** byte-for-byte copy of git-kit's
  canonical; a drift test in plugins-kit asserts copies match. If you change the
  guard, change the canonical and re-vendor.
- The example YAML/HTML are the author's home. Keep them buildable but treat them
  as a worked example, not this plugin's own config.
