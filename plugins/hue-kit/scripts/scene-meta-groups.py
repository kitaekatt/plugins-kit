#!/usr/bin/env python3
"""Hue scene analysis PRIMITIVES -- a shared library for the layered framework.

The reusable building blocks imported by scene-layers.py (as `smg`): bridge
I/O (clip_get), the per-light Signature model (Sig + clustering), colour math
(CIE xy / mirek -> sRGB / HSL, plus Christina's hsl(...) labels), scene
analysis (analyze_scene -> a SceneResult of colour+brightness Clusters),
light-group derivation from the bridge zones (build_groups).

READ-ONLY: only GETs against the bridge CLIP v2 API. Nothing is actuated.
This module has NO CLI -- scene-layers.py is the entry point; here live only the
primitives it composes.

Model:
  - LIGHT GROUPS come from the bridge zones: each light belongs to the
    smallest zone containing it (dedicated group zones like 'Accent Lights'
    win over aggregates like 'Main'/'Bathroom', which are reported but not
    used for grouping). Zone-less lights fall back to the "<group>-N" name
    prefix and are listed as ungrouped.
  - Lights are clustered per scene by signature closeness (tolerances below;
    snapshot-created scenes carry per-bulb jitter) -> the scene's colour cells.

Tolerances: brightness +/- 1.5 %, xy distance <= 0.006, mirek +/- 10.

Colors are reported as CSS-style hsl(hue, sat%, light%) -- Christina's
format call 2026-07-16 -- converted from the Hue-native encoding (CIE xy or
mirek), which stays available in tooltips/detail labels. The HSL value is
chromaticity only; bulb brightness remains the separate percent.
"""

from __future__ import annotations

