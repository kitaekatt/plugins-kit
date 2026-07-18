# Layered scene model -- solver + sync (scene-layers.py)

The LAYERED (painter's-algorithm) model for describing Hue scenes, and the
`scene-layers.py` tool that solves and syncs it. This is the operating reference
for the `hue-kit` CLI verbs, which are thin wrappers over this tool.

## The model

A scene = a default of **OFF** plus an **ordered stack of LAYERS** (bottom ->
top). Each layer paints one **meta-group** (a named light-subset) a single
colour + brightness. Every light takes the value of the **topmost layer whose
group contains it**; a light in no layer stays OFF.

The key property: a lower layer's group may be a **SUPERSET** of the cell it
ultimately shows, because higher layers overpaint the excess. So
`ALL(red) -> Kitchen(green)` bakes identically to `AllExceptKitchen(red) +
Kitchen(green)` -- and every "everything-except-X" complement group disappears.
Minimising the GLOBAL count of distinct groups (not per-scene) yields a small,
reusable vocabulary. The solver returns a **certified minimum**: no smaller
family of groups can express every scene.

## The two config files (source of truth)

- **`scene-groups.yaml`** -- the meta-group **registry**: each group is a `name`
  + the bridge `zones` (or explicit `lights`) whose union defines it. This is
  the editable vocabulary. An optional `templates:` block names each distinct
  layer-stack sequence (see Template names below).
- **`scene-designs.yaml`** -- the **layered design**: per scene, an ordered
  `layers:` list, each `{ group, xy: [x,y] | ct: <mirek>, bri }`. Colour model:
  **xy is AUTHORITATIVE** (exact Hue gamut) with a trailing `# hsl(...)`
  annotation for readability; `ct: <mirek>` = tunable white; `bri` = percent.

The `hue-kit` CLI resolves both files in the current working directory by
default (override with `--dir`, or the `HUE_GROUPS_FILE` / `HUE_DESIGNS_FILE`
env vars).

## The tool -- scene-layers.py (via `hue-kit`)

Solver (read-only) -- `hue-kit report`:

- print the minimum group family + per-scene stacks + bake verification.
- `hue-kit render [PATH]` -- browsable HTML report (config + source embedded),
  via the shared `scene-meta-groups.py` renderer (guardrail: all HTML goes
  through `smg.layered_report()` -- never hand-roll one).
- underlying flags also expose `--json` and offline-cell solving (`--cells`,
  `--export-cells`) for tooling.

Sync (over the two files above):

- `hue-kit groups [PATH]` (`--export-groups`) -- write a starter registry with
  placeholder group names for you to rename.
- `hue-kit export` (`--export-designs`) -- materialise `scene-designs.yaml` from
  the live bridge colours + `scene-groups.yaml`. VERIFIES the registry family
  expresses AND bakes every scene before writing (fail loud), so the design is
  always faithful.
- `hue-kit validate` (`--validate-design`) -- diff the design against the live
  bridge (report only, analyzer tolerances). `0 discrepancies` = match.
- `hue-kit apply` (`--apply`) -- bake the layer stacks onto the bridge.
  **Dry-run unless `--yes`.** Resolves every targeted scene first (atomic -- a
  parse error writes nothing), writes ONLY beyond-tolerance lights (in-tolerance
  lights stay byte-exact), backs each scene up to
  `tmp/scene-backup-<scene>-layered-<ts>.json` (never-overwriting) then PUT +
  verify by re-read. `--scene NAME` (repeatable) limits the set.

## Authoring workflow

1. Edit `scene-groups.yaml` (to rename/re-scope groups) and/or
   `scene-designs.yaml` (to restyle a scene's layers).
2. `hue-kit validate` -- see the pending diff vs the bridge.
3. `hue-kit apply` (dry-run) to review, then `hue-kit apply --yes` to write.
4. Re-run the solver (`hue-kit report`) after adding/restyling scenes to confirm
   the group family is still a certified minimum; if the vocabulary changed,
   edit `scene-groups.yaml` and re-run `hue-kit export`.

To re-capture the live bridge into the design (e.g. after ad-hoc scene edits):
`hue-kit export` (verifies + overwrites the design file).

## Notes

- bri-0 cells fold into OFF (a colour at 0% brightness is dark -> the default
  layer, not an on-at-0% state).
- ct (tunable-white) scenes stay `ct: <mirek>`; they are not converted to
  saturated xy colour.
- Colour math + the group primitives live in `scene-meta-groups.py`
  (`clip_get`, `Sig`, `analyze_scene`, `build_groups`, `sig_hsl`,
  `sig_color_label`, `layered_report`); scene-layers.py imports them (single
  source, no duplication). scene-meta-groups.py is a READ-ONLY library -- never
  run it directly, and never write to the bridge through it.
- The layered design carries only `xy` (authoritative) or `ct` -- there is no
  `hsl:` hand-authoring field. To shift a hue, edit the `xy` (the `# hsl(...)`
  annotation is regenerated on export).
- **Template names** live in the `templates:` block of scene-groups.yaml, each a
  `name` + the exact layer-stack `stack` (group sequence). The report labels
  each scene group by its stack. The solver orders a scene's layers by
  brightness (brighter layer on top), so changing a layer's brightness can FLIP
  the stack order; when a template shows unnamed, update its `stack` to the new
  order. Order matters: two scenes on the same two groups but in opposite layer
  order are distinct templates.
