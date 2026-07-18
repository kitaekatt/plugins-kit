#!/usr/bin/env python3
"""
Layered / painter's-algorithm META-GROUP solver for the Hue scene framework.

Reads the live bridge (via the scene-meta-groups.py analyzer), decomposes every
scene into colour+brightness CELLS, then computes the SMALLEST global family F of
META-GROUPS (light-subsets) such that EVERY scene can be rendered as an ordered
stack of LAYERS painted over a default of OFF -- each layer = (group in F, a paint
value), every light taking the value of the TOPMOST layer whose group contains it.

The point of the layered model: a lower layer's group may be a SUPERSET of the
cell it ultimately shows, because higher layers overpaint the excess. So one big
base group such as ALL(42) is shared across scenes and every "everything-except-X"
group vanishes -- e.g. `AllExceptKitchen(red) + Kitchen(green)` becomes the
identical bake of `All(red) + Kitchen(green)`, so only ALL + Kitchen need naming.

    usage:
      scene-layers.py                 # read live bridge, print the report
      scene-layers.py --cells P.json  # solve an offline export instead of the bridge
      scene-layers.py --json [--out P] # emit machine-readable result (for tooling)
      scene-layers.py --export-cells P # write the live per-scene cells to P (no solve)

The solver is parameterised entirely by the input data (no constants tied to
today's scenes): re-run it whenever scenes are added or restyled to get the new
minimal vocabulary + per-scene layer stacks.

------------------------------------------------------------------------------
ALGORITHM  (the reusable artifact)
------------------------------------------------------------------------------
Universe U of lights. Scene S has disjoint lit cells c_1..c_m and an off set;
L_S = union of lit cells.

1. EXPRESSIBILITY CHECKER (necessary & sufficient). S is expressible by family F
   iff we can pick, for each lit cell c_i, a group g_i in F with

        c_i  SUBSET-OF  g_i  SUBSET-OF  L_S                         (*)

   AND the induced "must-be-above" relation is ACYCLIC (cell j must lie strictly
   above cell i whenever g_i meets c_j, j != i). A bottom->top layer order is any
   topological order. Why (*) is tight:
     - g_i SUBSET L_S => no group touches an OFF light, so OFF is handled by the
       default alone -- no OFF-painting layer is ever needed.
     - for x in c_i, no higher group can contain x (it would break that group's
       own containment), so x keeps c_i's value -> the bake is exact.
   One layer per cell always suffices; multi-layer-per-cell never lowers |F|.

2. COMPLETE CANDIDATE POOL = unions of ATOMS. Atoms = the common refinement of
   every scene's partition (its cells + its off set). Lights inside one atom
   behave identically in every scene, so WLOG every meta-group is a union of
   atoms (rounding a group up to atom boundaries preserves (*) in every scene at
   once). Search space is then 2^(#atoms), small in practice.

3. MINIMISE |F| by monotone iterative-deepening DFS, seeded with a fast
   union-of-cells upper bound and pruned by an admissible conflict lower bound.
   Feasibility is monotone (adding groups never hurts), so the search is complete
   and the returned size is a certified minimum.

4. VERIFY by baking each scene from its computed stack and asserting equality
   with the ground-truth cells/off.

5. INTERPRETABILITY: optionally restrict F to unions of whole named zones and
   report the extra cost (0 on today's data -- the optimum is already nameable).
------------------------------------------------------------------------------
HTML RENDERING -- READ BEFORE TOUCHING --html  (repeated-mistake guardrail)
------------------------------------------------------------------------------
There is ONE renderer for these reports: scene-meta-groups.py's layered_report()
(it owns REPORT_CSS, swatch(), and the bar/table markup). It is the single
source of truth for the report's look.

So any HTML this file emits MUST go through smg.layered_report(): assemble the
plain report data (family + per-scene layer stacks + baked colour clusters) and
hand it to it -- that is exactly what layered_view() does below.

Do NOT (this has regressed several times): hand-roll a parallel renderer here,
copy/duplicate REPORT_CSS or swatch(), or emit your own <table> layout by
borrowing only the CSS. Every one of those makes this report visually DRIFT from
the familiar report. If layered_report() needs to render something new, extend
it in scene-meta-groups.py so both call sites benefit.
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SCRIPT_DIR = Path(__file__).resolve().parent
KEY_FILE = Path("secrets/hue-bridge-key.txt")


def _cfg_file(name, env_var):
    """Resolve a config YAML across layouts: an env override, else the source
    repo's ../references/<name>, else <name> next to this script (flat/pastebin
    layout)."""
    override = os.environ.get(env_var)
    if override:
        return Path(override).expanduser()
    ref = SCRIPT_DIR.parent / "references" / name
    return ref if ref.exists() else SCRIPT_DIR / name


GROUPS_YAML = _cfg_file("scene-groups.yaml", "HUE_GROUPS_FILE")
DESIGNS_YAML = _cfg_file("scene-designs.yaml", "HUE_DESIGNS_FILE")


def _load_analyzer():
    """Import the hyphen-named analyzer module and return it."""
    spec = importlib.util.spec_from_file_location(
        "scene_meta_groups", SCRIPT_DIR / "scene-meta-groups.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scene_meta_groups"] = mod
    spec.loader.exec_module(mod)
    return mod


smg = _load_analyzer()


# ========================================================================
# Live-bridge extraction  ->  the same data shape the solver consumes
# ========================================================================
def bridge_session() -> requests.Session:
    """A CLIP v2 session. The application key comes from (in order) the
    HUE_APP_KEY env var, the HUE_KEY_FILE env var's path, or the default
    secrets/hue-bridge-key.txt -- so it runs on any bridge (see README)."""
    key = os.environ.get("HUE_APP_KEY")
    if not key:
        kf = Path(os.environ.get("HUE_KEY_FILE", str(KEY_FILE))).expanduser()
        if not kf.exists():
            raise SystemExit(
                "error: no Hue application key -- set HUE_APP_KEY, or put the "
                f"key in {kf} (set HUE_KEY_FILE to change the path). Create one "
                "by pressing the bridge link button then POSTing to the bridge; "
                "see the README.")
        key = kf.read_text().strip()
    session = requests.Session()
    session.headers["hue-application-key"] = key
    session.verify = False
    return session


def extract_from_bridge() -> dict:
    """Read the bridge and return {universe, light_groups, zone_lightsets,
    scenes:[{name, scale, cells:[{lights, mode, bri, xy?/hsl?/mirek?}],
    off_lights}]} -- one cell per colour+brightness cluster per scene."""
    session = bridge_session()
    lights = {l["id"]: l["metadata"]["name"]
              for l in smg.clip_get(session, "light")}
    # names are the identity used throughout the framework (naming contract);
    # a duplicate would silently collapse two lights -- fail loud instead.
    dups = sorted(n for n, c in Counter(lights.values()).items() if c > 1)
    if dups:
        raise SystemExit(f"error: duplicate light names on the bridge {dups}; "
                         "light names must be unique (see naming-conventions.md)")
    rooms = smg.clip_get(session, "room")
    zones = smg.clip_get(session, "zone")
    owners = {g["id"]: g["metadata"]["name"] for g in rooms + zones}
    members, _ungrouped, _aggregates = smg.build_groups(zones, lights)
    zone_lightsets = {z["metadata"]["name"]:
                      sorted(lights[c["rid"]] for c in z["children"]
                             if c["rid"] in lights) for z in zones}
    scenes = sorted(smg.clip_get(session, "scene"),
                    key=lambda s: s["metadata"]["name"].lower())
    universe = sorted(lights.values())

    out = {"universe": universe, "n_lights": len(universe),
           "light_groups": members, "zone_lightsets": zone_lightsets,
           "scenes": []}
    for s in scenes:
        res = smg.analyze_scene(s, lights, members,
                                owners.get(s["group"]["rid"], "?"))
        lit, touched = set(), set()
        cells = []
        for c in res.clusters:
            sig = c.sig
            cell = {"lights": list(c.lights), "mode": sig.mode, "bri": sig.bri}
            if sig.mode == "xy":
                cell["xy"] = [round(sig.x, 4), round(sig.y, 4)]
                hsl = smg.sig_hsl(sig)
                if hsl:
                    cell["hsl"] = list(hsl)
            elif sig.mode == "ct":
                cell["mirek"] = sig.mirek
            cells.append(cell)
            touched.update(c.lights)
            if sig.mode != "off":
                lit.update(c.lights)
        # a light the scene never sets is left AT ITS CURRENT STATE (passthrough),
        # which the layered bake cannot represent -- it models such lights as OFF.
        # Every current scene sets all lights, so this is latent; warn if not.
        untouched = sorted(set(universe) - touched)
        if untouched:
            print(f"warning: scene {res.name!r} leaves {len(untouched)} light(s) "
                  f"untouched (passthrough); modeling them as OFF: {untouched}",
                  file=sys.stderr)
        out["scenes"].append({"name": res.name, "scale": res.scale,
                              "cells": cells,
                              "off_lights": sorted(set(universe) - lit)})
    return out


# ========================================================================
# Model: turn the data dict into solver scenes
# ========================================================================
def is_off_cell(cell) -> bool:
    """A light ON at 0% brightness is visually dark -> treat as OFF, so it folds
    into the default layer rather than inflating the cell count."""
    return cell.get("mode") == "off" or cell.get("bri") in (0, 0.0)


def build_model(data: dict):
    U = frozenset(data["universe"])
    zones = {n: frozenset(v) for n, v in data.get("light_groups", {}).items()}

    scenes = []
    for s in data["scenes"]:
        lit, off = [], set(s.get("off_lights", []))
        for c in s["cells"]:
            if is_off_cell(c):
                off.update(c["lights"])
            else:
                lit.append(frozenset(c["lights"]))
        seen = set()
        for c in lit:
            if not seen.isdisjoint(c):
                raise SystemExit(
                    f"error: overlapping lit cells in scene {s['name']!r} "
                    f"(lights {sorted(seen & c)}); a scene must partition its "
                    "lights (each light one colour/brightness)")
            seen |= c
        extra = (seen | off) - set(U)
        missing = set(U) - (seen | off)
        if extra or missing:
            raise SystemExit(
                f"error: scene {s['name']!r} cells+off != universe -- "
                f"unknown lights {sorted(extra)}, unaccounted {sorted(missing)}")
        scenes.append({"name": s["name"], "cells": lit,
                       "L": frozenset(seen), "off": frozenset(off)})
    return U, zones, scenes


# ========================================================================
# 1. Expressibility checker  +  bake verification
# ========================================================================
def _toposort(m, above):
    succ = [set(a) for a in above]
    indeg = [0] * m
    for i in range(m):
        for j in succ[i]:
            indeg[j] += 1
    order, ready = [], [i for i in range(m) if indeg[i] == 0]
    while ready:
        n = ready.pop()
        order.append(n)
        for j in succ[n]:
            indeg[j] -= 1
            if indeg[j] == 0:
                ready.append(j)
    return order if len(order) == m else None


def express(scene, F, want_layers=False):
    """Return bottom->top [(group, cell), ...] if expressible, else None."""
    cells, L = scene["cells"], scene["L"]
    if not cells:
        return [] if want_layers else True
    cand = []
    for c in cells:
        opts = [g for g in F if c <= g <= L]
        if not opts:
            return None
        cand.append(opts)
    m = len(cells)
    for choice in itertools.product(*cand):
        above = [set() for _ in range(m)]
        for i, g in enumerate(choice):
            extra = g - cells[i]
            if not extra:
                continue
            for j in range(m):
                if j != i and (extra & cells[j]):
                    above[i].add(j)
        order = _toposort(m, above)
        if order is not None:
            if not want_layers:
                return True
            return [(choice[k], cells[k]) for k in order]
    return None


def bake_ok(scene, layers):
    """Paint layers bottom->top; assert the result equals ground truth."""
    truth = {}
    for idx, c in enumerate(scene["cells"]):
        for x in c:
            truth[x] = idx
    for x in scene["off"]:
        truth[x] = -1
    painted = {x: -1 for x in truth}
    for g, cell in layers:
        cid = scene["cells"].index(cell)
        for x in g:
            painted[x] = cid
    return painted == truth


# ========================================================================
# 2. Candidate pools
# ========================================================================
def atoms_of(scenes):
    """Common refinement of every scene's partition (cells + off)."""
    parts = frozenset({frozenset(s["off"]) for s in scenes if s["off"]})
    parts |= {c for s in scenes for c in s["cells"]}
    plist = list(parts)
    lights = set().union(*plist) if plist else set()
    atoms = {}
    for x in lights:
        lab = tuple(i for i, p in enumerate(plist) if x in p)
        atoms.setdefault(lab, set()).add(x)
    return [frozenset(v) for v in atoms.values()]


