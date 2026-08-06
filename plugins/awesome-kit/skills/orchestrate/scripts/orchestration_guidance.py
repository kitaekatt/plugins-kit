"""orchestration_guidance.py -- render the orchestrate skill's variable policy.

The `orchestrate` skill's durable half (economics, procedure, anti-patterns)
lives in SKILL.md. Its VARIABLE half -- which model tier suits which unit,
which dispatch backends exist on this machine and how to drive them, and how
much usage capacity is left -- is configuration, and this script renders it.

Configuration resolves over three layers, later winning:

    1. shipped   <plugin>/skills/orchestrate/defaults/orchestration.yaml
    2. user      ~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml
    3. project   <project_root>/.claude/orchestration.yaml

Override files are sparse. Mappings deep-merge; the `tiers` and `backends`
lists merge by record `id` (patch a known id, append a new one, drop one with
`disabled: true`); scalars and plain lists replace.

Usage:
    orchestration_guidance.py [--project-root PATH]
    orchestration_guidance.py --explain      # layer provenance + resolved config
    orchestration_guidance.py --paths        # where the layers are read from
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Re-exec under awesome-kit's bootstrap-provisioned venv before importing
# pyyaml: a bare `python` / `uv run python` invocation builds a different
# environment that has no pyyaml. No-op when already there. The guard is the
# vendored, stdlib-only bootstrap_guard next to this script. See plugins/CLAUDE.md.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bootstrap_guard import reexec_under_plugin_venv  # noqa: E402

reexec_under_plugin_venv("awesome-kit")

try:
    import yaml  # noqa: E402
except ImportError:
    from bootstrap_guard import require_bootstrap  # noqa: E402

    require_bootstrap(
        "awesome-kit",
        feature="orchestration guidance",
        missing="pyyaml",
        force=True,
    )

PLUGIN = "awesome-kit"
MARKETPLACE = "plugins-kit"
CONFIG_NAME = "orchestration.yaml"

_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = _SCRIPT_DIR.parent / "defaults" / CONFIG_NAME

# Records in these lists are identified by `id` rather than position.
RECORD_LISTS = ("tiers", "backends")


# --------------------------------------------------------------------------
# Layer resolution
# --------------------------------------------------------------------------


def user_config_path() -> Path:
    return (
        Path.home()
        / ".claude"
        / "plugins"
        / "data"
        / MARKETPLACE
        / PLUGIN
        / CONFIG_NAME
    )


def project_config_path(project_root: Path) -> Path:
    return project_root / ".claude" / CONFIG_NAME


def layer_paths(project_root: Path) -> List[Tuple[str, Path]]:
    """The three layers in precedence order (lowest first)."""
    return [
        ("shipped", DEFAULTS_PATH),
        ("user", user_config_path()),
        ("project", project_config_path(project_root)),
    ]


def load_layer(path: Path) -> Optional[Dict[str, Any]]:
    """Parse one layer. Returns None when absent; {} for an empty/comment-only file."""
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping, got {type(data).__name__}")
    return data


def merge_records(base: List[Any], override: List[Any]) -> List[Any]:
    """Merge two record lists by `id`: patch known ids, append new ones."""
    merged = [dict(r) if isinstance(r, dict) else r for r in base]
    index = {r["id"]: i for i, r in enumerate(merged) if isinstance(r, dict) and "id" in r}
    for record in override:
        if not isinstance(record, dict) or "id" not in record:
            # Not addressable by id -- fall back to replacing the whole list.
            return list(override)
        rid = record["id"]
        if rid in index:
            merged[index[rid]] = deep_merge(merged[index[rid]], record)
        else:
            index[rid] = len(merged)
            merged.append(dict(record))
    return merged


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Mappings merge key by key; record lists merge by id; everything else replaces."""
    result = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        elif key in RECORD_LISTS and isinstance(current, list) and isinstance(value, list):
            result[key] = merge_records(current, value)
        else:
            result[key] = value
    return result


