# Hue beyond scene definitions

This plugin's core surface is scene DEFINITIONS: read the bridge, solve a
meta-group vocabulary, sync YAML, write scenes back. That surface deliberately
excludes runtime behaviour. This reference collects the bridge facts that sit
just outside it -- the ones you need when something asks "why didn't my client
notice?", "why is this laggy?", or "how do I rename a bulb?".

Nothing here is required to author scenes. It is here so the knowledge has a
home instead of being rediscovered the hard way.

## The event stream (`/eventstream/clip/v2`)

The bridge pushes state changes over Server-Sent Events. Subscribe with the
usual `hue-application-key` header plus `Accept: text/event-stream`.

**The trap: there is no periodic keepalive.** The bridge sends a single `: hi`
comment at connect and then NOTHING until a real event occurs. A quiet house can
idle for hours.

That matters because a silently-dropped TCP connection is otherwise never
noticed. A naive `for line in stream:` blocks forever with no data, the
reconnect loop never runs, and the client sits there looking connected while
being deaf to every change. This failure is invisible from the outside -- the
process is healthy, the socket is "open", and no error is ever raised.

An application-level read timeout cannot distinguish "idle" from "dead" here,
because idle is indefinite and legitimate. The fix is OS-level dead-peer
detection, so the kernel probes during idle and fails the read only when the
peer is genuinely gone:

```python
sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,  60)  # probe after 60s quiet
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 15)  # then every 15s
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT,    4)  # dead after 4 misses
```

`TCP_KEEPIDLE`/`TCP_KEEPINTVL`/`TCP_KEEPCNT` are not available on every platform
-- guard each with `hasattr(socket, opt)` and keep a long read timeout (e.g.
3600s) as a backstop so a host lacking them degrades to a harmless periodic
reconnect rather than permanent deafness.

**Scene activation status.** Scene events carry a `status` of `static` or
`dynamic_palette` when a scene becomes active. This is the only reliable way to
observe a scene being activated from OUTSIDE your own client -- the Hue app, a
Hue dimmer switch, or a voice assistant going through the bridge directly. Any
integration that only watches its own commands will miss all of those.

## Throughput: the REST API is not a real-time channel

The normal REST/CLIP API is rate-limited to roughly **10 requests/second
bridge-wide** -- not per light, not per client. Anything built on it that tries
to animate lights (beat-flashing, video-following, chase effects) is laggy by
construction and gets worse as the light count grows.

Real-time light driving goes through the **Entertainment streaming API**: CLIP
v2 `entertainment_configuration` resources plus a **DTLS/UDP stream on port
2100**. That is a genuinely different channel with a different protocol, not a
faster REST endpoint. If a request needs lights to track audio or video, the
answer is Entertainment or nothing; do not attempt it over REST.

This plugin does not implement Entertainment. Existing clients that do:
`hue-entertainment-pykit`, `aiohue`'s entertainment models, HyperHDR.

## Zigbee group commands can silently miss a bulb

A command addressed to a group is a single Zigbee multicast, not N unicasts. An
individual bulb can simply not receive it -- RF interference, a congested mesh, a
bulb that just repowered. The bridge does NOT detect this: it reports success
optimistically because it sent the multicast, and its own cached state for the
missed bulb is wrong until something refreshes it.

The consequence for diagnosis: querying the bulb's state through the bridge is
not evidence of anything, because you may be reading the same optimistic cache
that is already wrong. Re-send the group command instead. If misses recur in one
area, that is a mesh-health problem (distance, interference, too few routers),
not a bug in whatever issued the command.

## Renaming lights

The naming guardrail in SKILL.md says not to invent names for meta-groups. That
is about the LOCAL group vocabulary. Renaming an actual bulb on the bridge is a
different thing, and this is the call:

```
PUT /clip/v2/resource/light/<id>
{"metadata": {"name": "kitchen-3"}}
```

The name is bridge state, so every client sees it. Group/zone names are renamed
the same way on their own resource type. Meta-group names in `scene-groups.yaml`
are local to this tool and are NEVER written to the bridge.

## Colour: mirek -> xy

Scenes hold tunable white as `color_temperature.mirek` and colour as
`color.xy`. To convert a mirek value into an xy point (e.g. to compare a white
scene against a coloured one, or to drive a non-Hue device from a Hue scene),
use the **Kang et al. approximation**: mirek -> kelvin (`1e6 / mirek`), then the
piecewise-cubic Kang polynomials for chromaticity x and y.

Keep this one-directional. Converting xy back into mirek is lossy and mostly
meaningless -- an arbitrary xy point is not on the blackbody locus at all. The
model's rule stands: a tunable-white light stays `ct`, and is never rewritten
into a saturated xy point just because a conversion exists.

## Not covered anywhere in this plugin

Sensors (motion / tap / dial), rules, schedules, behaviour instances, CLIP v1,
the remote/cloud API, bridge firmware, backup/restore, factory reset, and
multi-bridge management. If a task needs those, it is outside hue-kit and you
are working against the raw CLIP API.