def unions_of(atoms):
    """All non-empty unions of the given atom sets (capped for safety)."""
    n = len(atoms)
    if n > 20:
        raise RuntimeError(f"{n} atoms -> pool too large; refine input")
    pool = set()
    for r in range(1, n + 1):
        for combo in itertools.combinations(range(n), r):
            pool.add(frozenset().union(*(atoms[i] for i in combo)))
    return pool


def cell_union_pool(scenes):
    """Fast pool: unions of whole cells of individual scenes (upper bound)."""
    pool = set()
    for s in scenes:
        for r in range(1, len(s["cells"]) + 1):
            for combo in itertools.combinations(s["cells"], r):
                pool.add(frozenset().union(*combo))
    return pool


# ========================================================================
# 3. Minimum-family search (complete, monotone, iterative-deepening DFS)
# ========================================================================
def relevant(scene, pool):
    L = scene["L"]
    return [g for g in pool if g <= L and any(c <= g for c in scene["cells"])]


def min_family(pool, scenes, seed_upper=None):
    """Smallest F subset-of pool expressing all scenes.  Complete + exact."""
    pool = set(pool)
    rel = {i: sorted(relevant(s, pool), key=lambda g: -len(g))
           for i, s in enumerate(scenes)}

    best = {"F": set(seed_upper) if seed_upper else None,
            "size": len(seed_upper) if seed_upper else len(pool) + 1}
    visited = set()

    def conflict_lb(F, fscenes):
        # admissible: failing scenes whose possible fixer-groups are pairwise
        # disjoint each need a distinct new group.
        fixers = [frozenset(g for g in rel[i] if g not in F) for i in fscenes]
        chosen = []
        for k, fx in sorted(enumerate(fixers), key=lambda t: len(t[1])):
            if all(fx.isdisjoint(fixers[j]) for j in chosen):
                chosen.append(k)
        return len(chosen)

    def dfs(F):
        if len(F) >= best["size"]:
            return
        key = frozenset(F)
        if key in visited:
            return
        visited.add(key)
        fscenes = [i for i, s in enumerate(scenes) if express(s, F) is None]
        if not fscenes:
            best["F"], best["size"] = set(F), len(F)
            return
        if len(F) + conflict_lb(F, fscenes) >= best["size"]:
            return
        i = min(fscenes, key=lambda i: sum(1 for g in rel[i] if g not in F))
        for g in rel[i]:
            if g not in F:
                dfs(F | {g})

    dfs(set())
    return best["F"]


