#!/usr/bin/env python3
"""Hue scene analysis PRIMITIVES -- a shared library for the layered framework.

The reusable building blocks imported by scene-layers.py (as `smg`): bridge
I/O (clip_get), the per-light Signature model (Sig + clustering), colour math
(CIE xy / mirek -> sRGB / HSL, plus Christina's hsl(...) labels), scene
analysis (analyze_scene -> a SceneResult of colour+brightness Clusters),
light-group derivation from the bridge zones (build_groups), and the HTML
rendering primitives (REPORT_CSS, swatch, scene_bar_svg) together with the
layered report renderer (layered_report).

READ-ONLY: only GETs against the bridge CLIP v2 API. Nothing is actuated.
This module has NO CLI -- scene-layers.py is the entry point and the single
report generator; here live only the primitives it composes.

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
import html
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


def action_sig(action: dict) -> Sig:
    on = action.get("on", {}).get("on")
    if on is False:
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


def sig_hex(sig: Sig) -> str:
    rgb = sig_rgb(sig)
    if rgb is not None:
        return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)
    return "#2a2a2a" if sig.mode == "off" else "#c8c8a0"


BRI_RENDER_FLOOR = 0.25  # a 1%-bright bulb still renders at 25% so it stays
                         # visible; bulb brightness maps [0,100]% -> [floor,1].


def sig_hex_bri(sig: Sig) -> str:
    """Hex of the signature's colour scaled by a rendered-brightness factor, so
    a dim scene renders darker (a brightness visualisation) WITHOUT vanishing:
    bulb brightness is remapped [0,100]% -> [BRI_RENDER_FLOOR, 1] (perceptually
    1% is nowhere near 1% lightness). bri None -> full; off stays the 'off' swatch."""
    rgb = sig_rgb(sig)
    if rgb is None:
        return "#2a2a2a" if sig.mode == "off" else "#c8c8a0"
    b = 1.0 if sig.bri is None else max(0.0, min(1.0, sig.bri / 100.0))
    f = BRI_RENDER_FLOOR + (1.0 - BRI_RENDER_FLOOR) * b
    return "#%02x%02x%02x" % tuple(round(c * f * 255) for c in rgb)


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

def greedy_clusters(items: list, sig_of, close) -> list[list]:
    """Cluster by closeness to the running cluster MEAN, then merge clusters
    whose means end up within tolerance -- resistant to input-order effects
    at tolerance boundaries (input is pre-sorted for determinism)."""
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
    return clusters


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


# ---------------------------------------------------------------- reporting

def swatch(sig: Sig, size: int = 18, dim: bool = False) -> str:
    """A colour square. dim=True scales the colour by the bulb brightness (a
    brightness visualisation); default renders the colour at full luminance."""
    color = sig_hex_bri(sig) if dim else sig_hex(sig)
    return (f'<span class="sw" style="background:{color};'
            f'width:{size}px;height:{size}px" '
            f'title="{html.escape(sig_label(sig))}"></span>')


def _short_group(name: str) -> str:
    """Drop the redundant ' Lights'/' Light' suffix -- every group is lights."""
    for suf in (" Lights", " Light"):
        if name.endswith(suf):
            return name[:-len(suf)]
    return name


def _bar_geometry(structure, light2group, bar_w):
    """Lay a template's meta-group structure out along a bar of width bar_w:
    one box per meta-group (width proportional to its light count), each box
    subdivided by light group (zone). Returns (segments, meta_boxes) where a
    segment is (x, w, meta_index, zone_name, count) and a meta_box is
    (x, w, meta_index). Deterministic, so a template's header bar and every one
    of its scene bars share identical geometry and line up exactly."""
    total = sum(len(lts) for lts, _ in structure) or 1
    ppl = bar_w / total                    # px per light
    segs, boxes, x = [], [], 0.0
    for mi, (lts, _rel) in enumerate(structure):
        counts: dict[str, int] = {}
        for n in lts:
            g = light2group.get(n, "ungrouped")
            counts[g] = counts.get(g, 0) + 1
        mstart = x
        for g, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            w = n * ppl
            segs.append((x, w, mi, g, n))
            x += w
        boxes.append((mstart, x - mstart, mi))
    return segs, boxes


def scene_bar_svg(structure, light2group, fill_of, *, labels, label_of=None):
    """One proportional bar for a template's structure. fill_of(meta_index)->CSS
    colour fills each meta-group box; when `labels`, each zone segment is tagged
    with its (shortened) name ALTERNATING above/below the bar (so 1-light zones,
    too narrow to label inside, stay legible). Meta-group boxes get a thicker
    outer border; zones a thin internal divider. Half-height, per Christina."""
    BAR_W, MARGIN, BAR_H = 760.0, 60.0, 16.0
    band = 32.0 if labels else 3.0
    bar_top, bar_left = band, MARGIN
    svg_w = BAR_W + 2 * MARGIN
    svg_h = band + BAR_H + band
    segs, boxes = _bar_geometry(structure, light2group, BAR_W)
    s = [f"<svg class='scenebar' viewBox='0 0 {svg_w:.0f} {svg_h:.0f}' "
         "preserveAspectRatio='xMidYMid meet' role='img'>"]
    for i, (x, w, mi, g, n) in enumerate(segs):
        tip = f"{html.escape(g)} &mdash; {n} light{'s' if n != 1 else ''}"
        if label_of:
            tip += " &mdash; " + html.escape(label_of(mi))
        s.append(f"<rect x='{bar_left + x:.2f}' y='{bar_top:.1f}' "
                 f"width='{w:.2f}' height='{BAR_H:.1f}' fill='{fill_of(mi)}' "
                 f"stroke='#00000055' stroke-width='0.5'><title>{tip}</title>"
                 "</rect>")
        if labels:
            cx = bar_left + x + w / 2
            if i % 2 == 0:                 # above
                ty, y0, y1, base = bar_top - 8, bar_top, bar_top - 6, \
                    "text-after-edge"
            else:                          # below
                ty, y0, y1, base = bar_top + BAR_H + 15, bar_top + BAR_H, \
                    bar_top + BAR_H + 6, "text-before-edge"
            s.append(f"<line x1='{cx:.2f}' y1='{y0:.1f}' x2='{cx:.2f}' "
                     f"y2='{y1:.1f}' stroke='#6a6a76' stroke-width='0.75'/>")
            s.append(f"<text x='{cx:.2f}' y='{ty:.1f}' text-anchor='middle' "
                     f"dominant-baseline='{base}' font-size='10'>"
                     f"{html.escape(_short_group(g))}</text>")
    for mx, mw, mi in boxes:
        s.append(f"<rect x='{bar_left + mx:.2f}' y='{bar_top:.1f}' "
                 f"width='{mw:.2f}' height='{BAR_H:.1f}' fill='none' "
                 "stroke='#0c0c10' stroke-width='2'/>")
    s.append("</svg>")
    return "".join(s)


REPORT_CSS = """
    body { font-family: -apple-system, Segoe UI, sans-serif; margin: 2rem;
           background: #16161d; color: #e8e8ee; }
    h1 { font-size: 1.4rem; } h2 { font-size: 1.15rem; margin-top: 2rem; }
    h3 { font-size: 1rem; margin-top: 1.2rem; }
    table { border-collapse: collapse; margin: .6rem 0; }
    th, td { border: 1px solid #3a3a46; padding: .35rem .6rem; text-align: left;
             font-size: .85rem; vertical-align: top; }
    th { background: #23232e; }
    .sw { display: inline-block; border-radius: 4px; border: 1px solid #555;
          vertical-align: middle; }
    .dim { color: #9a9aa8; } .mono { font-family: ui-monospace, monospace; }
    .off { color: #777; }
    td.c { text-align: center; }
    .note { color: #9a9aa8; font-size: .8rem; max-width: 60rem; }
    .tbadge { background: #2e2e4e; border-radius: 6px; padding: .1rem .45rem;
              font-size: .8rem; }
    svg.scenebar { display: block; width: 100%; max-width: 60rem; height: auto; }
    svg.scenebar text { fill: #cfd0da; font-family: -apple-system, Segoe UI,
                        sans-serif; }
    .sname { font-size: .82rem; margin: .55rem 0 .05rem; }
    .swatches { font-size: .76rem; margin: .05rem 0 .1rem; }
    .swatches .sw { vertical-align: middle; margin-right: .2rem; }
    """


def _zones_of(lights, members):
    """The bridge zones whose light-sets are fully contained in `lights`
    (largest first) -- the human-readable definition of a meta-group."""
    lset = set(lights)
    zs = [(g, mem) for g, mem in members.items() if set(mem) <= lset]
    zs.sort(key=lambda gm: (-len(gm[1]), gm[0]))
    return [g for g, _ in zs]


# An overlay showing the config + source inline (no new page, no external
# library), so the report doubles as a self-contained buildable spec: syntax
# highlight for YAML + Python (via tokenize), a small TOC, minimal inline JS.
OVERLAY_CSS = """
    .yaml-link { display: inline-block; margin: .2rem 0 1rem; padding: .35rem .8rem;
      background: #23232e; border: 1px solid #3a3a46; border-radius: 6px;
      color: #c9d4ff; text-decoration: none; font-size: .85rem; cursor: pointer; }
    .yaml-link:hover { background: #2c2c3a; }
    .overlay { position: fixed; inset: 0; z-index: 100; display: none; }
    .overlay .backdrop { position: absolute; inset: 0; display: block;
      background: rgba(0,0,0,.72); }
    .overlay .sheet { position: relative; max-width: 68rem; margin: 3vh auto;
      max-height: 94vh; overflow: auto; background: #1b1b24;
      border: 1px solid #3a3a46; border-radius: 10px;
      box-shadow: 0 12px 44px #000a; }
    .overlay .sheethead { position: sticky; top: 0; z-index: 2;
      background: #1b1b24; padding: .8rem 1.4rem 0; border-radius: 10px 10px 0 0; }
    .overlay .close { float: right; color: #b7b7c6; text-decoration: none;
      font-size: 1.5rem; line-height: 1; cursor: pointer; }
    .overlay h2 { margin: .1rem 0 .5rem; }
    .overlay pre { background: #111119; border: 1px solid #2a2a36;
      border-radius: 6px; padding: .8rem 1rem; overflow-x: auto; margin: 0;
      font-family: ui-monospace, monospace; font-size: .76rem; line-height: 1.5;
      color: #d6d7e2; white-space: pre; }
    .tabs { display: flex; flex-wrap: wrap; gap: .35rem;
      border-bottom: 1px solid #2a2a36; }
    .tab { padding: .4rem .85rem; background: #23232e; border: 1px solid #3a3a46;
      border-bottom: none; border-radius: 6px 6px 0 0; color: #c9d4ff;
      cursor: pointer; font-size: .82rem; }
    .tab.active { background: #111119; color: #fff; }
    .doc { display: none; padding: 1rem 1.4rem 1.4rem; }
    .doc.active { display: block; }
    .y-key { color: #8fb7ff; } .y-val { color: #cfe6a6; }
    .y-com, .py-com { color: #7f7f8e; font-style: italic; }
    .py-kw { color: #c792ea; } .py-str { color: #c3e88d; }
    .py-num { color: #f78c6c; } .py-def { color: #82aaff; }
    """


def _yaml_highlight(text: str) -> str:
    """Minimal dependency-free YAML -> highlighted HTML (keys / values /
    comments). Good enough to read; not a full parser."""
    lines = []
    for raw in text.split("\n"):
        if raw.lstrip().startswith("#"):
            lines.append(f"<span class='y-com'>{html.escape(raw)}</span>")
            continue
        code, comment = raw, ""
        idx = raw.find(" #")
        if idx != -1:
            code, comment = raw[:idx], raw[idx:]
        m = re.match(r"^(\s*(?:- )?)([\w\- ]+?)(:)(\s.*|)$", code)
        if m:
            indent, key, colon, val = m.groups()
            line = (html.escape(indent)
                    + f"<span class='y-key'>{html.escape(key)}</span>{colon}"
                    + f"<span class='y-val'>{html.escape(val)}</span>")
        else:
            line = html.escape(code)
        if comment:
            line += f"<span class='y-com'>{html.escape(comment)}</span>"
        lines.append(line)
    return "\n".join(lines)


def _py_highlight(src: str) -> str:
    """Python -> highlighted HTML using the stdlib tokenizer, so multi-line
    strings / f-strings / comments are correct. Falls back to plain-escaped
    text if the source does not tokenize."""
    import io
    import keyword
    import tokenize
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return html.escape(src)
    lines = src.splitlines(keepends=True)

    def sub(r1, c1, r2, c2):  # exact text between two (row, col) positions
        if r1 == r2:
            return lines[r1 - 1][c1:c2] if r1 - 1 < len(lines) else ""
        parts = [lines[r1 - 1][c1:]]
        parts.extend(lines[r - 1] for r in range(r1 + 1, r2))
        if r2 - 1 < len(lines):
            parts.append(lines[r2 - 1][:c2])
        return "".join(parts)

    fstr = {getattr(tokenize, n) for n in
            ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
            if hasattr(tokenize, n)}
    out, lr, lc, prev = [], 1, 0, ""
    for ttype, tstr, (sr, sc), (er, ec), _ln in toks:
        out.append(html.escape(sub(lr, lc, sr, sc)))
        cls = ""
        if ttype == tokenize.COMMENT:
            cls = "py-com"
        elif ttype == tokenize.STRING or ttype in fstr:
            cls = "py-str"
        elif ttype == tokenize.NUMBER:
            cls = "py-num"
        elif ttype == tokenize.NAME:
            if keyword.iskeyword(tstr):
                cls = "py-kw"
            elif prev in ("def", "class"):
                cls = "py-def"
        esc = html.escape(tstr)
        out.append(f"<span class='{cls}'>{esc}</span>" if cls else esc)
        lr, lc = er, ec
        if tstr.strip():
            prev = tstr
    return "".join(out)


def _highlight(text: str, lang: str) -> str:
    return _py_highlight(text) if lang == "python" else _yaml_highlight(text)


def layered_report(family, scenes, members, source_docs=None):
    """Render the LAYERED scene report (the single renderer for the layered
    model, reusing REPORT_CSS + swatch + scene_bar_svg). Inputs are plain data
    assembled by scene-layers.py (no solver types here):
      family = [(group_name, [lights...]), ...]  (largest first)
      scenes = [{name, owner, template:(gname,...), layers:[(gname, Sig)],
                 clusters:[([lights], Sig), ...]}]  (layers bottom->top;
                 clusters = the baked colour partition of the scene)
      members = {zone: [lights]}
      source_docs = optional [(title, text, lang), ...] (lang: yaml|python) --
                  shown inline in an overlay behind a 'View config & source'
                  link, so the report doubles as a self-contained spec.
    Dropped vs the partition report (Christina, 2026-07-18): the meta-group
    catalog, the templates/relative-brightness table, and the groups x scenes
    matrix."""
    light2group = {n: g for g, ns in members.items() for n in ns}
    out = ["<meta charset='utf-8'><title>Hue scenes -- layered</title>",
           f"<style>{REPORT_CSS}{OVERLAY_CSS}</style>",
           "<h1>Hue scenes &mdash; layered model</h1>",
           "<p class='note'>Each scene is an ordered stack of <b>layers</b> "
           "painted over a default of OFF; the <b>topmost</b> layer covering a "
           "light wins. Layers paint the reusable <b>meta-groups</b> below; a "
           "lower layer's group may be a superset that higher layers overpaint. "
           "Colours are hsl(hue, sat%, light%) chromaticity + a separate "
           "brightness %; hover a swatch for the Hue-native xy/Kelvin. "
           "READ-ONLY, generated by scene-layers.py.</p>"]
    if source_docs:
        out.append("<p><a class='yaml-link' onclick=\"document."
                   "getElementById('srcmodal').style.display='block';"
                   "showDoc(0)\">&#9776; View config &amp; source</a></p>")

    out.append(f"<h2>Meta-group family <span class='dim'>({len(family)} groups "
               "&mdash; certified minimum)</span></h2>")
    out.append("<table><tr><th>Group</th><th>Lights</th><th>Zones</th></tr>")
    for name, lts in family:
        zs = _zones_of(lts, members)
        out.append(f"<tr><td><b>{html.escape(name)}</b></td>"
                   f"<td class='c'>{len(lts)}</td>"
                   f"<td class='dim'>{html.escape(', '.join(zs))}</td></tr>")
    out.append("</table>")

    out.append("<h2>Templates <span class='dim'>(scenes by layer stack)"
               "</span></h2>")
    by_tpl: dict[tuple, list] = {}
    for sc in scenes:
        by_tpl.setdefault(sc["template"], []).append(sc)
    # templates run simplest -> most elaborate: fewest layers first, so the
    # section reads as a build-up (all-off, then one-layer washes, then the
    # multi-layer stacks). Ties break on scene count, then name for determinism.
    for tpl, scs in sorted(by_tpl.items(),
                           key=lambda kv: (len(kv[0]), -len(kv[1]), kv[0])):
        seq = " &rarr; ".join(html.escape(g) for g in tpl) or "(all off)"
        tname = scs[0].get("template_name")
        head = html.escape(tname) if tname else seq
        sub = f"{seq} &mdash; " if tname else ""
        out.append(f"<h3>{head} <span class='dim'>&mdash; {sub}{len(scs)} "
                   f"scene{'s' if len(scs) != 1 else ''}</span></h3>")
        # within a template, order the scenes by lightness then hue -- so the
        # brightness-scaled swatches read as a dark -> light progression. The
        # sort key is the scene's base (bottom) layer.
        def _lightness_hue(sc):
            if not sc["layers"]:
                return (1e9, 1e9)
            sig = sc["layers"][0][1]
            bri = sig.bri if sig.bri is not None else 100.0
            hsl = sig_hsl(sig)
            return (bri, hsl[0] if hsl else 1e9)
        for sc in sorted(scs, key=_lightness_hue):
            legend = []
            for g, sig in sc["layers"]:
                bri = (f" &middot; {sig.bri:.0f}%" if sig.mode != "off"
                       and sig.bri is not None else "")
                legend.append(
                    f"{swatch(sig, 14, dim=True)} "
                    f"<span class='mono'>{html.escape(g)}</span>"
                    f" <span class='dim mono'>{sig_color_label(sig)}{bri}</span>")
            legend_html = (" &nbsp; ".join(legend)
                           or "<span class='off'>all off</span>")
            out.append(f"<div class='sname'>{html.escape(sc['name'])} "
                       f"<span class='dim'>{html.escape(sc['owner'])}</span> "
                       f"&nbsp; <span class='swatches'>{legend_html}</span></div>")
            structure = [(tuple(lts), "") for lts, _ in sc["clusters"]]
            sigs = [sig for _, sig in sc["clusters"]]
            out.append(scene_bar_svg(
                structure, light2group,
                lambda mi, sigs=sigs: sig_hex_bri(sigs[mi]), labels=True))

    # palette: every colour used by any scene, one row per hue
    pal: dict[int, dict[tuple[int, int], tuple[Sig, set[str]]]] = {}
    for sc in scenes:
        for _lts, sig in sc["clusters"]:
            hsl = sig_hsl(sig)
            if hsl is None:
                continue
            h, sat, lig = hsl
            _sig, names = pal.setdefault(h, {}).setdefault(
                (lig, sat), (sig, set()))
            names.add(sc["name"])
    out.append("<h2>Palette</h2><table><tr><th>Hue</th><th>Colours</th>"
               "<th>Used by</th></tr>")
    for h in sorted(pal):
        variants = sorted(pal[h].items())
        cells = " &nbsp; ".join(
            f"{swatch(sig)} <span class='mono'>{sig_color_label(sig)}</span>"
            for _, (sig, _) in variants)
        used = sorted({sc for _, (_, names) in variants for sc in names})
        out.append(f"<tr><td>{h}</td><td>{cells}</td>"
                   f"<td>{html.escape(', '.join(used))}</td></tr>")
    out.append("</table>")

    # appendix: which lights are in each bridge zone (the building blocks the
    # meta-groups above are unions of)
    out.append("<h2>Appendix &mdash; light groups "
               "<span class='dim'>(Hue bridge zones)</span></h2>")
    out.append("<table><tr><th>Group</th><th>Lights</th></tr>")
    for grp, mem in sorted(members.items()):
        out.append(f"<tr><td>{html.escape(grp)}</td>"
                   f"<td class='dim'>{html.escape(', '.join(mem))}</td></tr>")
    out.append("</table>")

    # Overlay showing the config + source inline (no new page). The top row is
    # a TAB SWITCHER: one document visible at a time (showDoc). Minimal inline
    # JS -- this is a local file, not an Artifact.
    if source_docs:
        tabs = "".join(
            f"<span class='tab' onclick='showDoc({i})'>{html.escape(title)}"
            "</span>" for i, (title, _t, _l) in enumerate(source_docs))
        secs = "".join(
            f"<div class='doc' id='src{i}'><pre>{_highlight(text, lang)}</pre>"
            "</div>" for i, (_title, text, lang) in enumerate(source_docs))
        hide = "document.getElementById('srcmodal').style.display='none'"
        script = ("function showDoc(i){var d=document.querySelectorAll("
                  "'#srcmodal .doc');for(var k=0;k<d.length;k++)"
                  "d[k].style.display=(k==i?'block':'none');"
                  "var t=document.querySelectorAll('#srcmodal .tab');"
                  "for(var k=0;k<t.length;k++)t[k].className="
                  "(k==i?'tab active':'tab');}")
        out.append(
            "<div id='srcmodal' class='overlay'>"
            f"<div class='backdrop' onclick=\"{hide}\"></div>"
            "<div class='sheet'>"
            f"<div class='sheethead'><a class='close' onclick=\"{hide}\">"
            "&times;</a><h2>Lighting configuration &amp; source</h2>"
            f"<div class='tabs'>{tabs}</div></div>{secs}"
            f"<script>{script}</script></div></div>")

    return "".join(out)