import colorsys
import math
import os
import re
from dataclasses import dataclass, field

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Bridge address comes from HUE_BRIDGE_IP -- there is NO default (a general tool
# must not ship one home's IP). The hue-kit CLI sets it, resolving via
# `hue-kit discover` when unset. Standalone callers must export it themselves.
BRIDGE_IP = os.environ.get("HUE_BRIDGE_IP", "").strip()
BRIDGE = "https://" + BRIDGE_IP if BRIDGE_IP else ""

BRI_TOL = 1.5      # percent
XY_TOL = 0.006     # CIE xy euclidean distance
MIREK_TOL = 10     # mired


# ---------------------------------------------------------------- bridge I/O

def clip_get(session: requests.Session, resource: str) -> list[dict]:
    if not BRIDGE:
        raise SystemExit(
            "error: no bridge address -- set HUE_BRIDGE_IP=<your-bridge-ip> "
            "(or run `hue-kit discover` / `hue-kit pair`).")
    r = session.get(f"{BRIDGE}/clip/v2/resource/{resource}", timeout=10)
    r.raise_for_status()
    return r.json()["data"]


# ---------------------------------------------------------------- signatures

@dataclass
class Sig:
    """One light's state inside a scene."""
    mode: str                 # off | xy | ct | none (on, no color specified)
    bri: float | None = None  # percent
    x: float | None = None
    y: float | None = None
    mirek: float | None = None
    flags: tuple[str, ...] = ()  # gradient / effect markers

    def close(self, other: "Sig") -> bool:
        if self.mode != other.mode or self.flags != other.flags:
            return False
        if self.mode == "off":
            return True
        if (self.bri is None) != (other.bri is None):
            return False
        if self.bri is not None and abs(self.bri - other.bri) > BRI_TOL:
            return False
        if self.mode == "xy":
            return math.dist((self.x, self.y), (other.x, other.y)) <= XY_TOL
        if self.mode == "ct":
            return abs(self.mirek - other.mirek) <= MIREK_TOL
        return True


def is_dark(action: dict) -> bool:
    """The one darkness test for a bridge action: `on:false`, `on` absent
    entirely (a light the scene never addresses reads as off), or `on:true`
    at brightness 0 -- the Hue app's own way of writing "off" for some
    scenes (the bulb is nominally on but emits nothing). Every other site
    that decides whether an action is dark calls this, or compares a
    signature's `mode` to `"off"` (a mode this function's caller,
    `action_sig`, already set from the same test)."""
    if action.get("on", {}).get("on") is not True:
        return True
    bri = action.get("dimming", {}).get("brightness")
    return bri is not None and bri <= 0.0


def action_sig(action: dict) -> Sig:
    if is_dark(action):
        return Sig(mode="off")
    flags = tuple(k for k in ("gradient", "effects", "effects_v2") if k in action)
    bri = action.get("dimming", {}).get("brightness")
    color = action.get("color", {}).get("xy")
    ct = action.get("color_temperature", {}).get("mirek")
    if color is not None:
        return Sig("xy", bri, color["x"], color["y"], flags=flags)
    if ct is not None:
        return Sig("ct", bri, mirek=ct, flags=flags)
    return Sig("none", bri, flags=flags)


def mean_sig(sigs: list[Sig]) -> Sig:
    """Representative signature for a cluster (member modes already agree)."""
    first = sigs[0]
    if first.mode == "off":
        return first

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    return Sig(
        first.mode,
        avg([s.bri for s in sigs]),
        avg([s.x for s in sigs]),
        avg([s.y for s in sigs]),
        avg([s.mirek for s in sigs]),
        first.flags,
    )


# ---------------------------------------------------------------- color math

def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def xy_to_rgb(x: float, y: float) -> tuple[float, float, float]:
    """CIE xy -> sRGB (0-1 floats) at full luminance (Philips matrix)."""
    if not y:
        return (0.0, 0.0, 0.0)
    Y = 1.0
    X = (Y / y) * x
    Z = (Y / y) * (1 - x - y)
    r = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    m = max(r, g, b, 1.0)
    r, g, b = (max(0.0, min(1.0, _gamma(max(0.0, c / m))))
               for c in (r, g, b))
    return (r, g, b)


def mirek_to_rgb(mirek: float) -> tuple[float, float, float]:
    """Mired -> Kelvin -> approximate sRGB 0-1 floats (Tanner Helland)."""
    t = (1_000_000 / mirek) / 100
    if t <= 66:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0.0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * ((t - 60) ** -0.1332047592)
        g = 288.1221695283 * ((t - 60) ** -0.0755148492)
        b = 255.0
    return (max(0.0, min(1.0, r / 255)), max(0.0, min(1.0, g / 255)),
            max(0.0, min(1.0, b / 255)))


def sig_rgb(sig: Sig) -> tuple[float, float, float] | None:
    if sig.mode == "xy":
        return xy_to_rgb(sig.x, sig.y)
    if sig.mode == "ct":
        return mirek_to_rgb(sig.mirek)
    return None


def sig_hsl(sig: Sig) -> tuple[int, int, int] | None:
    """Rounded (hue, sat%, light%) of the signature's chromaticity, or None
    when it has no color (off / color-unchanged / degenerate xy)."""
    if sig.mode == "xy" and not sig.y:
        return None
    rgb = sig_rgb(sig)
    if rgb is None:
        return None
    h, l, s = colorsys.rgb_to_hls(*rgb)
    return (round(h * 360) % 360, round(s * 100), round(l * 100))


def sig_color_label(sig: Sig) -> str:
    """Primary color representation: HSL (Christina's format call,
    2026-07-16). Chromaticity only -- the bulb brightness is the separate
    scene/meta-group percent, NOT the L channel."""
    hsl = sig_hsl(sig)
    if hsl is not None:
        return f"hsl({hsl[0]}, {hsl[1]}%, {hsl[2]}%)"
    if sig.mode == "xy":
        return "invalid"  # degenerate chromaticity; native label follows
    return "off" if sig.mode == "off" else "color unchanged"


def sig_native_label(sig: Sig) -> str:
    """The Hue-native encoding, kept for auditability (tooltips/detail)."""
    if sig.mode == "xy":
        return f"xy({sig.x:.4f}, {sig.y:.4f})"
    if sig.mode == "ct":
        return f"{round(1_000_000 / sig.mirek)}K ({round(sig.mirek)} mirek)"
    return ""


def sig_label(sig: Sig) -> str:
    parts = [sig_color_label(sig)]
    native = sig_native_label(sig)
    if native:
        parts.append(native)
    if sig.mode != "off" and sig.bri is not None:
        parts.append(f"{sig.bri:.0f}%")
    parts.extend(sig.flags)
    return "  ".join(parts)


# ---------------------------------------------------------------- clustering

def _tighten(items: list, sig_of, close) -> list[list]:
    """Split `items` until every member is close() to its OWN cluster's final
    mean. A running-mean accumulation (as greedy_clusters builds one) can
    accept a member against an early mean that later members pull away from,
    so the cluster's finished mean can end up farther than tolerance from a
    member even though every step along the way looked fine. Recompute the
    mean, peel off whichever members disagree with it, and recurse on
    both halves -- this converges (each recursion strictly shrinks the
    non-agreeing side, and a single item is trivially close to its own
    mean), and it is what makes the emitted representative provably within
    tolerance of every member, not just of the item that triggered its
    merge."""
    if len(items) <= 1:
        return [items] if items else []
    rep = mean_sig([sig_of(it) for it in items])
    bad_ids = {id(it) for it in items if not close(rep, sig_of(it))}
    if not bad_ids:
        return [items]
    good = [it for it in items if id(it) not in bad_ids]
    bad = [it for it in items if id(it) in bad_ids]
    if not good:  # degenerate safety net; a lone item always agrees with itself
        return [[it] for it in items]
    return _tighten(good, sig_of, close) + _tighten(bad, sig_of, close)


def greedy_clusters(items: list, sig_of, close) -> list[list]:
    """Cluster by closeness to the running cluster MEAN, then merge clusters
    whose means end up within tolerance -- resistant to input-order effects
    at tolerance boundaries (input is pre-sorted for determinism). A final
    tightening pass (`_tighten`) guarantees the property the callers rely on:
    every member ends up close() to the cluster's own finished mean -- the
    representative later gets emitted from that mean, so a member cannot
    drift outside tolerance of what is actually written out."""
    clusters: list[list] = []
    reps: list[Sig] = []
    for item in items:
        s = sig_of(item)
        for i, rep in enumerate(reps):
            if close(rep, s):
                clusters[i].append(item)
                reps[i] = mean_sig([sig_of(it) for it in clusters[i]])
                break
        else:
            clusters.append([item])
            reps.append(s)
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if close(reps[i], reps[j]):
                    clusters[i].extend(clusters[j])
                    reps[i] = mean_sig([sig_of(it) for it in clusters[i]])
                    del clusters[j], reps[j]
                    merged = True
                    break
            if merged:
                break
    return [tight for cluster in clusters
            for tight in _tighten(cluster, sig_of, close)]


GROUP_SUFFIX = re.compile(r"^(.*?)-\d+$")


def light_group(name: str) -> str:
    m = GROUP_SUFFIX.match(name)
    return m.group(1) if m else name


def build_groups(zones: list[dict], lights: dict[str, str]
                 ) -> tuple[dict[str, list[str]], list[str], list[str]]:
    """Light groups from bridge zones. A zone is an AGGREGATE if any of its
    lights sits in a strictly smaller zone ('Main', 'Bathroom') -- reported
    but never used for grouping. Each light joins its smallest non-aggregate
    zone (alphabetical on a size tie; an unchosen duplicate is reported as
    overlapping). Zone-less lights fall back to the <group>-N name prefix
    (their own name if that would collide with a zone name) and are listed
    as ungrouped. Returns (group -> sorted member names, ungrouped light
    names, zone names not used for grouping)."""
    zsets = [(z["metadata"]["name"],
              {lights[c["rid"]] for c in z["children"] if c["rid"] in lights})
             for z in zones]
    zone_names = {zn for zn, _ in zsets}

    def smallest(n: str) -> int:
        return min((len(ls) for _, ls in zsets if n in ls), default=0)

    aggregates = {zn for zn, ls in zsets
                  if ls and any(smallest(n) < len(ls) for n in ls)}
    members: dict[str, list[str]] = {}
    ungrouped: list[str] = []
    chosen: set[str] = set()
    for name in sorted(set(lights.values())):
        containing = sorted((len(ls), zn) for zn, ls in zsets
                            if name in ls and zn not in aggregates)
        if containing:
            zn = containing[0][1]
            chosen.add(zn)
            members.setdefault(zn, []).append(name)
        else:
            ungrouped.append(name)
            key = light_group(name)
            if key in zone_names:  # never merge a stray into a zone group
                key = name
            members.setdefault(key, []).append(name)
    unused = sorted(aggregates | ({zn for zn, ls in zsets if ls}
                                  - chosen - aggregates))
    return members, ungrouped, unused


# ---------------------------------------------------------------- analysis

@dataclass
class Cluster:
    lights: tuple[str, ...]   # sorted light names
    sig: Sig                  # mean signature


@dataclass
class SceneResult:
    name: str
    owner: str
    clusters: list[Cluster] = field(default_factory=list)  # on by -bri, off last
    scale: float = 0.0        # brightest cluster's absolute brightness


def analyze_scene(scene: dict, lights: dict[str, str],
                  members: dict[str, list[str]], owner: str) -> SceneResult:
    pairs = sorted(
        ((lights[a["target"]["rid"]], action_sig(a["action"]))
         for a in scene.get("actions", []) if a["target"]["rid"] in lights),
        key=lambda p: p[0])
    raw = greedy_clusters(pairs, lambda p: p[1], lambda a, b: a.close(b))
    clusters = [Cluster(tuple(sorted(n for n, _ in c)),
                        mean_sig([s for _, s in c]))
                for c in raw]
    clusters.sort(key=lambda c: (c.sig.mode == "off", -(c.sig.bri or 0),
                                 c.lights))
    res = SceneResult(scene["metadata"]["name"], owner, clusters)
    on = [c for c in clusters if c.sig.mode != "off" and c.sig.bri is not None]
    res.scale = max((c.sig.bri for c in on), default=0.0)
    return res