# ========================================================================
# Naming / reporting helpers
# ========================================================================
def namer(U, zones):
    def name(g):
        for n, z in zones.items():
            if z == g:
                return n
        if g == U:
            return "ALL (whole home, %d)" % len(U)
        comp = [n for n, z in zones.items() if z and z <= g]
        cov = frozenset().union(*(zones[n] for n in comp)) if comp else frozenset()
        if cov == g and comp:
            return "UNION{ %s }" % " + ".join(
                sorted(comp, key=lambda n: -len(zones[n])))
        return "set(%d): %s" % (len(g), ", ".join(sorted(g)))
    return name


def is_zone_union(g, zones):
    comp = [z for z in zones.values() if z and z <= g]
    cov = frozenset().union(*comp) if comp else frozenset()
    return cov == g


def _cellrep(cell):
    xs = sorted(cell)
    return f"cell<{len(cell)}:{xs[0]}..>"


def solve(scenes):
    """Return (F, layers, upper, full_pool) with F a certified minimum family.
    Depends only on the scene partitions -- the universe U and named zones are
    the caller's concern (naming/reporting), never the solve itself."""
    upper = min_family(cell_union_pool(scenes), scenes)
    full_pool = unions_of(atoms_of(scenes))
    F = min_family(full_pool, scenes, seed_upper=upper)
    layers = {s["name"]: express(s, F, want_layers=True) for s in scenes}
    return F, layers, upper, full_pool


