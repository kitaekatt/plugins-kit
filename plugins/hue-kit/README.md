# hue-kit

A Claude Code plugin for describing Philips Hue scenes as an ordered stack of
**layers** over a minimal, reusable vocabulary of light groups -- and for
syncing that description bi-directionally with your bridge.

**The draw:** a single self-contained HTML report (`examples/index.html`, open
it) that renders a real 12-scene home (42 lights) as brightness-scaled swatches
and layer stacks. Its "View config & source" button embeds the whole system
(the YAML config + the Python source), so the one file is a buildable spec. The
plugin ships that example so you can see the shape immediately -- then you
overwrite the config and the report with your own bridge's.

## The idea

A scene = a default of **OFF** plus an ordered stack of layers; each layer paints
one **meta-group** (a named set of lights) a single colour + brightness, and the
**topmost** layer covering a light wins. A lower layer's group may be a superset
that higher layers overpaint, so "everything-except-X" groups never need to
exist. A solver finds the **smallest** group vocabulary that can express all your
scenes (a certified minimum), and every scene becomes a short, readable stack.

See [`scene-layers.md`](scene-layers.md) for the full model + command reference.

## Install

Enable the plugin from the `plugins-kit` marketplace. The `bootstrap` plugin
provisions a venv (requests, pyyaml, urllib3) from `pyproject.toml` on session
start -- no manual `pip install`. Claude Code adds this plugin's `bin/` to PATH,
so the `hue-kit` command works from any directory.

## Point it at your bridge

1. **Find your bridge IP** -- the Hue app, your router, or
   <https://discovery.meethue.com>.
2. **Create an application key** -- press the round link button on the bridge,
   then within 30s:
   ```bash
   curl -k -X POST https://<BRIDGE_IP>/api \
     -H 'Content-Type: application/json' \
     -d '{"devicetype":"hue-kit#tool","generateclientkey":true}'
   ```
   The response contains `"username":"<KEY>"` -- that string is your key.
3. **Export both** (add to your shell rc to persist):
   ```bash
   export HUE_BRIDGE_IP=<BRIDGE_IP>
   export HUE_APP_KEY=<KEY>
   ```
   (Alternatively put the key in a file and set `HUE_KEY_FILE=/path/to/key`.)

## The CLI

Run from the directory where you want your `scene-groups.yaml`,
`scene-designs.yaml`, and `index.html` to live (or pass `--dir`).

```bash
hue-kit report            # read the bridge; print the minimal group family
                          #   + each scene as a layer stack (read-only)
hue-kit groups            # write a starter scene-groups.yaml (placeholder
                          #   names G1..) -- then rename the groups meaningfully
hue-kit export            # materialise scene-designs.yaml from your live scenes
hue-kit render            # render index.html (config + source embedded)
hue-kit validate          # diff your YAML vs the bridge, per light (read-only)
hue-kit apply             # DRY-RUN: show what would change on the bridge
hue-kit apply --yes       # actually write to the bridge

hue-kit init [DIR]        # drop the shipped example YAML + HTML into DIR to
                          #   overwrite with your own (default: current dir)
```

Typical first run: `report` -> `groups` (rename the groups) -> `export` ->
`render` -> `apply --yes`.

## Author + apply changes

Edit `scene-designs.yaml` (a scene's layers -- `xy` colour is authoritative, the
`# hsl(...)` is a readable note; `bri` is percent) and/or `scene-groups.yaml`
(the group vocabulary), then:

```bash
hue-kit validate          # diff your edits vs the bridge
hue-kit apply             # DRY-RUN
hue-kit apply --yes       # write to the bridge
```

`apply` backs up each scene to `tmp/` before it writes, updates only lights that
differ beyond tolerance, and verifies by re-reading. It changes scene
*definitions* only -- nothing actuates until you next activate the scene.

## The two config files

- **`scene-groups.yaml`** -- the meta-group registry: each group is a `name` +
  the bridge `zones` (or explicit `lights`) it unions. An optional `templates:`
  block names each distinct layer-stack sequence.
- **`scene-designs.yaml`** -- per scene, the ordered `layers:` stack. The source
  of truth for `apply`; regenerate with `hue-kit export`.

## Environment variables

| var | meaning | default |
|---|---|---|
| `HUE_BRIDGE_IP` | your bridge's IP | `192.168.0.246` (the example home) |
| `HUE_APP_KEY` | the application key (value) | -- |
| `HUE_KEY_FILE` | path to a file holding the key | `secrets/hue-bridge-key.txt` |
| `HUE_GROUPS_FILE` | path to the registry | `<dir>/scene-groups.yaml` |
| `HUE_DESIGNS_FILE` | path to the design | `<dir>/scene-designs.yaml` |

The CLI is read-only against the bridge except `apply --yes`.

## Provenance

Grown from Christina's home-automation skill and the standalone snapshot
published at <https://kitaekatt.github.io/pastebin/hue/>. This plugin is the
packaged, bridge-agnostic form.
