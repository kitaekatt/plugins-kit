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

See [`skills/hue-domain/references/scene-layers.md`](skills/hue-domain/references/scene-layers.md)
for the full model + command reference, and
[`skills/hue-domain/references/hue-bridge-basics.md`](skills/hue-domain/references/hue-bridge-basics.md)
for Hue CLIP v2 fundamentals.

## Install

Enable the plugin from the `plugins-kit` marketplace. The `bootstrap` plugin
provisions a venv (requests, pyyaml, urllib3) from `pyproject.toml` on session
start -- no manual `pip install`. Claude Code adds this plugin's `bin/` to PATH,
so the `hue-kit` command works from any directory.

## Point it at your bridge

There is **no default bridge** -- set it up once and the tool remembers:

```bash
hue-kit discover     # find your bridge on the network (caches the IP)
hue-kit pair         # press the bridge link button when prompted; mints +
                     #   stores the application key user-scoped (0600)
```

`pair` also takes `--no-wait`, which skips the press-Enter prompt and starts
the ~30s poll immediately -- for agents driving the flow, which confirm you are
ready first and then tell you to press the button.

After that, every verb just works -- `hue-kit discover` caches the IP and
`hue-kit pair` stores the key, both under
`~/.claude/plugins/data/plugins-kit/hue-kit/`.

**Manual override** (CI, multiple bridges, or if discovery is blocked): set the
env vars instead, and they win over the cached/paired values.

```bash
export HUE_BRIDGE_IP=<BRIDGE_IP>     # skip discovery
export HUE_APP_KEY=<KEY>             # or HUE_KEY_FILE=/path/to/key -- skip pairing
```

To create a key by hand: press the round link button on the bridge, then within
30s POST to it (the `"username"` in the response is your key):
```bash
curl -k -X POST https://<BRIDGE_IP>/api \
  -H 'Content-Type: application/json' \
  -d '{"devicetype":"hue-kit#tool","generateclientkey":true}'
```

## The CLI

Your `scene-groups.yaml`, `scene-designs.yaml`, and `index.html` live in the
plugin data dir (`~/.claude/plugins/data/plugins-kit/hue-kit`) by default, so
every verb sees the same files no matter where you run from (pass `--dir` to
relocate).

```bash
hue-kit discover          # find your bridge on the network (caches the IP)
hue-kit pair              # mint + store the app key (press the link button)

hue-kit start             # START HERE. First run: build the YAML, render the
                          #   report, open it. After that: check whether the
                          #   bridge still matches your YAML. Add --no-open to
                          #   skip the browser, --accept to re-baseline after a
                          #   reviewed change.

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
                          #   overwrite with your own (default: the data dir)
```

Typical first run: just `hue-kit start` -- it does `groups` -> `export` ->
`render` and opens the report. Then rename the placeholder groups (`G1..`) in
`scene-groups.yaml` to something meaningful.

Re-running `start` later checks the bridge against your YAML. If they differ it
reports what changed and stops rather than guessing: a difference can mean the
bridge moved, or that your YAML holds edits you never applied, and the fixes are
opposite. Pull with `export`, push with `apply`.

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
| `HUE_BRIDGE_IP` | your bridge's IP | none -- auto-discovered + cached (`hue-kit discover`) |
| `HUE_APP_KEY` | the application key (value) | -- |
| `HUE_KEY_FILE` | path to a file holding the key | the paired key (`hue-kit pair`) if present |
| `HUE_GROUPS_FILE` | path to the registry | `<dir>/scene-groups.yaml` |
| `HUE_DESIGNS_FILE` | path to the design | `<dir>/scene-designs.yaml` |

The CLI is read-only against the bridge except `apply --yes`.

## Provenance

Grown from Christina's home-automation skill and the standalone snapshot
published at <https://kitaekatt.github.io/pastebin/hue/>. This plugin is the
packaged, bridge-agnostic form.