# ========================================================================
# Main
# ========================================================================
def report(U, zones, scenes):
    name = namer(U, zones)
    print("=" * 72)
    print(f"universe = {len(U)} lights, {len(scenes)} scenes, "
          f"{len(zones)} named zones")
    atoms = atoms_of(scenes)
    print(f"atoms (common refinement of all scene partitions) = {len(atoms)}")

    forced = {s["cells"][0] for s in scenes if len(s["cells"]) == 1}
    if forced:
        print("\nsingle-cell scenes force these exact groups:")
        for g in sorted(forced, key=lambda g: -len(g)):
            who = [s["name"] for s in scenes
                   if len(s["cells"]) == 1 and s["cells"][0] == g]
            print(f"    |{len(g):2d}|  {name(g):32s} <- {who}")

    F, layers, upper, full_pool = solve(scenes)
    print(f"\nupper-bound (union-of-cells) |F| = {len(upper)}")
    print(f"\n*** MINIMUM GLOBAL META-GROUP FAMILY:  |F| = {len(F)} ***")
    for g in sorted(F, key=lambda g: -len(g)):
        print(f"    |{len(g):2d}|  {name(g)}")

    print("\n" + "=" * 72)
    print("VERIFICATION  (bake each scene from its computed layer stack)")
    print("=" * 72)
    ok_all = True
    for s in scenes:
        ly = layers[s["name"]]
        ok = bake_ok(s, ly)
        ok_all &= ok
        desc = " -> ".join(f"{name(g)}={_cellrep(c)}" for g, c in ly) \
            or "[all OFF]"
        print(f"  {s['name']:9s} {'OK ' if ok else 'FAIL'} {desc}")
    print(f"\nALL {len(scenes)} SCENES BAKE EXACTLY TO GROUND TRUTH: {ok_all}")
    if not ok_all:
        raise SystemExit("error: bake verification FAILED -- the computed layer "
                         "stack does not reproduce a scene; do not trust this run")

    print("\n" + "=" * 72)
    print("INTERPRETABILITY  (restrict F to unions of whole named zones)")
    print("=" * 72)
    zpool = {g for g in full_pool if is_zone_union(g, zones)}
    Fz = min_family(zpool, scenes)
    print(f"  unconstrained optimum |F|         = {len(F)}")
    print(f"  every optimum group is zone-union? {all(is_zone_union(g, zones) for g in F)}")
    if Fz is None:
        print("  min |F| restricted to zone-unions = INFEASIBLE "
              "(some scene needs a partial-zone group)")
    else:
        print(f"  min |F| restricted to zone-unions = {len(Fz)}")
        print(f"  interpretability cost             = {len(Fz) - len(F)} group(s)")
    return F, layers


def json_result(U, zones, scenes):
    name = namer(U, zones)
    F, layers, _upper, _pool = solve(scenes)
    return {
        "n_lights": len(U),
        "family": [{"name": name(g), "lights": sorted(g), "size": len(g)}
                   for g in sorted(F, key=lambda g: -len(g))],
        "scenes": {
            sname: [{"group": name(g), "lights": sorted(g),
                     "cell": sorted(c)} for g, c in ly]
            for sname, ly in layers.items()},
    }


# ========================================================================
# Layered design exporter: materialise the scene-groups.yaml registry + the
# live-bridge colours into the layered scene-designs.yaml (per-scene layer
# stacks). This is the Phase-1 half of the layered migration -- the registry is
# the editable vocabulary, the designs file is the authorable source of truth
# the layered sync (Phase 2) bakes onto the bridge.
# ========================================================================
def load_group_registry(zone_lightsets, path=GROUPS_YAML):
    """Read scene-groups.yaml -> ordered [(name, frozenset(lights))]. A group's
    light set is the union of its `zones:` (resolved live) plus any explicit
    `lights:` (a fallback for groups that are not a whole-zone union, e.g. an
    --export-groups starter registry)."""
    doc = yaml.safe_load(Path(path).read_text()) or {}
    fam = []
    for entry in doc.get("groups", []):
        name = entry["name"]
        lights = set()
        for z in entry.get("zones", []):
            if z not in zone_lightsets:
                raise SystemExit(
                    f"error: scene-groups.yaml group {name!r} names unknown "
                    f"zone {z!r} (see naming-conventions.md zone table)")
            lights |= set(zone_lightsets[z])
        lights |= set(entry.get("lights", []))
        if not lights:
            raise SystemExit(f"error: scene-groups.yaml group {name!r} is empty")
        fam.append((name, frozenset(lights)))
    if not fam:
        raise SystemExit(f"error: {path} declares no groups")
    names = [n for n, _ in fam]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SystemExit(f"error: {path} has duplicate group name(s) {dup}; "
                         "group names must be unique (they label design layers)")
    return fam


def load_template_names(path=GROUPS_YAML):
    """Read the optional `templates:` block of scene-groups.yaml ->
    {(group, ...): template_name}. A template is a layer-stack sequence."""
    doc = yaml.safe_load(Path(path).read_text()) or {}
    return {tuple(e["stack"]): e["name"] for e in doc.get("templates", [])}


