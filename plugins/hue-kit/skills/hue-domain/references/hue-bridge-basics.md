# Hue bridge basics (CLIP v2)

General fundamentals for talking to any Philips Hue bridge over the local CLIP v2
API -- the substrate the layered scene model and the `hue-kit` CLI sit on. None
of this is specific to one home; it applies to every bridge.

## Connecting

**There is no default bridge.** The tool never ships or assumes an IP -- it
either uses `HUE_BRIDGE_IP`, a cached discovery result, or auto-discovers.

- **Bridge IP (discovery).** `hue-kit discover` finds bridges two ways and
  caches the result; verbs auto-discover a single bridge when `HUE_BRIDGE_IP` is
  unset:
    1. the cloud service <https://discovery.meethue.com> (returns LAN bridges
       keyed to the caller's public IP) -- fast, but needs internet and is
       **rate-limited** (HTTP 429 after repeated calls);
    2. **local mDNS** (`_hue._tcp`, via zeroconf) as an automatic fallback when
       the cloud path fails -- LAN-only, no rate limit.
  When the cloud service is rate-limited but mDNS succeeds, the tool says so and
  carries on. The resolved IP is cached user-scoped after the first hit, so
  repeat calls never touch the network. If both methods fail or more than one
  bridge is found, set `HUE_BRIDGE_IP` explicitly (from the Hue app or router).
  Delete the cache file (printed in the message) to force re-discovery.
- **Application key (pairing / app authentication).** The bridge authenticates
  every request with a `hue-application-key` header, and the key **cannot be
  auto-detected** -- minting one requires pressing the physical link button.
  `hue-kit pair` does the flow: discover the bridge, prompt for the button press,
  POST `generateclientkey` to `/api`, and store the key user-scoped (0600). The
  underlying call:
  ```bash
  curl -k -X POST https://<BRIDGE_IP>/api \
    -H 'Content-Type: application/json' \
    -d '{"devicetype":"hue-kit#tool","generateclientkey":true}'
  ```
  The response's `"username"` value IS the key. Instead of pairing you may set
  `HUE_APP_KEY` directly, or point `HUE_KEY_FILE` at a file holding it. **A key
  is a credential -- never commit it.** There is no unauthenticated bypass: only
  bridge discovery and this initial key-creation POST work without a key; every
  `/clip/v2/resource/*` call requires one.
- **TLS.** The bridge serves HTTPS with a self-signed certificate, so clients
  disable cert verification for LAN calls (`curl -k`; the tool does the
  equivalent and silences the urllib3 warning). CLIP v2 lives under
  `https://<ip>/clip/v2/resource/<type>`.

## The entity model

CLIP v2 exposes typed resources; the ones this domain uses:

| Resource | What it is |
|---|---|
| `light` | one bulb/fixture. Carries `on`, `dimming.brightness` (0-100 percent), and colour as `color.xy` (CIE xy in the bulb's gamut) or `color_temperature.mirek` (tunable white). |
| `room` | a physical room. A light belongs to **exactly one** room. |
| `zone` | an arbitrary grouping of lights. A light may be in **many** zones (or none). Zones are the natural building block for scene meta-groups. |
| `grouped_light` | the controllable group behind a room/zone (used to actuate a whole group at once). |
| `scene` | a **stored, per-light set of target states** scoped to a room or zone. A scene holds an `actions` list -- one target state per member light. |

## Scenes are definitions, not live state

Editing a scene changes its **stored definition** -- the target states it will
apply. **Nothing actuates until the scene is next activated.** So scene-editing
tools are safe to run against a live home: they change what a scene *will* look
like, not what the lights are doing right now. (Turning a scene on -- actuation
-- is a different operation this domain deliberately does not do.)

## Colour is xy-authoritative

A bulb's real colour is its `color.xy` point in its own gamut. HSL/RGB are
lossy, gamut-dependent conversions -- useful as human-readable annotations, not
as the source of truth. When authoring, edit `xy`; treat any `# hsl(...)` note
as derived. Tunable-white values stay as `color_temperature.mirek` rather than
being forced into a saturated xy point.

## Working discipline

- **Read before you write.** The bridge is the source of truth for current scene
  definitions; re-read it rather than assuming what a scene contains.
- **Back up before a write, verify after.** A scene edit should snapshot the
  prior definition, PUT the change, then re-read to confirm it landed.
- **Change definitions, flag looks.** Redefining a scene is cheap and reversible;
  whether the new look is *good* is a human judgement -- surface look changes for
  review rather than deciding them silently.
