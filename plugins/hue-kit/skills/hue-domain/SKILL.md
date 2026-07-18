---
_schema_version: 1
name: hue-domain
author: christina
skill-type: domain-skill
description: Use when reading, analysing, authoring, or syncing Philips Hue scenes on a bridge via the hue-kit CLI. Do NOT use for non-Hue lighting or activating scenes.
---

# Hue Domain

Administering and authoring **Philips Hue scenes** on a bridge with the layered
(painter's-algorithm) scene model, via the `hue-kit` CLI. This domain owns the
model, the bridge fundamentals, the vocabulary, and the read/author/sync
operations -- everything needed to describe a home's scenes as editable YAML and
keep it in sync with the bridge, on ANY bridge.

The YAML contract below is the load-bearing surface; match user phrasing against
the `keywords:` clusters to route to a reference, capability, or tool.

```yaml
domain_skill:
  _schema_version: "1"
  identity: >-
    Administering and authoring Philips Hue scenes on a bridge with the layered
    scene model, via the hue-kit CLI.
  companions:
    siblings: []
    note: >-
      No sibling domains in this plugin. Grew out of a home smart-home domain
      (home-domain), but this skill is deliberately bridge-agnostic: it carries
      only knowledge that applies to every Hue bridge, no one home's inventory,
      IPs, zone names, or scene set. Distinct from any whole-home domain (network
      / non-Hue devices / automations) -- this is Hue scenes only.
  scope:
    covers:
      - reading a bridge's scenes and solving the minimal meta-group vocabulary
      - materialising the current bridge configuration to editable YAML
      - authoring scene look/colour/brightness changes as YAML layer edits
      - writing YAML scene definitions back to the bridge (definition-only)
      - rendering the self-contained browsable HTML report
      - Hue CLIP v2 bridge fundamentals (connect, entity model, xy colour)
    excludes:
      - activating / triggering / turning scenes or lights on at runtime (this
        edits DEFINITIONS, it does not actuate)
      - non-Hue lighting ecosystems (LIFX, Nanoleaf, etc.)
      - whole-home administration beyond Hue scenes (network, other devices)
      - one specific home's inventory / credentials / named scenes
  orientation:
    summary: >-
      Work in this domain is documentation-first and verification-led: the bridge
      is the source of truth for current scene definitions, so read it (hue-kit
      report / validate) before recommending or writing a change. The core loop
      is edit-YAML -> validate -> apply: a scene is a default of OFF plus an
      ordered stack of layers, each painting one meta-group a colour+brightness,
      topmost layer wins. Colour is xy-authoritative. Scene edits change
      DEFINITIONS only (visible on next activation, nothing actuates live), so
      they are safe against a live home -- but a redefinition that changes a
      scene's LOOK is a human-judgement call to surface, not decide. Start with
      hue-bridge-basics if the bridge connection or entity model is unclear;
      scene-layers is the full model + verb spec.
    vocabulary:
      - term: meta-group
        definition: >-
          A named light-subset in the registry (scene-groups.yaml), defined as a
          union of bridge zones (or explicit lights). The layered vocabulary is a
          small set of these -- a certified minimum.
      - term: layer
        definition: >-
          One entry in a scene's stack = (meta-group, colour + brightness).
          Layers are ordered bottom -> top; the topmost layer covering a light
          wins.
      - term: layer stack
        definition: >-
          A scene's ordered list of layers over a default of OFF. A lower layer's
          group may be a SUPERSET the higher layers overpaint.
      - term: certified minimum
        definition: >-
          The solver's guarantee that no smaller family of meta-groups can
          express every scene as a layer stack.
      - term: xy-authoritative
        definition: >-
          A bulb's real colour is its CIE xy point in its gamut; the # hsl(...)
          annotation is derived and readable-only. To shift a hue, edit the xy.
      - term: definition vs actuation
        definition: >-
          Editing a scene changes its stored definition (what it will apply);
          nothing lights up until the scene is next activated. This domain edits
          definitions and never actuates.
    behavioral_guardrails:
      - >-
        Investigate before answering: read the live bridge (hue-kit report /
        validate) before recommending or writing a change; do not reason from
        memory of how the scenes "should" look.
      - >-
        Only `hue-kit apply --yes` writes to the bridge. Everything else is
        read-only. Always dry-run (`hue-kit apply`) and show the diff first.
      - >-
        Flag look changes. Redefining a scene is cheap and reversible; whether
        the new look is good is the user's call -- surface it, do not decide it.
      - >-
        Bridge-agnostic only: never bake one home's IPs, keys, zone names, or
        scene set into this skill's docs. Connection details come from the user's
        environment (HUE_BRIDGE_IP / HUE_APP_KEY / HUE_KEY_FILE).
      - >-
        Never write to the bridge through the read-only primitives library
        (scene-meta-groups.py); writes go through `hue-kit apply` (backup + PUT +
        verify).
  index:
    references:
      - id: hue-bridge-basics
        path: references/hue-bridge-basics.md
        keywords: [bridge, connect, ip, discovery, application key, link button,
                   clip v2, api, https, self-signed, entity model, light, room,
                   zone, grouped_light, scene, xy, mirek, brightness, definition
                   vs actuation, credential]
        summary: >-
          General CLIP v2 fundamentals for any bridge -- connecting (IP, app key,
          TLS), the resource model (light/room/zone/grouped_light/scene), why
          scenes are definitions not live state, and xy-authoritative colour.
      - id: scene-layers
        path: references/scene-layers.md
        keywords: [layered model, painter's algorithm, meta-group, layer, layer
                   stack, certified minimum, solver, scene-groups.yaml,
                   scene-designs.yaml, export, validate, apply, template names,
                   authoring workflow, xy, ct, bri]
        summary: >-
          The layered scene model + the scene-layers.py solver/sync behind the
          CLI verbs -- the two config files, the report/export/validate/apply
          operations, the authoring workflow, and template-name maintenance.
  capabilities:
    - id: report
      keywords: [read scenes, analyse, solve, minimal groups, meta-groups,
                 report, layer stacks, what scenes]
      description: >-
        Read the live bridge, solve the smallest meta-group vocabulary, and print
        each scene as a layer stack with bake verification. Read-only; start here.
      operation: hue-kit report
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (The tool -- solver)
    - id: groups
      keywords: [starter registry, group vocabulary, placeholder names, rename
                 groups, scene-groups.yaml]
      description: >-
        Write a starter scene-groups.yaml with placeholder group names for the
        user to rename to something meaningful.
      operation: hue-kit groups [PATH]
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (Sync)
    - id: export
      keywords: [bridge to yaml, materialise design, current configuration,
                 scene-designs.yaml, capture]
      description: >-
        Materialise scene-designs.yaml from the live bridge colours + the
        registry; verifies the family expresses and bakes every scene first.
      operation: hue-kit export
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (Sync)
    - id: render
      keywords: [html, report page, browsable, self-contained, the draw, index.html]
      description: >-
        Render the self-contained HTML report (config + source embedded) -- the
        shareable, buildable spec of the scenes.
      operation: hue-kit render [PATH]
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (The tool -- solver)
    - id: validate
      keywords: [diff, compare, validate, discrepancies, yaml vs bridge, drift]
      description: Diff the YAML design against the live bridge, per light. Read-only.
      operation: hue-kit validate
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (Sync)
    - id: apply
      keywords: [write to bridge, bake, apply, set scenes, yaml to bridge, --yes,
                 dry-run, backup]
      description: >-
        Write the YAML layer stacks back to the bridge. Dry-run unless --yes;
        backs each scene up, writes only beyond-tolerance lights, verifies by
        re-read.
      operation: hue-kit apply [--yes] [--scene NAME]
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (Sync)
    - id: init
      keywords: [example, scaffold, copy examples, overwrite with your own,
                 starter files]
      description: >-
        Copy the shipped example scene-groups.yaml / scene-designs.yaml /
        index.html into a directory to overwrite with the user's own.
      operation: hue-kit init [DIR]
      tool: scripts/hue_kit_cli.py
      reference_section: scene-layers.md (The two config files)
  tools:
    - name: hue-kit
      command: hue-kit <report|groups|export|render|validate|apply|init> [--dir PATH]
      description: >-
        The verb CLI over the layered scene tool. PATH shims at bin/hue-kit(.cmd)
        put it on PATH; or run scripts/hue_kit_cli.py. Re-execs under the plugin's
        bootstrap-provisioned venv. Working files (scene-groups.yaml /
        scene-designs.yaml / index.html) default to the cwd.
    - name: scene-layers.py
      command: python scripts/scene-layers.py [--html|--export-designs|--validate-design|--apply ...]
      description: >-
        The layered solver + bi-directional sync the CLI wraps. Read-only against
        the bridge except --apply --yes. See references/scene-layers.md.
    - name: scene-meta-groups.py
      command: (imported, not run directly)
      description: >-
        READ-ONLY primitives library (bridge I/O, colour math, HTML renderer)
        that scene-layers.py imports. Never run directly; never write through it.
```

```yaml
asset_dependencies:
  - path: ../../examples/scene-groups.yaml
    consumer: scripts/hue_kit_cli.py (init)
    purpose: shipped example meta-group registry copied by `hue-kit init`
  - path: ../../examples/scene-designs.yaml
    consumer: scripts/hue_kit_cli.py (init)
    purpose: shipped example layered design copied by `hue-kit init`
  - path: ../../examples/index.html
    consumer: scripts/hue_kit_cli.py (init)
    purpose: shipped example rendered report copied by `hue-kit init`
  - path: ../../scripts/scene-layers.py
    consumer: scripts/hue_kit_cli.py
    purpose: the solver/sync tool the CLI execs with translated flags
  - path: ../../scripts/bootstrap_guard.py
    consumer: scripts/hue_kit_cli.py
    purpose: vendored guard that re-execs the CLI under the plugin venv
```

## When to invoke

- Setting up scene management on a Hue bridge for the first time
  (`report` -> `groups` -> `export` -> `render`).
- Changing a scene's colour/brightness ("make Reading warmer", "dim the bar in
  Movie night") -- edit YAML -> `validate` -> `apply`.
- Seeing or regenerating the HTML report, or exporting the current bridge
  configuration to YAML.

Do NOT use to turn scenes/lights on or off at runtime (this edits definitions),
for non-Hue ecosystems, or for whole-home administration beyond Hue scenes.