def _cell_cfg(cell):
    """(inline-yaml-config, hsl-comment) for a live colour cell dict.
    xy authoritative + a readable # hsl(...); ct: <mirek> = tunable white.
    A lit cell always has a colour (xy or ct); bri floors at 1 so a lit layer
    is never written as on-at-0% (exact-0 cells fold to OFF via is_off_cell)."""
    bri = cell.get("bri")
    bri_i = max(1, int(round(bri))) if bri is not None else 100
    if cell.get("mode") == "ct":
        sig = smg.Sig("ct", bri=bri, mirek=cell["mirek"])
        return f"ct: {int(round(cell['mirek']))}, bri: {bri_i}", \
            smg.sig_color_label(sig)
    if cell.get("mode") == "xy":
        xy = cell["xy"]
        sig = smg.Sig("xy", bri=bri, x=xy[0], y=xy[1])
        return f"xy: [{xy[0]:.4f}, {xy[1]:.4f}], bri: {bri_i}", \
            smg.sig_color_label(sig)
    raise SystemExit(
        f"error: cannot export a lit cell with mode {cell.get('mode')!r} "
        f"(lights {sorted(cell.get('lights', []))}): a layer must paint a "
        "colour (xy) or white (ct); a colour-unchanged 'on' state is not "
        "representable in the layered design.")


def export_designs(data):
    """Build the layered scene-designs.yaml text from live cells + the
    registry. VERIFIES the registry family expresses AND bakes every scene
    before emitting (fail loud) so the design file is always faithful.
    Returns (yaml_text, n_groups, n_min)."""
    zone_lightsets = data["light_groups"]
    fam = load_group_registry(zone_lightsets)
    name_of = {g: n for n, g in fam}
    if len(name_of) != len(fam):
        raise SystemExit("error: scene-groups.yaml has two groups sharing one "
                         "light set (names must map to distinct sets)")
    F = {g for _, g in fam}

    _U, _zones, scenes = build_model(data)
    Fmin, _layers, _upper, _pool = solve(scenes)  # certified minimum (informational)

    color_by_cell = {}
    for s in data["scenes"]:
        for c in s["cells"]:
            if not is_off_cell(c):
                color_by_cell[(s["name"], frozenset(c["lights"]))] = c

    out = [
        "# Layered scene designs -- source of truth for the layered Hue sync.",
        "# Generated by `scene-layers.py --export-designs` from the live bridge",
        "# colours + the scene-groups.yaml registry; edit here, then apply.",
        "#",
        "# Each scene = default OFF + an ordered LAYER stack (bottom -> top); the",
        "# topmost layer covering a light wins, so a lower layer's group may be a",
        "# superset that higher layers overpaint. Each layer paints one registry",
        "# group. xy is AUTHORITATIVE (exact Hue gamut); the trailing # hsl(...)",
        "# is a readable annotation only. ct: <mirek> = tunable white. bri = %.",
        f"# Vocabulary ({len(fam)} groups): " + ", ".join(n for n, _ in fam)
        + " -- defined in scene-groups.yaml.",
        "scenes:",
    ]
    for s in scenes:
        layers = express(s, F, want_layers=True)
        if layers is None:
            raise SystemExit(
                f"error: scene {s['name']!r} is NOT expressible by the "
                "scene-groups.yaml family (no group covers one of its colour "
                "cells within the scene's lit set). Edit the registry and re-run.")
        if not bake_ok(s, layers):
            raise SystemExit(
                f"error: scene {s['name']!r} does not bake exactly from its "
                "computed layer stack -- refusing to emit an unfaithful design.")
        out.append(f"  - name: {s['name']}")
        if not layers:
            out.append("    layers: []   # all lights off")
            out.append("")
            continue
        frags = [(name_of[g],) + _cell_cfg(color_by_cell[(s["name"], cell)])
                 for g, cell in layers]
        gw = max(len(n) for n, _, _ in frags)
        out.append("    layers:   # bottom -> top")
        for gname, cfg, hsl in frags:
            line = f"      - {{ group: {(gname + ',').ljust(gw + 1)} {cfg} }}"
            if hsl:
                line = f"{line.ljust(58)}  # {hsl}"
            out.append(line)
        out.append("")
    return "\n".join(out).rstrip() + "\n", len(fam), len(Fmin)


def export_groups(data):
    """Generate a STARTER scene-groups.yaml from the solver's certified-minimum
    family. Names are placeholders (G1..; the whole-home group is ALL) to be
    renamed. Emits `zones:` when a group is a whole-zone union, else an explicit
    `lights:` list. This is the bootstrap for a new bridge -- run it, rename the
    groups, then `--export-designs`."""
    U, zones, scenes = build_model(data)
    F, _layers, _upper, _pool = solve(scenes)
    fam = sorted(F, key=lambda g: -len(g))
    out = [
        "# Layered scene-group registry -- GENERATED by `scene-layers.py",
        "# --export-groups`. A CERTIFIED-MINIMUM meta-group family for the live",
        "# scenes (no smaller family can express them). RENAME the placeholder",
        "# groups (G1.. / ALL) to something meaningful, then run",
        "# `--export-designs` to materialise scene-designs.yaml against them.",
        "# (Optionally add a `templates:` block naming each layer-stack sequence.)",
        "groups:",
    ]
    for i, g in enumerate(fam, 1):
        name = "ALL" if g == U else f"G{i}"
        zs = [z for z, zl in zones.items() if zl and zl <= g]
        cov = frozenset().union(*(zones[z] for z in zs)) if zs else frozenset()
        out.append(f"  - name: {name}")
        if cov == g and zs:
            zs = sorted(zs, key=lambda z: (-len(zones[z]), z))
            out.append(f"    zones: [{', '.join(zs)}]   # {len(g)} lights")
        else:
            out.append(f"    lights: [{', '.join(sorted(g))}]")
    return "\n".join(out) + "\n"


