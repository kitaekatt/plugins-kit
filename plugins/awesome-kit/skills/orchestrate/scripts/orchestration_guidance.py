"""orchestration_guidance.py -- render the orchestrate skill's variable policy.

The `orchestrate` skill's durable half (economics, procedure, anti-patterns)
lives in SKILL.md. Its VARIABLE half is configuration, and this script renders
it as a DECISION TREE plus a machine-data section:

    decision  shape -> backend -> tier -> agent type -> effort -> announcement,
              derived from the orchestration principles and stated in the
              controlled vocabulary of `lexicon:`
    machine   which dispatch backends exist here and how to drive them, and how
              much usage capacity is left

Configuration resolves over three layers, later winning:

    1. shipped   <plugin>/skills/orchestrate/defaults/orchestration.yaml
    2. user      ~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml
    3. project   <project_root>/.claude/orchestration.yaml

Override files are sparse. Mappings deep-merge; the record lists in
RECORD_LISTS merge by record `id` (patch a known id, append a new one, drop one
with `disabled: true`); scalars and plain lists replace.

Usage:
    orchestration_guidance.py [--project-root PATH]
    orchestration_guidance.py --explain      # layer provenance + resolved config
    orchestration_guidance.py --paths        # where the layers are read from
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

# Records in these lists are identified by `id` rather than position, so an
# override patches a record instead of replacing the list. A list under one of
# these keys whose members carry no `id` still replaces outright (see
# merge_records), which is what keeps plain lists such as
# `capabilities.tiers` behaving as scalars.
RECORD_LISTS = (
    "tiers",
    "backends",
    "lexicon",
    "ladders",
    "rungs",
    "tests",
    "gates",
    "pulls",
    "items",
    "notes",
    "examples",
    "backend_notes",
)


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
    """Collapse a YAML block scalar to a single line."""
    return " ".join(str(text).split()) if text is not None else ""


def renders(record: Any) -> bool:
    """Does this record reach the artifact at all?

    `render_scope: principles-only` marks a record that is genuine policy but
    is NOT a per-unit routing decision, so it does not earn tokens in a file
    read once per orchestration. It stays in the data as the audit trail.
    """
    if not isinstance(record, dict):
        return True
    return str(record.get("render_scope") or "") != "principles-only"


def live(records: Any) -> List[Dict[str, Any]]:
    """Records that survive `disabled: true` AND `render_scope`."""
    return [r for r in active(records or []) if renders(r)]


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

_TERM_REF = re.compile(r"\{([a-z][a-z0-9-]*)\}")


class Terms:
    """The controlled vocabulary, with first-occurrence glossing.

    Every orchestration is a fresh read -- there is no accumulated vocabulary
    -- so a term whose natural reading diverges from its test carries its gloss
    the FIRST time it renders and goes bare afterwards. That says it once and
    costs less than a glossary block, which would pay for the term name twice.
    Because glossing is stateful, blocks must be rendered in document order.

    Only `kind: skill` terms select a branch, so only they render. A `concept`
    term degrades to its bare name rather than vanishing mid-sentence; the
    shipped data never places one in a rendered position.
    """

    def __init__(self, lexicon: Any) -> None:
        self.records: Dict[str, Dict[str, Any]] = {}
        for record in live(lexicon):
            if "id" in record:
                self.records[str(record["id"])] = record
        self.seen: set = set()

    def is_skill(self, term_id: str) -> bool:
        record = self.records.get(str(term_id))
        return bool(record) and str(record.get("kind") or "skill") == "skill"

    def term(self, term_id: Any) -> str:
        term_id = str(term_id)
        record = self.records.get(term_id)
        text = f"`{term_id}`"
        if record is None or not self.is_skill(term_id):
            return text
        gloss = fold(record.get("gloss"))
        if gloss and str(record.get("render") or "bare") == "glossed" and term_id not in self.seen:
            text += f" ({gloss})"
        self.seen.add(term_id)
        return text

    def fill(self, text: Any) -> str:
        """Expand `{term-id}` references in prose, glossing on first sight."""
        return _TERM_REF.sub(lambda m: self.term(m.group(1)), fold(text))

    def skill_terms(self, ids: Iterable[Any]) -> List[str]:
        return [str(i) for i in ids if self.is_skill(i)]


# --------------------------------------------------------------------------
# The decision tree
# --------------------------------------------------------------------------


class Blocks:
    """Numbers the top-level blocks as they are emitted.

    Numbering is assigned at render time rather than stored, because the
    backend block disappears when there is only one backend and a stored
    number would then leave a hole.
    """

    def __init__(self, out: List[str]) -> None:
        self.out = out
        self.n = 0

    def heading(self, title: str) -> None:
        self.n += 1
        self.out.append(f"## {self.n}. {title}")
        self.out.append("")


def visible_rungs(config: Dict[str, Any], available_backends: set) -> set:
    """Rung ids that actually render.

    A ladder whose backend is not detected disappears whole -- its rungs
    cannot be dispatched to, and naming one would leak the absent backend into
    the guidance through a section that does not otherwise consult the gate.
    """
    ids = set()
    for ladder in live(config.get("ladders")):
        if str(ladder.get("id")) not in available_backends:
            continue
        for rung in live(ladder.get("rungs")):
            ids.add(str(rung.get("id")))
    return ids


def render_shape(
    config: Dict[str, Any], terms: Terms, available_backends: set, blocks: Blocks, out: List[str]
) -> None:
    """Block 0 -- shaping the unit, before anything else.

    Most routing defects trace to a unit that was never shaped: the later
    blocks then argue about model capability when the real problem is an
    ill-formed brief.
    """
    block = config.get("shape") or {}
    if not renders(block):
        return
    tests = live(block.get("tests"))
    if not tests:
        return
    blocks.heading(fold(block.get("title")) or "Shape the unit")
    if block.get("intro"):
        out.append(terms.fill(block["intro"]))
        out.append("")
    for test in tests:
        line = "- " + terms.fill(test.get("text"))
        # A clause that renders only when the named backend is ABSENT: the
        # tree teaches a test, so going silent on a positive result reads as
        # an oversight and invites the reader to invent an answer.
        for backend_id, clause in (test.get("without_backend") or {}).items():
            if str(backend_id) not in available_backends:
                line += " " + terms.fill(clause)
        out.append(line)
    out.append("")


def _group_by_backend(rows: List[Dict[str, Any]]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    order: List[str] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        bid = str(row.get("backend"))
        if bid not in grouped:
            grouped[bid] = []
            order.append(bid)
        grouped[bid].append(row)
    return [(bid, grouped[bid]) for bid in order]


def render_backend_choice(
    config: Dict[str, Any],
    terms: Terms,
    available_backends: set,
    backend_names: Dict[str, str],
    blocks: Blocks,
    out: List[str],
) -> None:
    """Block 1 -- where the work RUNS.

    Omitted entirely when the backend it selects for is absent: with one
    backend there is nothing to choose, and a row naming an uninstalled tool
    invites dispatch to it.
    """
    block = config.get("backend") or {}
    if not block or not renders(block):
        return
    required = block.get("requires_backend")
    if required and str(required) not in available_backends:
        return

    sections = []
    for key, intro_key in (("gates", "gates_intro"), ("pulls", "pulls_intro")):
        rows = [r for r in live(block.get(key)) if str(r.get("backend")) in available_backends]
        if rows:
            sections.append((fold(block.get(intro_key)), rows))
    if not sections:
        return

    blocks.heading(fold(block.get("title")) or "Backend")
    if block.get("intro"):
        out.append(terms.fill(block["intro"]))
    default = str(block.get("default") or "")
    if default in available_backends:
        out.append(f"Default: **{backend_names.get(default, default)}**.")
    out.append("")
    for intro, rows in sections:
        for bid, group in _group_by_backend(rows):
            listed = ", ".join(terms.term(r.get("term")) for r in group)
            name = backend_names.get(bid, bid)
            out.append(f"- {intro} **{name}**: {listed}.")
    out.append("")


class UnrenderableRung(Exception):
    """A rung whose test cannot be rendered faithfully. Fails CLOSED -- see rung_criteria."""


def rung_criteria(rung: Dict[str, Any], terms: Terms) -> str:
    """The rung's test: OR'd groups of AND'd terms, plus any shape restriction.

    Fails CLOSED. A criteria group is a CONJUNCTION, so dropping one unresolvable
    conjunct would render a strictly WIDER test than the data specifies -- on this
    ladder that silently widens the gate on the most expensive rung, which is the
    exact direction the guards exist to prevent. An id that does not resolve to a
    live `[skill]` term therefore invalidates its whole group; a non-terminal rung
    left with no group at all raises, because an empty test under first-match-wins
    reads as an unconditional match rather than as a missing one.
    """
    groups: List[str] = []
    dropped: List[str] = []
    for group in rung.get("criteria") or []:
        if isinstance(group, dict):
            ids, where = group.get("terms") or [], fold(group.get("where"))
        else:
            ids, where = group, ""
        ids = list(ids or [])
        resolved = terms.skill_terms(ids)
        if len(resolved) != len(ids):
            # Partial resolution would widen the conjunction -- drop the group whole.
            dropped.extend(t for t in ids if t not in set(resolved))
            continue
        text = " + ".join(terms.term(t) for t in resolved)
        if not text:
            continue
        if where:
            text += f" where {where}"
        groups.append(text)

    declared = bool(rung.get("criteria"))
    if declared and not groups and not rung.get("terminal"):
        # Every group was invalidated. `shape` must NOT stand in as the test: it
        # is a NARROWING clause on a criteria match, so alone it renders as
        # "<shape> work only" -- which on the top rung matches every unit of that
        # shape. That is the same widening this function exists to prevent,
        # arriving through a different door.
        raise UnrenderableRung(
            f"rung {rung.get('id')!r} declares criteria but none resolved"
            + (f" (unresolved terms: {', '.join(sorted(set(dropped)))})" if dropped else "")
            + ". A non-terminal rung must state a test; only a terminal rung may state none."
        )

    body = "; or ".join(groups)
    if rung.get("shape"):
        clause = f"{terms.term(rung['shape'])} work only"
        body = f"{body}; {clause}" if body else clause

    if not body and not rung.get("terminal"):
        raise UnrenderableRung(
            f"rung {rung.get('id')!r} has no renderable criteria. "
            "A non-terminal rung must state a test; only a terminal rung may state none."
        )
    return body


def render_rung(rung: Dict[str, Any], index: int, terms: Terms, out: List[str]) -> None:
    head = f"**{fold(rung.get('model')) or str(rung.get('id'))}**"
    if rung.get("effort"):
        head += f" at `{fold(rung['effort'])}` effort"
    parts = [p for p in (rung_criteria(rung, terms), terms.fill(rung.get("text"))) if p]
    line = f"{index}. {head} -- " + ". ".join(parts) if parts else f"{index}. {head}"
    if rung.get("announce_as"):
        forms = " or ".join(
            "`(" + ", ".join(str(t) for t in form) + ")`" for form in rung["announce_as"]
        )
        line = line.rstrip(".") + f". Announced as {forms}"
    out.append(line.rstrip(".") + ".")
    if rung.get("gate"):
        out.append(f"   - Gate: {terms.fill(rung['gate'])}")
    for note in live(rung.get("notes")):
        out.append(f"   - {terms.fill(note.get('text'))}")
    # Negative guards render unconditionally. A rung something must NOT be
    # used for is a decision, not rationale: without it a reader invents the
    # dispatch the guard exists to prevent.
    for guard in rung.get("guards") or []:
        out.append(f"   - {terms.fill(guard)}")


def render_tiers(
    config: Dict[str, Any],
    terms: Terms,
    available_backends: set,
    blocks: Blocks,
    out: List[str],
) -> None:
    """Block 2 -- the tier, one subtree per available backend."""
    ladders = [l for l in live(config.get("ladders")) if str(l.get("id")) in available_backends]
    if not ladders:
        return
    blocks.heading("Tier")
    multi = len(ladders) > 1
    for ladder in ladders:
        rungs = live(ladder.get("rungs"))
        if not rungs:
            continue
        if multi:
            out.append(f"### {fold(ladder.get('label')) or str(ladder.get('id'))} ladder")
            out.append("")
        for index, rung in enumerate(rungs, 1):
            render_rung(rung, index, terms, out)
        out.append("")
        guards = [terms.fill(g) for g in (ladder.get("guards") or [])]
        for note in live(ladder.get("notes")):
            guards.append(terms.fill(note.get("text")))
        if guards:
            out.append(" ".join(guards))
            out.append("")


def render_agent_types(
    config: Dict[str, Any], terms: Terms, blocks: Blocks, out: List[str]
) -> None:
    """Block 3 -- which dispatch, once the tier has decided which model."""
    block = config.get("agent_types") or {}
    if not renders(block):
        return
    items = live(block.get("items"))
    if not items:
        return
    blocks.heading(fold(block.get("title")) or "Agent type")
    if block.get("intro"):
        out.append(terms.fill(block["intro"]))
        out.append("")
    for item in items:
        out.append(f"- `{fold(item.get('name')) or item.get('id')}` -- {terms.fill(item.get('text'))}")
    out.append("")


def render_effort(
    config: Dict[str, Any],
    terms: Terms,
    available_backends: set,
    blocks: Blocks,
    out: List[str],
) -> None:
    """Block 4 -- effort, orthogonal to tier and decided after it.

    Deliberately not a tree node: its criteria partly overlap tier criteria
    with different consequences, and forcing it in would require a cross-edge.
    """
    block = config.get("effort") or {}
    if not block or not renders(block):
        return
    if not isinstance(block, dict):
        blocks.heading("Effort")
        out.append(fold(block))
        out.append("")
        return
    blocks.heading(fold(block.get("title")) or "Effort")
    if block.get("intro"):
        out.append(terms.fill(block["intro"]))
        out.append("")
    for note in live(block.get("backend_notes")):
        if str(note.get("backend")) in available_backends:
            out.append(terms.fill(note.get("text")))
            out.append("")
    for key in ("note", "up_effort_note"):
        if block.get(key):
            out.append(terms.fill(block[key]))
            out.append("")
    for key, label in (("raise_when", "Raise"), ("lower_when", "Lower")):
        rows = [terms.fill(r) for r in (block.get(key) or [])]
        if rows:
            out.append(f"- {label}: " + "; ".join(rows) + ".")
    if block.get("raise_when") or block.get("lower_when"):
        out.append("")


def render_announce(
    config: Dict[str, Any],
    terms: Terms,
    available_backends: set,
    blocks: Blocks,
    out: List[str],
) -> None:
    """Block 5 -- the announcement form.

    The one place a worked example renders, because here the form IS the
    content. A closed vocabulary is what makes the lines aggregate into a
    usage record.
    """
    block = config.get("announce") or {}
    if not block or not renders(block):
        return
    examples = [
        e
        for e in live(block.get("examples"))
        if not e.get("requires_backend") or str(e["requires_backend"]) in available_backends
    ]
    if not block.get("form") and not examples:
        return
    blocks.heading(fold(block.get("title")) or "Announce every dispatch")
    if block.get("form"):
        out.append("```")
        out.append(fold(block["form"]))
        out.append("```")
        out.append("")
    if block.get("rule"):
        out.append(terms.fill(block["rule"]))
        out.append("")
    for note in live(block.get("backend_notes")):
        if str(note.get("backend")) in available_backends:
            out.append(terms.fill(note.get("text")))
            out.append("")
    if examples:
        out.append("```")
        for example in examples:
            out.append(fold(example.get("text")))
        out.append("```")
        out.append("")


def render_decision_tree(
    config: Dict[str, Any],
    available_backends: set,
    backend_names: Dict[str, str],
    out: List[str],
) -> None:
    """The derived half, in principle order: shape -> backend -> tier ->
    agent type -> effort -> announcement.

    Rendered strictly in document order because glossing is first-occurrence
    and therefore stateful.
    """
    terms = Terms(config.get("lexicon"))
    if config.get("resolution"):
        out.append(f"**Resolution.** {fold(config['resolution'])}")
        out.append("")
    blocks = Blocks(out)
    render_shape(config, terms, available_backends, blocks, out)
    render_backend_choice(config, terms, available_backends, backend_names, blocks, out)
    render_tiers(config, terms, available_backends, blocks, out)
    render_agent_types(config, terms, blocks, out)
    render_effort(config, terms, available_backends, blocks, out)
    render_announce(config, terms, available_backends, blocks, out)


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

    # Choosing BETWEEN backends is a decision and lives in the decision tree;
    # this section is the machine data for the ones that are present.
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


# Schema-1 top-level keys. The decision half was reshaped in schema 2 and none of
# these is read any more, so an override written against schema 1 deep-merges
# cleanly and then contributes nothing. Silence there is the worst outcome: the
# user's policy is not in force and nothing says so.
LEGACY_SCHEMA_1_KEYS = (
    "tiers",
    "default_tier",
    "backend_selection",
    "implementation",
    "pool_economics",
)


def legacy_schema_keys(config: Dict[str, Any]) -> List[str]:
    """Schema-1 keys present in the merged config, in declaration order."""
    return [k for k in LEGACY_SCHEMA_1_KEYS if k in config]


def render(config: Dict[str, Any], provenance: List[Tuple[str, Path, str]]) -> str:
    out: List[str] = ["# Orchestration policy (generated)", ""]
    detected = detect_all(config)
    available_backends = {str(b.get("id")) for b, ok, _ in detected if ok}
    backend_names = {
        str(b.get("id")): str(b.get("name") or b.get("id")) for b, _, _ in detected
    }
    # One visibility set, computed once and threaded through every section, so
    # no render path can name a rung the gate removed.
    visible_tiers = visible_rungs(config, available_backends)
    render_decision_tree(config, available_backends, backend_names, out)
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
    stale = legacy_schema_keys(config)
    if stale:
        overrides = [
            f"{layer} ({path})"
            for layer, path, status in provenance
            if status == "applied" and layer != "shipped"
        ]
        out.append("")
        out.append(
            "**Stale override -- NOT IN FORCE.** A layer sets schema-1 key(s) "
            + ", ".join(f"`{k}`" for k in stale)
            + ", which schema 2 no longer reads; those settings contribute nothing to "
            "the policy above. Port them to the schema-2 sections "
            "(`lexicon`, `shape`, `backend`, `ladders`, `agent_types`, `effort`, "
            "`announce`) -- see references/configuration.md."
            + (" Layer(s): " + "; ".join(overrides) + "." if overrides else "")
        )
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
        for ladder in active(config.get("ladders") or []):
            required = str(ladder.get("id"))
            state = "available" if required in available else "HIDDEN"
            for rung in active(ladder.get("rungs") or []):
                print(f"rung     {state:9} {rung.get('id')}: requires backend {required}")
        print()
        print(yaml.safe_dump(config, sort_keys=False, allow_unicode=False, width=100))
        return 0

    try:
        text = render(config, provenance)
    except UnrenderableRung as exc:
        # Fail CLOSED and LOUDLY. Rendering a partial policy would hand the
        # orchestrator a ladder whose most-guarded rung had quietly widened,
        # which is worse than no policy at all.
        print(f"orchestration config error: {exc}", file=sys.stderr)
        print(
            "A layer has removed or renamed a lexicon term a rung's criteria "
            "depend on. Run --explain to see which layers applied.",
            file=sys.stderr,
        )
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