def strip_executable_fields(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Remove every field this script would EXECUTE. Returns (data, removed).

    Applied to the project layer only. That layer is a file inside whatever
    repository happens to be the cwd, so honoring `detect.command` or
    `capacity.command` from it would mean that merely rendering the policy
    inside a cloned repo runs programs that repo chose. Machine-level trust
    (the shipped and user layers) is a different question from repo-level
    trust, and only the former gets to name an executable.
    """
    removed: List[str] = []
    result = dict(data)
    backends = result.get("backends")
    if isinstance(backends, list):
        cleaned = []
        for backend in backends:
            if isinstance(backend, dict) and isinstance(backend.get("detect"), dict) \
                    and "command" in backend["detect"]:
                backend = dict(backend)
                backend["detect"] = {k: v for k, v in backend["detect"].items() if k != "command"}
                removed.append(f"backends[{backend.get('id')}].detect.command")
            cleaned.append(backend)
        result["backends"] = cleaned
    capacity = result.get("capacity")
    if isinstance(capacity, dict) and capacity.get("command") is not None:
        result["capacity"] = {k: v for k, v in capacity.items() if k != "command"}
        removed.append("capacity.command")
    return result, removed


def resolve_config(project_root: Path) -> Tuple[Dict[str, Any], List[Tuple[str, Path, str]]]:
    """Merge the layers. Returns (config, provenance) where provenance is
    (layer, path, status) with status in {applied, empty, absent}, or
    ('project', path, 'applied (N executable field(s) ignored)')."""
    config: Dict[str, Any] = {}
    provenance: List[Tuple[str, Path, str]] = []
    for layer, path in layer_paths(project_root):
        data = load_layer(path)
        if data is None:
            provenance.append((layer, path, "absent"))
            continue
        if not data:
            provenance.append((layer, path, "empty"))
            continue
        status = "applied"
        if layer == "project":
            data, removed = strip_executable_fields(data)
            if removed:
                status = f"applied ({len(removed)} executable field(s) ignored: {', '.join(removed)})"
        config = deep_merge(config, data)
        provenance.append((layer, path, status))
    return config, provenance


def active(records: List[Any]) -> List[Dict[str, Any]]:
    """Records that survive `disabled: true`."""
    return [r for r in records if isinstance(r, dict) and not r.get("disabled")]


# --------------------------------------------------------------------------
# Backend detection
# --------------------------------------------------------------------------


def detect_backend(backend: Dict[str, Any]) -> Tuple[bool, str]:
    """Is this backend usable here? Returns (available, reason).

    Fails CLOSED on anything it cannot evaluate. An undetectable backend that
    renders anyway is worse than one that silently disappears: the guidance
    would advertise mechanics for a tool that is not installed, which is the
    one outcome the omission rule exists to prevent.
    """
    if "detect" not in backend:
        return True, "no detect rule"
    rule = backend.get("detect")
    if not isinstance(rule, dict):
        return False, "malformed `detect` (expected a mapping)"
    if "always" in rule:
        return bool(rule["always"]), "always available" if rule["always"] else "disabled via detect.always"

    command = rule.get("command")
    if command:
        argv = [str(a) for a in (command if isinstance(command, list) else [command])]
        shown = " ".join(argv)
        # Resolve through PATHEXT: a CLI installed by npm/scoop is `foo.cmd` on
        # Windows, which CreateProcess will not find from the bare name.
        resolved = shutil.which(argv[0])
        if resolved is None:
            return False, f"`{argv[0]}` not found on PATH"
        argv[0] = resolved
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"`{shown}` did not run ({type(exc).__name__})"
        if proc.returncode != 0:
            return False, f"`{shown}` exited {proc.returncode}"
        first = proc.stdout.decode("utf-8", "replace").strip().splitlines()
        return True, first[0].strip() if first else "detected"

    path_rule = rule.get("path")
    if path_rule:
        expanded = Path(os.path.expanduser(str(path_rule)))
        return (expanded.exists(), str(expanded))

    # A `detect:` mapping that declares no recognized rule is a config error,
    # not an assertion of availability -- fail closed.
    return False, "`detect` declares no recognized rule (always / command / path)"


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------


def load_snapshot(capacity: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """Load the rate-limit snapshot. Returns (snapshot, note)."""
    source = capacity.get("source", "auto")
    if source == "none":
        return None, "reporting disabled (capacity.source: none)"

    raw: Optional[str] = None
    origin = ""
    if source == "command":
        command = capacity.get("command")
        if not command:
            return None, "capacity.source is `command` but no command is configured"
        argv = command if isinstance(command, list) else [str(command)]
        try:
            proc = subprocess.run(
                [str(a) for a in argv],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"capacity command failed ({type(exc).__name__})"
        if proc.returncode != 0:
            return None, f"capacity command exited {proc.returncode}"
        raw = proc.stdout.decode("utf-8", "replace")
        origin = "capacity.command"
    else:
        configured = capacity.get("snapshot_path")
        if not configured:
            return None, "no snapshot_path configured"
        path = Path(os.path.expanduser(str(configured)))
        if not path.is_file():
            return None, (
                f"no snapshot at {path} -- install plugins-kit:claude-ui-kit "
                "(its statusline writes one) or set capacity.source"
            )
        raw = path.read_text(encoding="utf-8")
        origin = str(path)

    try:
        snapshot = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"snapshot at {origin} is not valid JSON ({exc.msg})"
    if not isinstance(snapshot, dict):
        return None, f"snapshot at {origin} is not a JSON object"
    return snapshot, origin


def format_reset(delta_min: float) -> str:
    """Human-scaled reset distance. Minutes read as noise beyond an hour or two."""
    if delta_min <= 0:
        return "reset due"
    if delta_min < 120:
        return f"resets in ~{int(delta_min)}min"
    if delta_min < 48 * 60:
        return f"resets in ~{int(round(delta_min / 60.0))}h"
    return f"resets in ~{int(round(delta_min / 1440.0))}d"


def window_rows(snapshot: Dict[str, Any], capacity: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Turn a snapshot into per-window remaining-capacity rows, plus a staleness note."""
    limits = snapshot.get("rate_limits")
    if not isinstance(limits, dict):
        # load_snapshot degrades carefully on bad input; do not undo that here.
        return [], None
    captured_at = snapshot.get("captured_at")
    stale: Optional[str] = None
    if isinstance(captured_at, (int, float)):
        age_min = (time.time() - float(captured_at)) / 60.0
        max_age = capacity.get("max_age_minutes")
        if isinstance(max_age, (int, float)) and age_min > float(max_age):
            stale = f"snapshot is {int(age_min)}min old (max_age_minutes: {int(max_age)}) -- treat as indicative only"

    # `or {}` not a get() default: a key present-but-null yields None, which a
    # default only covers when the key is ABSENT.
    thresholds = capacity.get("thresholds") or {}
    warn = thresholds.get("warn_remaining_pct", 25)
    critical = thresholds.get("critical_remaining_pct", 10)

    rows: List[Dict[str, Any]] = []
    for key, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage")
        if not isinstance(used, (int, float)):
            continue
        remaining = max(0, int(round(100 - float(used))))
        if remaining <= critical:
            state = "CRITICAL"
        elif remaining <= warn:
            state = "low"
        else:
            state = "ok"
        resets_at = window.get("resets_at")
        resets = ""
        if isinstance(resets_at, (int, float)):
            resets = format_reset((float(resets_at) - time.time()) / 60.0)
        rows.append({"label": label, "remaining": remaining, "state": state, "resets": resets})
    return rows, stale


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def fold(text: Any) -> str:
    """Collapse a YAML block scalar to a single line for table cells."""
    return " ".join(str(text).split()) if text is not None else ""


def tier_backend(tier: Dict[str, Any], config: Dict[str, Any]) -> str:
    """Which ladder a tier belongs to. Unmarked tiers ride the default backend."""
    return str(tier.get("backend") or config.get("default_backend") or "")


def usable_tiers(config: Dict[str, Any], available_backends: set) -> List[Dict[str, Any]]:
    """Tiers that survive `disabled` and whose backend is present.

    A tier on a backend that is not installed must vanish completely -- it
    cannot be dispatched to, and naming it would leak the absent backend into
    the guidance.
    """
    kept = []
    for tier in active(config.get("tiers") or []):
        backend = tier_backend(tier, config)
        if backend and backend not in available_backends:
            continue
        kept.append(tier)
    return kept


def ladders(
    config: Dict[str, Any], available_backends: set
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Usable tiers grouped into per-backend ladders, default backend first.

    Tiers are compared WITHIN a ladder. Across ladders the decision is the
    backend, not the model -- so they are rendered as separate tables rather
    than one cost-ordered list, which would invite comparing rungs whose real
    difference is dispatch shape.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for tier in usable_tiers(config, available_backends):
        grouped.setdefault(tier_backend(tier, config), []).append(tier)
    default_backend = str(config.get("default_backend") or "")
    order = ([default_backend] if default_backend in grouped else []) + [
        b for b in grouped if b != default_backend
    ]
    return [(backend, grouped[backend]) for backend in order]


def render_tiers(
    config: Dict[str, Any],
    overrides: Dict[str, Any],
    available_backends: set,
    backend_names: Dict[str, str],
    out: List[str],
) -> None:
    grouped = ladders(config, available_backends)
    out.append("## Model tiers")
    out.append("")
    if not grouped:
        out.append("No tiers configured -- pick a model by judgement.")
        out.append("")
        return
    if len(grouped) > 1:
        out.append(
            "One ladder per backend. Choose the BACKEND first (see the next "
            "section), then the rung -- tiers compare within a ladder, not "
            "across them."
        )
        out.append("")
    for backend, tiers in grouped:
        if len(grouped) > 1:
            out.append(f"### {backend_names.get(backend, backend)} ladder")
            out.append("")
        render_ladder(config, tiers, overrides, out)
    # Cross-cutting prose belongs to the section, not to each ladder.
    render_implementation(config.get("implementation"), out)
    if config.get("pool_economics"):
        out.append(f"**Pool economics.** {fold(config['pool_economics'])}")
        out.append("")
    render_effort(config.get("effort"), out)


def render_implementation(impl: Any, out: List[str]) -> None:
    """Implementation routes on specification quality, not on difficulty.

    Rendered as its own block rather than folded into the tier table, because
    the tier table's axis (how much reasoning) is the wrong one for code and
    reading it that way is what sends new-but-specified work to the wrong rung.
    """
    if not impl:
        return
    if not isinstance(impl, dict):
        out.append(f"**Implementation.** {fold(impl)}")
        out.append("")
        return
    out.append("**Implementation** -- routed by SPECIFICATION QUALITY, not difficulty:")
    out.append("")
    for row in impl.get("routing") or []:
        target = str(row.get("tier") or "none")
        if target == "none":
            out.append(f"- {fold(row.get('spec'))} -> **specify first**")
        else:
            out.append(f"- {fold(row.get('spec'))} -> `{target}`")
        if row.get("action"):
            out.append(f"  - {fold(row['action'])}")
    out.append("")
    for key in ("single_unit", "top_tier"):
        if impl.get(key):
            out.append(fold(impl[key]))
            out.append("")


def render_effort(effort: Any, out: List[str]) -> None:
    """Effort is its own decision axis, so it renders as tests rather than prose.

    Accepts the legacy plain-string form too -- an override written against the
    old schema keeps working instead of silently vanishing.
    """
    if not effort:
        return
    if not isinstance(effort, dict):
        out.append(f"**Effort.** {fold(effort)}")
        out.append("")
        return
    out.append("**Effort** -- decided per unit, separately from tier. Each tier's")
    out.append("`Effort` column is that rung's default; these tests override it.")
    out.append("")
    for key, label in (("raise_when", "Raise effort when"), ("lower_when", "Lower effort when")):
        rows = effort.get(key) or []
        if not rows:
            continue
        out.append(f"- *{label}:*")
        for row in rows:
            out.append(f"  - {fold(row)}")
    if effort.get("note"):
        out.append("")
        out.append(fold(effort["note"]))
    out.append("")


def render_backend_selection(
    config: Dict[str, Any],
    available_backends: set,
    backend_names: Dict[str, str],
    out: List[str],
) -> None:
    """The ordered where-does-this-run test.

    A row naming an unavailable backend is dropped, same rule as the backend
    and tier sections: guidance never mentions something that is not installed.
    """
    selection = config.get("backend_selection") or {}
    if not selection:
        return
    gates = [g for g in (selection.get("gates") or []) if str(g.get("backend")) in available_backends]
    pulls = [p for p in (selection.get("pulls") or []) if str(p.get("backend")) in available_backends]
    if not gates and not pulls:
        return

    default = str(selection.get("default") or "")
    out.append("### Choosing a backend")
    out.append("")
    if default in available_backends:
        out.append(f"Default: **{backend_names.get(default, default)}**. Work through these in order.")
        out.append("")

    def rows(items: List[Dict[str, Any]]) -> None:
        for item in items:
            name = backend_names.get(str(item.get("backend")), str(item.get("backend")))
            line = f"- {fold(item.get('test'))} -> **{name}**"
            if item.get("why"):
                line += f" -- {fold(item['why'])}"
            out.append(line)

    if gates:
        out.append("**Gates** (disqualifiers, not preferences -- if one fires, stop here):")
        rows(gates)
        out.append("")
    if pulls:
        out.append("**Then pulls** (none firing means the default stands):")
        rows(pulls)
        out.append("")


def render_ladder(
    config: Dict[str, Any],
    tiers: List[Dict[str, Any]],
    overrides: Dict[str, Any],
    out: List[str],
) -> None:
    default_tier = config.get("default_tier")
    show_effort = any(t.get("effort") for t in tiers)
    header = ["Tier", "Model"] + (["Effort"] if show_effort else []) + [
        "Use for",
        "Escalate when",
    ]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join(["---"] * len(header)) + " |")
    for tier in tiers:
        tid = str(tier.get("id", "?"))
        marks = []
        if tid == default_tier:
            marks.append("default")
        state = overrides.get(tid)
        if state and state != "available":
            marks.append(state.upper())
        label = tid + (f" ({', '.join(marks)})" if marks else "")
        cells = [label, fold(tier.get("model")) or "-"]
        if show_effort:
            cells.append(fold(tier.get("effort")) or "-")
        cells += [
            fold(tier.get("use_for")) or "-",
            fold(tier.get("escalate_when")) or "-",
        ]
        out.append("| " + " | ".join(cells) + " |")
    out.append("")
    wrote_note = False
    for tier in tiers:
        if tier.get("avoid_when"):
            out.append(f"- **{tier.get('id')} -- NOT this tier when**: {fold(tier['avoid_when'])}")
            wrote_note = True
        if tier.get("note"):
            out.append(f"- **{tier.get('id')}**: {fold(tier['note'])}")
            wrote_note = True
    if wrote_note:
        out.append("")


def detect_all(config: Dict[str, Any]) -> List[Tuple[Dict[str, Any], bool, str]]:
    """Run every enabled backend's detect rule once, in config order."""
    results = []
    for backend in active(config.get("backends") or []):
        ok, reason = detect_backend(backend)
        results.append((backend, ok, reason))
    return results


def render_backends(
    config: Dict[str, Any],
    detected: List[Tuple[Dict[str, Any], bool, str]],
    visible_tiers: set,
    out: List[str],
) -> None:
    default_backend = config.get("default_backend")
    out.append("## Dispatch backends")
    out.append("")
    if not detected:
        out.append("No backends configured.")
        out.append("")
        return

    available_backends = {str(b.get("id")) for b, ok, _ in detected if ok}
    backend_names = {str(b.get("id")): str(b.get("name") or b.get("id")) for b, _, _ in detected}
    render_backend_selection(config, available_backends, backend_names, out)

    # An undetected backend is omitted ENTIRELY -- not listed, not named, not
    # explained. Its mechanics are unusable here, and mentioning it invites
    # dispatch to something that is not installed. `--explain` reports the
    # detection status for anyone who wants to know why a backend is missing.
    available = [(b, reason) for b, ok, reason in detected if ok]

    if not available:
        out.append("None of the configured backends detected on this machine.")
        out.append("")

    for backend, reason in available:
        bid = str(backend.get("id", "?"))
        title = f"### {backend.get('name', bid)} (`{bid}`)"
        if bid == default_backend:
            title += " -- default"
        out.append(title)
        out.append("")
        out.append(f"*Detected: {reason}.*")
        out.append("")
        if backend.get("prefer_for"):
            out.append(f"**Prefer for.** {fold(backend['prefer_for'])}")
            out.append("")
        caps = backend.get("capabilities") or {}
        if caps:
            tiers = caps.get("tiers")
            if isinstance(tiers, list):
                # Advertise only tiers that survived the gate -- a disabled or
                # backend-gated tier must not be named here either.
                shown = [str(t) for t in tiers if str(t) in visible_tiers]
                caps = dict(caps)
                caps["tiers"] = ", ".join(shown) if shown else "n/a (no tier selection)"
            for key in ("tiers", "isolation", "effort", "network", "returns"):
                if key in caps and caps[key] not in (None, ""):
                    out.append(f"- {key}: {fold(caps[key])}")
            out.append("")
        if backend.get("command"):
            out.append("```")
            out.append(fold(backend["command"]))
            out.append("```")
            out.append("")
        if backend.get("dispatch"):
            out.append(str(backend["dispatch"]).rstrip())
            out.append("")
        gotchas = backend.get("gotchas") or []
        if gotchas:
            out.append("**Gotchas.**")
            for gotcha in gotchas:
                out.append(f"- {fold(gotcha)}")
            out.append("")


def tier_overrides(config: Dict[str, Any], visible_tiers: Optional[set] = None) -> Dict[str, str]:
    """Manual per-tier availability, restricted to tiers that actually render.

    An override may name a tier gated on an absent backend. Rendering it would
    leak that tier -- and by its name the backend behind it -- through the one
    section that does not otherwise consult the gate, so filter here too.
    """
    raw = (config.get("capacity") or {}).get("tier_overrides") or {}
    items = {str(k): str(v) for k, v in raw.items()}
    if visible_tiers is None:
        return items
    return {k: v for k, v in items.items() if k in visible_tiers}


def render_capacity(config: Dict[str, Any], visible_tiers: set, out: List[str]) -> None:
    capacity = config.get("capacity") or {}
    overrides = tier_overrides(config, visible_tiers)
    out.append("## Capacity")
    out.append("")

    snapshot, note = load_snapshot(capacity)
    if snapshot is None:
        out.append(f"Usage capacity unknown -- {note}. Assume nothing about remaining headroom.")
    else:
        rows, stale = window_rows(snapshot, capacity)
        if not rows:
            out.append(f"Snapshot at {note} carries no usable rate-limit windows -- capacity unknown.")
        else:
            for row in rows:
                line = f"- {row['label']} window: {row['remaining']}% remaining ({row['state']})"
                if row["resets"]:
                    line += f", {row['resets']}"
                out.append(line)
            if stale:
                out.append(f"- NOTE: {stale}")
            if any(r["state"] != "ok" for r in rows):
                out.append("")
                out.append(
                    "Headroom is tight: bias harder toward delegation (it spends the "
                    "cheaper pool), and hold the top tier for units that genuinely meet its bar."
                )
        out.append("")
        out.append(
            "These windows are ACCOUNT-wide, not per-model -- Claude Code exposes no "
            "per-model breakdown, so they cannot tell you a specific tier is spent."
        )
    out.append("")

    if overrides:
        out.append("**Manual tier overrides** (`capacity.tier_overrides`):")
        for tid, state in sorted(overrides.items()):
            out.append(f"- `{tid}`: {state}")
        blocked = [t for t, s in overrides.items() if s == "unavailable"]
        if blocked:
            out.append("")
            out.append(
                "Do not dispatch to " + ", ".join(f"`{t}`" for t in blocked)
                + " -- route those units to the next tier down and say so when you relay results."
            )
        out.append("")


def render(config: Dict[str, Any], provenance: List[Tuple[str, Path, str]]) -> str:
    out: List[str] = ["# Orchestration policy (generated)", ""]
    detected = detect_all(config)
    available_backends = {str(b.get("id")) for b, ok, _ in detected if ok}
    backend_names = {
        str(b.get("id")): str(b.get("name") or b.get("id")) for b, _, _ in detected
    }
    # One visibility set, computed once and threaded through every section, so
    # no render path can name a tier the gate removed.
    visible_tiers = {str(t.get("id")) for t in usable_tiers(config, available_backends)}
    render_tiers(config, tier_overrides(config, visible_tiers), available_backends, backend_names, out)
    render_backends(config, detected, visible_tiers, out)
    render_capacity(config, visible_tiers, out)
    out.append("---")
    out.append("")
    applied = [layer for layer, _, status in provenance if status == "applied"]
    out.append("Layers applied: " + (", ".join(applied) if applied else "none") + ".")
    absent = [
        f"{layer} ({path})" for layer, path, status in provenance if status == "absent"
    ]
    if absent:
        out.append("")
        out.append("To change this policy, create: " + "; ".join(absent) + ".")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-root",
        default=os.getcwd(),
        help="Project root for the project config layer (default: cwd)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print layer provenance and the fully resolved config instead of guidance",
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="Print the three layer paths and exit",
    )
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).resolve()

    if args.paths:
        for layer, path in layer_paths(project_root):
            print(f"{layer}\t{path}")
        return 0

    try:
        config, provenance = resolve_config(project_root)
    except (ValueError, yaml.YAMLError) as exc:
        print(f"orchestration config error: {exc}", file=sys.stderr)
        return 1

    if args.explain:
        for layer, path, status in provenance:
            print(f"{layer:8} {status:8} {path}")
        print()
        # Detection status lives here rather than in the rendered guidance:
        # an undetected backend is omitted from the guidance entirely, so this
        # is the place to find out why one is missing.
        detected = detect_all(config)
        available = {str(b.get("id")) for b, ok, _ in detected if ok}
        for backend, ok, reason in detected:
            print(f"backend  {'available' if ok else 'MISSING':9} {backend.get('id')}: {reason}")
        for tier in active(config.get("tiers") or []):
            required = tier.get("backend")
            if not required:
                continue
            state = "available" if str(required) in available else "HIDDEN"
            print(f"tier     {state:9} {tier.get('id')}: requires backend {required}")
        print()
        print(yaml.safe_dump(config, sort_keys=False, allow_unicode=False, width=100))
        return 0

    print(render(config, provenance))
    return 0


if __name__ == "__main__":
    sys.exit(main())