# ========================================================================
# Layered SYNC: validate + apply the layered scene-designs.yaml onto the bridge.
# (Phase 2 of the migration -- replaces scene-schema.py.) A scene is baked by
# painting its layer stack bottom -> top (topmost covering layer wins), every
# uncovered light -> OFF. The diff / backup / PUT / verify mechanics are the
# proven scene-schema.py ones: resolve EVERY targeted scene first (atomic -- a
# parse error writes nothing), write ONLY beyond-tolerance lights (in-tolerance
# lights stay byte-exact), never-overwriting per-scene backup, verify by re-read.
# ========================================================================
BRIDGE = smg.BRIDGE          # env-configurable (HUE_BRIDGE_IP); single source
BACKUP_DIR = Path("tmp")


def _resolve_registry(session):
    """(registry {name: frozenset(lights)}, universe [all light names])."""
    lights = {l["id"]: l["metadata"]["name"]
              for l in smg.clip_get(session, "light")}
    zones = smg.clip_get(session, "zone")
    zone_lightsets = {z["metadata"]["name"]:
                      sorted(lights[c["rid"]] for c in z["children"]
                             if c["rid"] in lights) for z in zones}
    fam = load_group_registry(zone_lightsets)
    return {n: g for n, g in fam}, sorted(lights.values())


def _bridge_maps(session):
    """(rid->name, name->[rids], scene-name->live-scene)."""
    lights_raw = smg.clip_get(session, "light")
    rid2name = {l["id"]: l["metadata"]["name"] for l in lights_raw}
    name2rids = {}
    for rid, nm in rid2name.items():
        name2rids.setdefault(nm, []).append(rid)
    scene_by_name = {s["metadata"]["name"]: s
                     for s in smg.clip_get(session, "scene")}
    return rid2name, name2rids, scene_by_name


def _layer_action(scene_name, layer):
    """One layer's Hue action (xy colour or ct white + brightness)."""
    bri = round(float(layer["bri"]), 2)
    act = {"on": {"on": True}, "dimming": {"brightness": bri}}
    if "xy" in layer:
        x, y = layer["xy"]
        act["color"] = {"xy": {"x": float(x), "y": float(y)}}
    elif "ct" in layer:
        act["color_temperature"] = {"mirek": int(layer["ct"])}
    else:
        raise SystemExit(f"error: scene {scene_name!r} layer for group "
                         f"{layer.get('group')!r} has neither xy nor ct")
    return act


def _bake_targets(scene_name, layers, registry, universe):
    """name -> Hue action for a layered scene: default OFF, then paint layers
    bottom -> top so the topmost covering layer wins per light."""
    targets = {n: {"on": {"on": False}} for n in universe}
    for layer in layers or []:
        gname = layer["group"]
        if gname not in registry:
            raise SystemExit(f"error: scene {scene_name!r} names unknown group "
                             f"{gname!r} (see scene-groups.yaml)")
        act = _layer_action(scene_name, layer)
        for lname in registry[gname]:
            targets[lname] = act
    return targets


def _color_mode(act):
    if act.get("color", {}).get("xy"):
        return "xy"
    if act.get("color_temperature", {}).get("mirek") is not None:
        return "ct"
    return "none"


def _action_diff(live, target):
    """Beyond-tolerance difference description, or None. Proven scene-schema
    logic incl. the colour-mode-none guard (a target colour vs a live action
    with no colour is a real difference, not a match)."""
    lon = live.get("on", {}).get("on")
    ton = target.get("on", {}).get("on")
    if bool(lon) != bool(ton):
        return f"on {lon} -> {ton}"
    if not ton:
        return None  # both off
    lb = live.get("dimming", {}).get("brightness")
    tb = target.get("dimming", {}).get("brightness")
    if tb is not None and (lb is None or abs(lb - tb) > smg.BRI_TOL):
        return f"bri {('?' if lb is None else f'{lb:.0f}%')} -> {tb:.0f}%"
    lmode, tmode = _color_mode(live), _color_mode(target)
    if tmode != "none" and tmode != lmode:
        return f"colour mode {lmode} -> {tmode}"
    if tmode == "xy" == lmode:
        lxy, txy = live["color"]["xy"], target["color"]["xy"]
        d = math.dist((lxy["x"], lxy["y"]), (txy["x"], txy["y"]))
        if d > smg.XY_TOL:
            return (f"xy ({lxy['x']:.4f},{lxy['y']:.4f}) -> "
                    f"({txy['x']:.4f},{txy['y']:.4f})  d={d:.4f}")
    if tmode == "ct" == lmode:
        lct = live["color_temperature"]["mirek"]
        tct = target["color_temperature"]["mirek"]
        if abs(lct - tct) > smg.MIREK_TOL:
            return f"ct {lct} -> {tct} mirek"
    return None


