# Layered scene model -- solver + sync (scene-layers.py)

The LAYERED (painter's-algorithm) model for the Hue scene framework. It replaced
the partition model (`scene-schema.py` + the partition `scene-designs.yaml` and
its meta-group/template name registry) on 2026-07-18; history is in
`dev/tasks/lighting/log.md`. This doc is the operating reference.

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
reusable vocabulary.

## The two files (source of truth)

- **`references/scene-groups.yaml`** -- the meta-group **registry**: each group
  is a `name` + the bridge `zones` whose light-union defines it. Editable
  vocabulary. Today: 6 groups `ALL / BACK / DINING / MOOD / KITCHEN / BATHROOM`,
  a CERTIFIED MINIMUM (no 5-group family can express the live scenes; every
  group is a whole-zone union, so it is also fully nameable).
- **`references/scene-designs.yaml`** -- the **layered design**: per scene, an
  ordered `layers:` list, each `{ group, xy: [x,y] | ct: <mirek>, bri }`.
  Colour model: **xy is AUTHORITATIVE** (exact Hue gamut) with a trailing
  `# hsl(...)` annotation for readability; `ct: <mirek>` = tunable white.
  `bri` = percent.

## The tool -- scene-layers.py

Solver (read-only):

- (no args) -- print the minimum family + per-scene stacks + bake verification.
- `--html [PATH]` -- browsable report (default `tmp/scene-layers.html`), via the
  shared `scene-meta-groups.py` renderer (guardrail: all HTML goes through
  `smg.layered_report()` -- never hand-roll one).
- `--json [--out P]` / `--cells P` (solve an offline export) / `--export-cells P`.

Sync (over the two files above):

- `--export-designs [PATH]` -- materialise `scene-designs.yaml` from the live
  bridge colours + `scene-groups.yaml`. VERIFIES the registry family expresses
  AND bakes every scene before writing (fail loud), so the design is always
  faithful. Default output = `references/scene-designs.yaml`.
- `--validate-design` -- diff the design against the live bridge (report only,
  analyzer tolerances). `0 discrepancies` = bridge matches design.
- `--apply` -- bake the layer stacks onto the bridge. **Dry-run unless `--yes`.**
  Resolves every targeted scene first (atomic -- a parse error writes nothing),
  writes ONLY beyond-tolerance lights (in-tolerance lights stay byte-exact),
  backs each scene up to `tmp/scene-backup-<scene>-layered-<ts>.json`
  (never-overwriting) then PUT + verify by re-read.
- `--design PATH` (default `references/scene-designs.yaml`), `--scene NAME`
  (repeatable) limit `--apply` / `--validate-design`.

## Authoring workflow

1. Edit `scene-groups.yaml` (to rename/re-scope groups) and/or
   `scene-designs.yaml` (to restyle a scene's layers).
2. `scene-layers.py --validate-design` -- see the pending diff vs the bridge.
3. `scene-layers.py --apply` (dry-run) to review, then `--apply --yes` to write.
4. Re-run the solver (`scene-layers.py`) after adding/restyling scenes to
   confirm the 6-group family is still a certified minimum; if the vocabulary
   changed, edit `scene-groups.yaml` and re-`--export-designs`.

To re-capture the live bridge into the design (e.g. after ad-hoc scene edits):
`scene-layers.py --export-designs` (verifies + overwrites the design file).

## Notes

- bri-0 cells fold into OFF (a colour at 0% brightness is dark -> the default
  layer, not an on-at-0% state).
- ct (tunable-white) scenes stay `ct: <mirek>`; they are not converted to
  saturated xy colour.
- Colour math + the group primitives live in `scene-meta-groups.py`
  (`clip_get`, `Sig`, `analyze_scene`, `build_groups`, `sig_hsl`, `sig_color_label`,
  `layered_report`); scene-layers.py imports them (single source, no duplication).
- The layered design carries only `xy` (authoritative) or `ct` -- there is no
  `hsl:` hand-authoring field (that was a partition-model convenience). To shift
  a hue, edit the `xy` (the `# hsl(...)` annotation is regenerated on export).
- **Template names** live in the `templates:` block of scene-groups.yaml, each
  a `name` + the exact layer-stack `stack` (group sequence). The report labels
  each scene group by its stack. The solver orders a scene's layers by
  brightness (brighter layer on top), so changing a layer's brightness can FLIP
  the stack order (e.g. Cooking went `[KITCHEN, MOOD]` -> `[MOOD, KITCHEN]` when
  MOOD dropped below KITCHEN); when a template shows unnamed, update its `stack`
  to the new order. Order matters for real: Night `[BATHROOM, MOOD]` and Theater
  `[MOOD, BATHROOM]` are distinct templates on the same two groups.
