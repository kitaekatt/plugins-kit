# CLAUDE.md -- hue-kit plugin

Guidance for an AI agent working in this plugin. hue-kit models Philips Hue
scenes with the **LAYERED** (painter's-algorithm) model and syncs them with a
bridge. A user may say **"set this up on my bridge"** or **"make Reading
warmer"** -- this file says how. `skills/hue-domain/references/scene-layers.md`
is the full model + verb spec (and `hue-bridge-basics.md` beside it the CLIP v2
fundamentals);
`README.md` is the human quickstart.

## What this is

A scene = a default of OFF plus an ordered stack of **layers**, each painting one
**meta-group** (a named light-subset) a colour + brightness; the **topmost**
layer covering a light wins. A lower layer's group may be a superset that higher
layers overpaint, so complement groups vanish. A solver computes the certified
minimum vocabulary size, then emits the lowest-overlap family within one group
of it -- extra group only when it buys a family that maps more cleanly onto the
home's natural structure.

## Layout

- `scripts/scene-layers.py` -- THE tool: the solver + the bi-directional sync
  (report / export / validate / apply). Driven via the CLI below.
- `scripts/scene-meta-groups.py` -- a READ-ONLY primitives library imported by
  scene-layers.py (bridge I/O, colour math, the HTML renderer). Not run directly.
- `scripts/hue_kit_cli.py` -- the `hue-kit` verb front-end (report / groups /
  export / render / validate / apply / init). Re-execs under the plugin venv via
  `bootstrap_guard.py` (vendored, stdlib-only; canonical in bootstrap's
  `bootstrap_lib/`).
- `bin/hue-kit`, `bin/hue-kit.cmd` -- PATH shims (Claude Code adds `bin/` to PATH).
- `examples/scene-groups.yaml`, `examples/scene-designs.yaml`, `examples/index.html`
  -- the author's home (42 lights, 12 scenes). **Example data**; a user
  regenerates their own or overwrites via `hue-kit init`.

## The CLI

`hue-kit <verb>` (from PATH), or
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/hue_kit_cli.py <verb>`. Working files
(`scene-groups.yaml`, `scene-designs.yaml`, `index.html`) default to the plugin
data dir (`~/.claude/plugins/data/plugins-kit/hue-kit`) -- one source of truth
regardless of invocation cwd; pass `--dir PATH` to relocate. Verbs map to
scene-layers.py flags:
`report` (default report), `groups` (`--export-groups`), `export`
(`--export-designs`), `render` (`--html`), `validate` (`--validate-design`),
`apply [--yes]` (`--apply`). `start` is the exception: it composes several of
these, so it runs them as SUBPROCESSES (`_call_scene_layers`) rather than via
the `os.execve` runner the single-verb commands use -- exec never returns.

## First: environment

The user provides the bridge connection (README "Point it at your bridge"):
`HUE_BRIDGE_IP` and either `HUE_APP_KEY` or `HUE_KEY_FILE`. Never commit a user's
key. If `HUE_APP_KEY` is unset the tool falls back to `HUE_KEY_FILE` then
`secrets/hue-bridge-key.txt`.

## The default entry point: `hue-kit start`

**Run this first for any opening request that does not already name an
operation** -- including a bare skill invocation. It replaces hand-running the
setup chain, and it decides between three states rather than making you infer
them. It prints `hue-kit-verdict: <state>` as its last line; branch on that.

- `first-run` -- nothing existed, so it built `scene-groups.yaml` +
  `scene-designs.yaml`, rendered `index.html`, and opened it. This is the ONLY
  state that writes without asking (nothing existed to overwrite). Report it and
  stop: the placeholder group names (`G1..`) are a working default, and asking
  the user to name them at setup -- or proposing names -- hands them a question
  their data cannot answer (see the naming guardrail in the domain skill).
- `clean` -- bridge matches the local design. Ask whether to view the report or
  change a scene.
- `changed` -- they disagree, in SHAPE (light/zone/scene added, removed, or
  renamed -- caught by the stored fingerprint) or in COLOUR (caught by
  `validate`). **Nothing is written.** Surface what differs and ask which way to
  sync: a diff cannot distinguish "the bridge moved" from "the YAML holds
  unapplied edits", and pulling vs pushing destroys opposite work. `hue-kit
  start --accept` re-baselines a reviewed shape change without touching YAML.

`bridge-fingerprint.txt` in the working dir stores the bridge's shape (lights,
zone membership, scene names). `export` re-baselines it -- that is what closes
the loop after a shape change, so do not remove that coupling.

### Setting it up manually (what `start` automates)

1. `hue-kit report` -- read-only; confirms the bridge is reachable and prints the
   solved group family (certified minimum, or one group more when that buys a
   better-structured family) + each scene as a layer stack.
2. `hue-kit groups` -- writes a starter `scene-groups.yaml` with suggested names
   (zone-derived where a group maps onto whole zones, else `G1..`; the whole
   home is `ALL`). Leave them; rename only when the USER asks (optionally
   adding a `templates:` block naming each stack sequence).
3. `hue-kit export` -- materialises `scene-designs.yaml` from their live scene
   colours + the registry. Verifies the family expresses AND bakes every scene.
4. `hue-kit render` -- renders `index.html`.

Never re-run `groups` over an existing registry to "refresh" it: it writes
placeholder names and would destroy the user's renames. A changed group
vocabulary is a conversation, not a regeneration.

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
  under the working dir (default: the plugin data dir) -- the revert path.
- Colour is **xy-authoritative**; do not hand-edit the `# hsl(...)` annotations
  expecting them to take effect -- edit `xy`.
- A dark light may be stored as `on: true, brightness: 0` rather than
  `on: false` (the Hue app writes some scenes this way). `_effectively_off()`
  in scene-layers.py treats both as off, which is what lets `export` ->
  `validate` round-trip cleanly. Before that guard existed, such a scene
  reported the same discrepancies on every run and no amount of `apply` could
  silence it. Keep the analyzer and the diff agreeing on what "dark" means.
- After changing scenes, re-run `hue-kit report` -- if the group vocabulary is no
  longer minimal, or a template's stack order flipped (the solver orders layers
  by brightness), update `scene-groups.yaml` (see
  skills/hue-domain/references/scene-layers.md "Template names").

## Maintenance notes

- `scene-meta-groups.py` is loaded by PATH (via `importlib`), so its hyphenated
  filename is intentional -- do not rename it or scene-layers.py without updating
  the loader.
- `bootstrap_guard.py` is a **vendored** byte-for-byte copy of the canonical at
  `plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`; a drift test in
  plugins-kit asserts copies match. Every other copy -- git-kit's, p4-kit's,
  this one -- is vendored, so editing one of THOSE is what breaks the test (it
  is how p4-kit 0.16.1 drifted). If you change the guard, change the canonical
  and re-vendor.
- The example YAML/HTML are the author's home. Keep them buildable but treat them
  as a worked example, not this plugin's own config.