def _unique_backup(scene_name):
    """A never-overwriting backup path (bumps a counter)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", scene_name.lower()).strip("-")
    base = f"scene-backup-{slug}-layered-{stamp}"
    path = BACKUP_DIR / f"{base}.json"
    n = 2
    while path.exists():
        path = BACKUP_DIR / f"{base}-{n}.json"
        n += 1
    return path


def _scene_pending(session, design, only):
    """Resolve every targeted scene up front (atomic). Returns
    [(name, live, {rid: action})] with only beyond-tolerance rids pending."""
    registry, universe = _resolve_registry(session)
    rid2name, name2rids, scene_by_name = _bridge_maps(session)
    plan = []
    for ds in design.get("scenes") or []:
        if only and ds["name"] not in only:
            continue
        live = scene_by_name.get(ds["name"])
        if live is None:
            print(f"{ds['name']}: MISSING on bridge -- skipped", file=sys.stderr)
            continue
        targets = _bake_targets(ds["name"], ds.get("layers"), registry, universe)
        rid_targets = {}
        for lname, act in targets.items():
            for rid in name2rids.get(lname, []):
                rid_targets[rid] = act
        rid2live = {a["target"]["rid"]: a["action"] for a in live["actions"]}
        # An absent rid diffs against {} -> included only when its target is ON
        # (a light the scene lacks but the design lights); absent+OFF is a no-op.
        pending = {rid: t for rid, t in rid_targets.items()
                   if _action_diff(rid2live.get(rid, {}), t) is not None}
        plan.append((ds["name"], live, pending))
    return plan, rid2name


def validate_design(session, design, only):
    """Diff each designed scene against the live bridge (report only)."""
    plan, rid2name = _scene_pending(session, design, only)
    total = 0
    for name, live, pending in plan:
        if not pending:
            print(f"{name}: OK")
            continue
        print(f"{name}: {len(pending)} discrepanc"
              f"{'y' if len(pending) == 1 else 'ies'}")
        rid2live = {a["target"]["rid"]: a["action"] for a in live["actions"]}
        total += len(pending)
        for rid, tgt in pending.items():
            print(f"    {rid2name.get(rid, rid)}: "
                  f"{_action_diff(rid2live.get(rid, {}), tgt)}")
    print(f"\n{total} discrepancies total"
          + ("" if total else " -- bridge matches the design"))
    return 1 if total else 0


def apply_design(session, design, only, assume_yes):
    """Bake the layered design onto the bridge (dry-run unless assume_yes)."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    plan, rid2name = _scene_pending(session, design, only)
    if not any(p for _, _, p in plan):
        print("nothing to do -- bridge already matches the design")
        return 0
    failed = False
    for name, live, pending in plan:
        if not pending:
            print(f"{name}: already matches -- no write")
            continue
        rid2live = {a["target"]["rid"]: a["action"] for a in live["actions"]}
        print(f"{name}: {len(pending)} light(s) to update")
        for rid in pending:
            print(f"    {rid2name.get(rid, rid)}: "
                  f"{_action_diff(rid2live.get(rid, {}), pending[rid]) or 'new'}")
        if not assume_yes:
            print(f"  (dry-run; pass --yes to write '{name}')")
            continue
        backup = _unique_backup(name)
        backup.write_text(json.dumps(live, indent=2))
        actions = live["actions"]
        idx = {a["target"]["rid"]: a for a in actions}
        for rid, act in pending.items():
            if rid in idx:
                idx[rid]["action"] = act
            else:
                actions.append({"target": {"rid": rid, "rtype": "light"},
                                "action": act})
        r = session.put(f"{BRIDGE}/clip/v2/resource/scene/{live['id']}",
                        json={"actions": actions}, timeout=10, verify=False)
        r.raise_for_status()
        errs = r.json().get("errors")
        fresh = next((s for s in smg.clip_get(session, "scene")
                      if s["id"] == live["id"]), None)
        if fresh is None:
            print(f"    -> backed up {backup.name}; PUT VERIFY FAILED "
                  "(scene not found on re-read)")
            failed = True
            continue
        fresh_live = {a["target"]["rid"]: a["action"] for a in fresh["actions"]}
        bad = [rid2name.get(rid, rid) for rid, act in pending.items()
               if _action_diff(fresh_live.get(rid, {}), act) is not None]
        status = "OK" if not bad and not errs else f"MISMATCH {bad or errs}"
        print(f"    -> backed up {backup.name}; PUT {status}")
        failed = failed or bool(bad or errs)
    return 1 if failed else 0


# ========================================================================
# HTML report -- the LAYERED report, rendered by smg.layered_report() (the
# single renderer for the layered model; it owns REPORT_CSS + swatch +
# scene_bar_svg). GUARDRAIL: never hand-roll a renderer or duplicate
# REPORT_CSS/swatch/bar markup here -- assemble plain data and hand it to
# smg.layered_report(). See the "HTML RENDERING" section in the module docstring.
# ========================================================================
def layered_view(session):
    """Assemble the layered-report data (family + per-scene layer stacks + baked
    colour clusters) for smg.layered_report(). Uses the scene-groups.yaml
    registry family directly (not a re-solve), so the report reflects the
    registry exactly; fails loud if a scene is no longer expressible by it."""
    lights = {l["id"]: l["metadata"]["name"]
              for l in smg.clip_get(session, "light")}
    rooms = smg.clip_get(session, "room")
    zones = smg.clip_get(session, "zone")
    owners = {g["id"]: g["metadata"]["name"] for g in rooms + zones}
    members, _ungrouped, _aggregates = smg.build_groups(zones, lights)
    zone_lightsets = {z["metadata"]["name"]:
                      sorted(lights[c["rid"]] for c in z["children"]
                             if c["rid"] in lights) for z in zones}
    fam = load_group_registry(zone_lightsets)          # [(name, frozenset)]
    name_of = {g: n for n, g in fam}
    F = {g for _, g in fam}
    tnames = load_template_names()
    universe = frozenset(lights.values())

    scenes_raw = sorted(smg.clip_get(session, "scene"),
                        key=lambda s: s["metadata"]["name"].lower())
    scenes = []
    for s in scenes_raw:
        res = smg.analyze_scene(s, lights, members,
                                owners.get(s["group"]["rid"], "?"))
        clusters = [(list(c.lights), c.sig) for c in res.clusters]
        lit, sig_by_cell = [], {}
        for c in res.clusters:
            fs = frozenset(c.lights)
            if c.sig.mode == "off" or c.sig.bri in (0, 0.0):
                continue
            lit.append(fs)
            sig_by_cell[fs] = c.sig
        L = frozenset().union(*lit) if lit else frozenset()
        scene_model = {"name": res.name, "cells": lit, "L": L,
                       "off": universe - L}
        layers = express(scene_model, F, want_layers=True)
        if layers is None:
            raise SystemExit(
                f"error: scene {res.name!r} is not expressible by "
                "scene-groups.yaml -- the registry family is stale for the live "
                "scenes; re-check scene-groups.yaml (run the solver).")
        stack = [(name_of[g], sig_by_cell[cell]) for g, cell in layers]
        seq = tuple(n for n, _ in stack)
        scenes.append({"name": res.name, "owner": res.owner,
                       "template": seq, "template_name": tnames.get(seq),
                       "layers": stack, "clusters": clusters})
    family = [(n, sorted(g)) for n, g in fam]
    return family, scenes, members


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--cells", metavar="PATH",
                    help="solve an offline scene-cells.json instead of the bridge")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable result instead of the report")
    ap.add_argument("--html", metavar="PATH", nargs="?",
                    const="tmp/scene-layers.html",
                    help="render a browsable HTML report (default "
                    "tmp/scene-layers.html) and exit")
    ap.add_argument("--out", metavar="PATH", help="write output to PATH")
    ap.add_argument("--export-cells", metavar="PATH",
                    help="write the live per-scene cells to PATH and exit (no solve)")
    ap.add_argument("--export-groups", metavar="PATH", nargs="?", const="-",
                    help="generate a STARTER scene-groups.yaml (certified-minimum "
                    "family, placeholder names) to PATH or stdout, then exit -- "
                    "the bootstrap for a new bridge; rename, then --export-designs")
    ap.add_argument("--export-designs", metavar="PATH", nargs="?",
                    const=str(DESIGNS_YAML),
                    help="materialise the layered scene-designs.yaml (default "
                    f"{DESIGNS_YAML}) from live colours + scene-groups.yaml, then exit")
    ap.add_argument("--validate-design", action="store_true",
                    help="diff the layered scene-designs.yaml against the live "
                    "bridge (report only) and exit")
    ap.add_argument("--apply", action="store_true",
                    help="bake the layered scene-designs.yaml onto the bridge "
                    "(dry-run unless --yes; per-scene tmp/ backup + verify)")
    ap.add_argument("--design", metavar="PATH", default=str(DESIGNS_YAML),
                    help="design file for --apply/--validate-design "
                    "(default %(default)s)")
    ap.add_argument("--scene", action="append", dest="scenes",
                    help="limit --apply/--validate-design to this scene (repeatable)")
    ap.add_argument("--yes", action="store_true",
                    help="--apply: actually write (default is dry-run)")
    args = ap.parse_args()

    if args.out and not args.json:
        ap.error("--out only applies to --json (--export-cells takes its own PATH)")
    if args.apply and args.validate_design:
        ap.error("--apply and --validate-design are mutually exclusive")

    # The layered sync (validate/apply) reads the design file + live bridge --
    # not the solver -- so handle it before the solve path, like --html.
    if args.apply or args.validate_design:
        design_path = Path(args.design)
        if not design_path.exists():
            ap.error(f"design file {design_path} not found -- run "
                     "--export-designs first")
        design = yaml.safe_load(design_path.read_text()) or {}
        only = set(args.scenes) if args.scenes else None
        session = bridge_session()
        if args.validate_design:
            return validate_design(session, design, only)
        return apply_design(session, design, only, args.yes)

    # --html renders the LAYERED report from the live bridge via
    # smg.layered_report() (the single renderer). It does not use the solver or
    # an offline export, so handle it first.
    if args.html:
        if args.cells:
            ap.error("--html renders from the live bridge (it reuses the "
                     "shared renderer); it cannot use --cells")
        family, scenes, members = layered_view(bridge_session())
        # embed the config (YAML) + source (Python) so the report is a
        # self-contained, buildable spec viewable from the overlay
        source_docs = [(p.name, p.read_text(), "yaml")
                       for p in (DESIGNS_YAML, GROUPS_YAML) if p.exists()]
        source_docs += [(p.name, p.read_text(), "python")
                        for p in (SCRIPT_DIR / "scene-layers.py",
                                  SCRIPT_DIR / "scene-meta-groups.py")
                        if p.exists()]
        html = smg.layered_report(family, scenes, members,
                                  source_docs=source_docs)
        dest = Path(args.html)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html)
        print(f"wrote {dest}  ({len(scenes)} scenes)")
        return 0

    data = json.loads(Path(args.cells).read_text()) if args.cells \
        else extract_from_bridge()

    if args.export_groups:
        text = export_groups(data)
        if args.export_groups == "-":
            sys.stdout.write(text)
        else:
            dest = Path(args.export_groups)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            print(f"wrote {dest}", file=sys.stderr)
        return 0

    if args.export_designs:
        text, n_groups, n_min = export_designs(data)
        dest = Path(args.export_designs)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        note = "" if n_groups == n_min else \
            f"  WARNING: registry has {n_groups} groups but the certified " \
            f"minimum is {n_min} -- the family is not minimal"
        print(f"wrote {dest}  ({len(data.get('scenes', []))} scenes, "
              f"{n_groups}-group vocabulary){note}")
        return 0

    if args.export_cells:
        dest = Path(args.export_cells)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(data, indent=2))
        n_lights = data.get("n_lights", len(data.get("universe", [])))
        print(f"wrote {dest} ({len(data.get('scenes', []))} scenes, "
              f"{n_lights} lights)")
        return 0

    U, zones, scenes = build_model(data)

    if args.json:
        text = json.dumps(json_result(U, zones, scenes), indent=2)
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
            print(f"wrote {out}")
        else:
            print(text)
        return 0

    report(U, zones, scenes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
