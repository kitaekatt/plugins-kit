"""Render the orchestrate skill's variable policy.

The `orchestrate` skill's durable half (economics, procedure, anti-patterns)
lives in SKILL.md. Its variable half is configuration, and this script renders
an ordered routing policy plus machine data:

    routing  ordered shape rows, with model priority and fallthrough
    machine  which dispatch backends exist here and how to drive them, and how
             much usage capacity is left

Configuration resolves over four layers, later winning:

    1. shipped   <plugin>/skills/orchestrate/defaults/orchestration.yaml
    2. machine   ~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml
    3. user      ~/.claude/config/orchestration.yaml
    4. project   <project_root>/.claude/orchestration.yaml

Override files are sparse. Mappings deep-merge; the record lists in
RECORD_LISTS merge by record `id` (patch a known id, append a new one, drop one
with `disabled: true`); scalars and plain lists replace.

Usage:
    orchestration_guidance.py [--project-root PATH] [--self ENDPOINT_OR_MODEL]
    orchestration_guidance.py --explain      # layer provenance + resolved config
    orchestration_guidance.py --paths        # where the layers are read from
"""

import argparse
from functools import partial
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

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
# merge_records), which is what keeps capability lists behaving as scalars.
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

DECISION_KEYS = frozenset(
    (
        "resolution",
        "lexicon",
        "shape",
        "routing",
        "agent_types",
        "effort",
        "announce",
        "review_overlap",
    )
)

# Capability keys rendered for each backend's `capabilities:` block, in
# display order. A key present in the shipped defaults but absent from this
# tuple is silently dropped from the rendered output -- see the render site
# below and TestCapabilityRendering in tests/awesome-kit/test_orchestration_guidance.py.
CAPABILITY_KEYS = ("isolation", "effort", "network", "concurrency", "returns")


# --------------------------------------------------------------------------
# Layer resolution
# --------------------------------------------------------------------------


def user_config_dir_path() -> Path:
    """The conventional home: the user's tracked config directory."""
    return Path.home() / ".claude" / "config" / CONFIG_NAME


def machine_config_path() -> Path:
    """The machine-local configuration path in the plugin data directory."""
    return (
        Path.home()
        / ".claude"
        / "plugins"
        / "data"
        / MARKETPLACE
        / PLUGIN
        / CONFIG_NAME
    )


def legacy_user_config_path() -> Path:
    """Compatibility alias for the former plugin-data user-layer path."""
    return machine_config_path()


def user_config_path() -> Path:
    """The portable user configuration path.

    ``~/.claude/config/`` is the established home for portable user
    configuration. A plugin's data directory holds machine-global values that
    deliberately stay out of version control (bootstrap's manifest-reference
    calls out that split), so it is resolved as the separate machine layer.
    """
    return user_config_dir_path()


def machine_layer_status(data: Mapping[str, Any]) -> str:
    """Describe an applied machine layer and flag portable policy content."""
    status = "applied (machine-local)"
    if DECISION_KEYS.intersection(data):
        return (
            "applied (machine-local; NOTE: decision-half keys found; keep portable "
            f"policy in {user_config_dir_path()})"
        )
    return status


def project_config_path(project_root: Path) -> Path:
    return project_root / ".claude" / CONFIG_NAME


def layer_paths(project_root: Path) -> List[Tuple[str, Path]]:
    """The four layers in precedence order (lowest first)."""
    return [
        ("shipped", DEFAULTS_PATH),
        ("machine", machine_config_path()),
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
    (the shipped, machine, and user layers) is a different question from repo-level
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


def status_is_applied(status: str) -> bool:
    """True when a provenance status means the layer WAS merged.

    An applied layer may carry a decorated status -- the project layer's
    stripped-executable-fields note, or the machine layer's provenance note.
    Matching the bare string "applied" reports a decorated layer as not
    applied, which is how a user's in-force policy came to be omitted from the
    "Layers applied" line. Every decoration must keep the "applied" prefix.
    """
    return status.startswith("applied")


def resolve_config(project_root: Path) -> Tuple[Dict[str, Any], List[Tuple[str, Path, str]]]:
    """Merge the layers. Returns (config, provenance) where provenance is
    (layer, path, status) with status in {applied, empty, absent}, or
    ('project', path, 'applied (N executable field(s) ignored)') or
    ('machine', path, 'applied (machine-local; ...)')."""
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
        if layer == "machine":
            status = machine_layer_status(data)
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
        # A batch launcher is not an executable: CreateProcess refuses a bare
        # `.cmd`/`.bat`, so it must be handed to the shell. Resolving without
        # this reports a CLI absent on exactly the machines where it was
        # installed by npm or scoop.
        #
        # Deliberately duplicated from bootstrap_lib.codex.resolve_cli rather
        # than imported: this module is stdlib-only by design and detect_backend
        # is generic over every backend, not codex-specific. Change both
        # together. That module additionally REFUSES cmd metacharacters, which
        # is unnecessary here -- this argv comes from trusted config (the
        # project layer cannot declare detect.command) and carries no
        # caller-supplied path.
        if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", *argv]
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
    return False, (
        "`detect` declares no recognized rule "
        "(always / command / path)"
    )


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
# Routing model resolution
# --------------------------------------------------------------------------


AGENT_MODEL_NAMES = frozenset(("fable", "opus", "sonnet", "haiku"))
AGENT_MODEL_PREFIX = "agent:"
HARNESS_NAMES = frozenset(("codex", "opencode"))


def _backend_id(backend: Mapping[str, Any]) -> str:
    """Return the display and routing id used for a backend record."""
    return str(backend.get("id", "?"))


def _backend_has_dispatch_mechanics(
    backend: Mapping[str, Any], rendered_command: Optional[str] = None
) -> bool:
    """Whether a backend record or its rendered output can drive a unit."""
    return any(
        fold(value)
        for value in (rendered_command, backend.get("command"), backend.get("dispatch"))
    )


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    """Read a model definition from either a dataclass or a mapping."""
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _project_root_from_provenance(
    provenance: List[Tuple[str, Path, str]],
) -> Path:
    for layer, path, _status in provenance:
        if layer == "project":
            # project_config_path() is <root>/.claude/orchestration.yaml.
            return path.parent.parent
    return Path.cwd()


def discover_model_definitions(project_root: Path) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    """Load harness model definitions when the shared library has the feature.

    The import is deliberately not the feature test. A stale shared-library
    copy can remain importable after its owner plugin is removed, and an older
    copy can import successfully while lacking the discovery and kind symbols
    this renderer needs. In either case routing falls back to its Agent-tool
    members and `--explain` receives a diagnostic.
    """
    try:
        import llm_scripting_kit as model_kit  # noqa: PLC0415
    except ImportError as exc:
        return {}, [
            "llm_scripting_kit unavailable; harness model rows skipped "
            f"({type(exc).__name__}). Install the owning plugin with "
            "`claude plugin install llm-scripting-kit@plugins-kit` and start a "
            "new session to restore those rows."
        ]

    discover = getattr(model_kit, "discover_model_entries", None)
    harness_kind = getattr(model_kit, "HARNESS_KIND", None)
    entry_type = getattr(model_kit, "EndpointEntry", None)
    if not callable(discover) or harness_kind != "harness" or entry_type is None:
        return {}, [
            "llm_scripting_kit is importable but lacks the harness-model discovery "
            "feature, so the linked shared library is older than this policy "
            "renderer requires; harness model rows skipped. Update it with "
            "`claude plugin update llm-scripting-kit@plugins-kit` and start a new "
            "session."
        ]

    try:
        discovery = discover(project_root=str(project_root))
    except Exception as exc:  # noqa: BLE001 -- stale/version-skewed shared libs degrade
        return {}, [
            "llm_scripting_kit harness-model discovery failed; harness model rows skipped "
            f"({type(exc).__name__}: {exc})"
        ]

    entries = getattr(discovery, "entries", discovery)
    if not isinstance(entries, Mapping):
        return {}, [
            "llm_scripting_kit harness-model discovery returned an unsupported value; "
            "harness model rows skipped"
        ]

    notes = [str(note) for note in (getattr(discovery, "notes", []) or [])]
    normalized: Dict[str, Dict[str, str]] = {}
    for raw_id, entry in entries.items():
        entry_id = str(raw_id)
        kind = _record_value(entry, "kind")
        if kind != harness_kind:
            continue
        harness = _record_value(entry, "harness")
        model = _record_value(entry, "model")
        if not isinstance(harness, str) or not harness:
            notes.append(f"model entry `{entry_id}` skipped: no harness")
            continue
        if not isinstance(model, str) or not model:
            notes.append(f"model entry `{entry_id}` skipped: no model")
            continue
        normalized[entry_id] = {
            "id": entry_id,
            "harness": harness,
            "model": model,
        }
        effort = _record_value(entry, "effort")
        if isinstance(effort, str) and effort:
            normalized[entry_id]["effort"] = effort
    return normalized, notes


def detect_harnesses(
    config: Dict[str, Any],
    detected: List[Tuple[Dict[str, Any], bool, str]],
    model_entries: Mapping[str, Mapping[str, str]],
) -> Dict[str, Tuple[bool, str]]:
    """Detect each harness named by the discovered model definitions.

    A configured backend record wins when one exists. A harness known only
    through the model registry (no corresponding machine record) is still
    probed, reusing the existing fail-closed command detector with the CLI's
    version command -- but presence alone renders an identity-only backend
    section, and resolve_routing_models does not route to it: dispatch
    mechanics live in a `backends[]` record, and a model a row prefers with
    no way to drive it is worse than an absent one. This checks presence
    only; it never probes a model server.
    """
    by_id = {str(backend.get("id")): (ok, reason) for backend, ok, reason in detected}
    records = {
        str(backend.get("id")): backend
        for backend in active(config.get("backends") or [])
    }
    result: Dict[str, Tuple[bool, str]] = {}
    for entry in model_entries.values():
        harness = str(entry.get("harness"))
        if harness in result:
            continue
        if harness not in HARNESS_NAMES:
            result[harness] = (False, f"unknown harness `{harness}`")
            continue
        if harness in by_id:
            result[harness] = by_id[harness]
            continue
        record = records.get(harness)
        if record is not None:
            result[harness] = detect_backend(record)
            continue
        result[harness] = detect_backend(
            {"id": harness, "detect": {"command": [harness, "--version"]}}
        )
    return result


def resolve_routing_models(
    config: Dict[str, Any],
    model_entries: Mapping[str, Mapping[str, str]],
    harness_status: Mapping[str, Tuple[bool, str]],
    routable_ids: Optional[set] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Resolve routing rows, retaining order and model priority.

    Agent-tool names are a reserved namespace. Every other name is looked up
    in the shared model registry and must resolve to a harness entry whose CLI
    is present AND whose active backend yields drivable mechanics: a rendered
    command, a record `command`, or dispatch prose. Invalid model members are
    skipped individually; a row with no surviving models is omitted. When
    `routable_ids` is omitted, this function derives it with the same detection
    statuses and adapter-rendered commands used by `render()`.
    """
    configured_backend_ids = {
        _backend_id(backend) for backend in active(config.get("backends") or [])
    }
    if routable_ids is None:
        detected: List[Tuple[Dict[str, Any], bool, str]] = []
        for backend in active(config.get("backends") or []):
            backend_id = _backend_id(backend)
            available, reason = harness_status.get(
                backend_id,
                (False, f"harness `{backend_id}` is not detected"),
            )
            detected.append((backend, available, reason))
        command_text_provider = partial(
            adapter_command_text_provider,
            model_entries=model_entries,
        )
        rendered_commands = _rendered_backend_command_texts(
            detected, command_text_provider
        )
        routable_ids = _routable_backend_ids(detected, rendered_commands)
    terms = Terms(config.get("lexicon"))
    routes: List[Dict[str, Any]] = []
    notes: List[str] = []
    raw_rows = config.get("routing") or []
    if not isinstance(raw_rows, list):
        return [], ["routing skipped: expected a list"]

    for row_number, raw_row in enumerate(raw_rows, 1):
        if not isinstance(raw_row, dict):
            notes.append(f"routing row {row_number} skipped: expected a mapping")
            continue
        raw_shape = raw_row.get("shape", [])
        if raw_shape is None:
            raw_shape = []
        if not isinstance(raw_shape, list) or any(
            not isinstance(term, str) or not term for term in raw_shape
        ):
            notes.append(f"routing row {row_number} skipped: shape must be a list of names")
            continue
        shape = [str(term) for term in raw_shape]
        unresolved_shape = [term for term in shape if not terms.is_skill(term)]
        if unresolved_shape:
            notes.append(
                f"routing row {row_number} skipped: unknown or non-skill shape term(s) "
                + ", ".join(f"`{term}`" for term in unresolved_shape)
            )
            continue

        raw_models = raw_row.get("models")
        if not isinstance(raw_models, list):
            notes.append(f"routing row {row_number} skipped: models must be a list")
            continue
        models: List[Dict[str, str]] = []
        inline_notes: List[str] = []
        for raw_model in raw_models:
            if not isinstance(raw_model, str) or not raw_model:
                notes.append(f"routing row {row_number}: invalid model name skipped")
                continue
            if raw_model.startswith(AGENT_MODEL_PREFIX):
                entry_id = raw_model[len(AGENT_MODEL_PREFIX):]
                if entry_id not in AGENT_MODEL_NAMES:
                    notes.append(
                        f"routing row {row_number}: `{raw_model}` skipped; unknown Agent-tool model"
                    )
                    continue
                models.append({"id": entry_id, "target": entry_id, "kind": "agent"})
                continue
            if ":" in raw_model:
                notes.append(
                    f"routing row {row_number}: `{raw_model}` skipped; only `agent:` is a reserved namespace"
                )
                continue
            entry = model_entries.get(raw_model)
            if entry is None:
                notes.append(
                    f"routing row {row_number}: `{raw_model}` skipped; no harness model entry resolves"
                )
                continue
            harness = str(entry.get("harness") or "")
            available, reason = harness_status.get(
                harness, (False, f"harness `{harness}` is not detected")
            )
            if not available:
                notes.append(
                    f"routing row {row_number}: `{raw_model}` skipped; {reason}"
                )
                continue
            if harness not in configured_backend_ids:
                note = (
                    f"`{raw_model}` skipped; harness `{harness}` has no "
                    "backends[] record, so no dispatch mechanics render for it "
                    "(see references/configuration.md)"
                )
                notes.append(
                    f"routing row {row_number}: {note}"
                )
                inline_notes.append(note)
                continue
            if harness not in routable_ids:
                notes.append(
                    f"routing row {row_number}: `{raw_model}` skipped; harness "
                    f"`{harness}` has no drivable dispatch mechanics: no rendered "
                    "command, `command`, or dispatch prose"
                )
                continue
            models.append(
                {
                    "id": raw_model,
                    "target": f"{harness}/{raw_model}",
                    "kind": "harness",
                    "harness": harness,
                    "model": str(entry.get("model")),
                }
            )
        if not models:
            notes.append(f"routing row {row_number} skipped: no model resolves")
            continue
        routes.append(
            {
                "number": row_number,
                "shape": shape,
                "models": models,
                "gate": raw_row.get("gate"),
                "guards": list(raw_row.get("guards") or []),
                "inline_notes": inline_notes,
            }
        )
    return routes, notes


def announcement_text(
    what: str,
    target: str,
    shape_terms: Iterable[str],
    fell_through_from: Optional[str] = None,
) -> str:
    """Build the stable dispatch announcement, including fallback context."""
    terms = list(shape_terms)
    parenthetical = ", ".join(terms) if terms else "default"
    if fell_through_from:
        parenthetical += f"; fell through from {fell_through_from}"
    return f"delegating {what} to {target} ({parenthetical})"


def _resolved_model_ids(routes: Iterable[Mapping[str, Any]]) -> set:
    """Return the model ids that survive routing resolution."""
    return {
        str(model["id"])
        for route in routes
        for model in (route.get("models") or [])
        if isinstance(model, Mapping) and model.get("id")
    }


def _requires_resolved_models(record: Mapping[str, Any], resolved_models: set) -> bool:
    """Whether a record's optional model dependencies all survived resolution."""
    required = record.get("requires_model")
    if required is None:
        return True
    required_models = required if isinstance(required, list) else [required]
    return all(str(model) in resolved_models for model in required_models)


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


def render_routing(
    config: Dict[str, Any],
    terms: Terms,
    routes: List[Dict[str, Any]],
    blocks: Blocks,
    out: List[str],
) -> None:
    """Render the ordered shape list and its model priority."""
    if not routes:
        return
    blocks.heading("Routing")
    out.append(
        "Evaluate rows in order; the first matching shape wins. Within a row, "
        "try models in the order shown. On a launch or transport error, continue "
        "to the next model; the fallback announcement names the model immediately "
        "before the fallback."
    )
    out.append("")
    for index, route in enumerate(routes, 1):
        shape = route.get("shape") or []
        shape_text = " + ".join(terms.term(term) for term in shape) if shape else "anything"
        models = route.get("models") or []
        targets = [f"**{model['target']}**" for model in models]
        if len(targets) == 1:
            dispatch = targets[0]
        else:
            dispatch = ", then ".join(targets)
        out.append(f"{index}. If {shape_text}: try {dispatch}.")
        for note in route.get("inline_notes") or []:
            out.append(f"   - {note}")
        if route.get("gate"):
            out.append(f"   - Gate: {terms.fill(route['gate'])}")
        for guard in route.get("guards") or []:
            out.append(f"   - {terms.fill(guard)}")
    out.append("")


def render_agent_types(
    config: Dict[str, Any], terms: Terms, blocks: Blocks, out: List[str]
) -> None:
    """Block 3 -- which Agent-tool role applies to the selected model."""
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
    routes: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Block 4 -- effort, orthogonal to routing and decided after it.

    Deliberately not a tree node: its signals partly overlap route signals with
    different consequences, and forcing it in would require a cross-edge.
    """
    block = config.get("effort") or {}
    if not block or not renders(block):
        return
    if not isinstance(block, dict):
        blocks.heading("Effort")
        out.append(fold(block))
        out.append("")
        return
    resolved_models = _resolved_model_ids(routes or [])
    blocks.heading(fold(block.get("title")) or "Effort")
    if block.get("intro"):
        out.append(terms.fill(block["intro"]))
        out.append("")
    for note in live(block.get("backend_notes")):
        if (
            str(note.get("backend")) in available_backends
            and _requires_resolved_models(note, resolved_models)
        ):
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
    routes: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Block 5 -- the announcement form.

    The one place a worked example renders, because here the form IS the
    content. A closed vocabulary is what makes the lines aggregate into a
    usage record.
    """
    block = config.get("announce") or {}
    if not block or not renders(block):
        return
    routes = routes or []
    resolved_models = _resolved_model_ids(routes)
    examples = [
        e
        for e in live(block.get("examples"))
        if (
            (not e.get("requires_backend") or str(e["requires_backend"]) in available_backends)
            and _requires_resolved_models(e, resolved_models)
        )
    ]
    if not block.get("form") and not examples and not routes:
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
        if (
            str(note.get("backend")) in available_backends
            and _requires_resolved_models(note, resolved_models)
        ):
            out.append(terms.fill(note.get("text")))
            out.append("")
    if examples:
        out.append("```")
        for example in examples:
            out.append(fold(example.get("text")))
        out.append("```")
        out.append("")


def render_review_overlap(config: Dict[str, Any], out: List[str]) -> None:
    """Render the configured posture for units overlapping a review."""
    block = config.get("review_overlap") or {}
    if not isinstance(block, dict):
        return
    modes = block.get("modes") or {}
    if not isinstance(modes, dict):
        return
    mode = block.get("mode")
    if not isinstance(mode, str) or not mode:
        mode = "premise-safe"
    guidance = modes.get(mode)
    if not isinstance(guidance, str) or not guidance:
        mode = "premise-safe"
        guidance = modes.get(mode)
    if not isinstance(guidance, str) or not guidance:
        return
    out.append(f"**Review overlap (`{mode}`).** {fold(guidance)}")
    out.append("")


def render_decision_tree(
    config: Dict[str, Any],
    available_backends: set,
    backend_names: Dict[str, str],
    out: List[str],
    routes: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Render the policy in document order: shape -> routing -> agent type ->
    effort -> announcement -> review overlap.

    Rendered strictly in document order because glossing is first-occurrence
    and therefore stateful.
    """
    terms = Terms(config.get("lexicon"))
    if config.get("resolution"):
        out.append(f"**Resolution.** {fold(config['resolution'])}")
        out.append("")
    blocks = Blocks(out)
    render_shape(config, terms, available_backends, blocks, out)
    render_routing(config, terms, routes or [], blocks, out)
    render_agent_types(config, terms, blocks, out)
    render_effort(config, terms, available_backends, blocks, out, routes)
    render_announce(config, terms, available_backends, blocks, out, routes)
    render_review_overlap(config, out)


def detect_all(config: Dict[str, Any]) -> List[Tuple[Dict[str, Any], bool, str]]:
    """Run every enabled backend's detect rule once, in config order."""
    results = []
    for backend in active(config.get("backends") or []):
        ok, reason = detect_backend(backend)
        results.append((backend, ok, reason))
    return results


def default_command_text_provider(backend: Dict[str, Any]) -> Optional[str]:
    """Return the hand-authored command used when adapter rendering is unavailable."""
    command = backend.get("command")
    return str(command) if command else None


def _rendered_backend_command_texts(
    detected: List[Tuple[Dict[str, Any], bool, str]],
    command_text_provider: Callable[[Dict[str, Any]], Optional[str]],
) -> Dict[str, Optional[str]]:
    """Render each available backend command once for routing and output."""
    return {
        _backend_id(backend): command_text_provider(backend)
        for backend, available, _reason in detected
        if available
    }


def _routable_backend_ids(
    detected: List[Tuple[Dict[str, Any], bool, str]],
    rendered_commands: Mapping[str, Optional[str]],
) -> set:
    """Return available backends with a command or dispatch prose."""
    return {
        _backend_id(backend)
        for backend, available, _reason in detected
        if available
        and _backend_has_dispatch_mechanics(
            backend, rendered_commands.get(_backend_id(backend))
        )
    }


def _placeholder_path(label: str) -> str:
    """Build an absolute, machine-independent path for adapter rendering."""
    return os.path.join(os.path.sep, "__orchestrate_placeholder__", label)


def _adapter_entry(
    entry_type: Any,
    harness_kind: str,
    entry_id: str,
    definition: Any,
    harness: str,
) -> Any:
    """Turn a discovered definition into the adapter's EndpointEntry type."""
    try:
        if isinstance(definition, entry_type):
            return definition
    except TypeError:
        pass

    model = _record_value(definition, "model")
    if not isinstance(model, str) or not model:
        raise TypeError(f"model entry `{entry_id}` has no model")
    effort = _record_value(definition, "effort")
    kwargs: Dict[str, Any] = {
        "id": entry_id,
        "base_url": None,
        "model": model,
        "kind": harness_kind,
        "harness": harness,
    }
    if effort is not None:
        kwargs["effort"] = effort
    return entry_type(**kwargs)


def _command_fallback(
    backend: Dict[str, Any],
    notes: Optional[List[str]],
    reason: str,
) -> Optional[str]:
    """Use the record command and disclose why adapter rendering did not happen."""
    command = default_command_text_provider(backend)
    if command and notes is not None:
        bid = str(backend.get("id", "?"))
        note = (
            f"backend `{bid}` command adapter unavailable; using fallback command "
            f"from config ({reason})"
        )
        if note not in notes:
            notes.append(note)
    return command


def adapter_command_text_provider(
    backend: Dict[str, Any],
    *,
    model_entries: Optional[Mapping[str, Any]] = None,
    notes: Optional[List[str]] = None,
) -> Optional[str]:
    """Render a harness command by calling its shared-library adapter.

    The provider supplies sentinel paths and a bare launcher name so the
    resulting command is illustrative without carrying paths from the host
    that rendered the policy. A missing or incompatible shared library, an
    absent harness entry, or any adapter failure returns the backend record's
    command as an explicit compatibility fallback.
    """
    harness = str(backend.get("id") or "")
    if harness not in HARNESS_NAMES:
        return default_command_text_provider(backend)

    entries = model_entries or {}
    selected_id: Optional[str] = None
    selected_definition: Any = None
    for raw_id, definition in entries.items():
        entry_harness = _record_value(definition, "harness")
        if entry_harness != harness:
            continue
        entry_kind = _record_value(definition, "kind")
        if entry_kind is not None and entry_kind != "harness":
            continue
        selected_id = str(_record_value(definition, "id", raw_id))
        selected_definition = definition
        break

    try:
        import llm_scripting_kit as model_kit  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 -- optional/version-skewed library degrades
        return _command_fallback(
            backend, notes, f"llm_scripting_kit unavailable: {type(exc).__name__}"
        )

    resolve_adapter = getattr(model_kit, "resolve_harness_adapter", None)
    entry_type = getattr(model_kit, "EndpointEntry", None)
    harness_kind = getattr(model_kit, "HARNESS_KIND", None)
    if (
        not callable(resolve_adapter)
        or not callable(entry_type)
        or harness_kind != "harness"
    ):
        return _command_fallback(
            backend,
            notes,
            "llm_scripting_kit lacks the harness adapter feature",
        )
    if selected_id is None:
        return _command_fallback(
            backend, notes, f"no resolved {harness} harness entry"
        )

    try:
        entry = _adapter_entry(
            entry_type,
            harness_kind,
            selected_id,
            selected_definition,
            harness,
        )
        adapter = resolve_adapter(entry)
        adapter_type = type(adapter)
        # The adapter defaults may resolve a host executable to an absolute
        # path. Give it a stable launcher token for rendered text instead.
        adapter = adapter_type(argv_prefix=(harness,))
        build_argv = getattr(adapter, "build_argv", None)
        if not callable(build_argv):
            raise TypeError("resolved harness adapter has no build_argv method")
        kwargs: Dict[str, Any] = {"prompt": ""}
        placeholder_root = _placeholder_path("root")
        placeholder_result = _placeholder_path("result")
        placeholder_scratch = _placeholder_path("scratchpad")
        allowed_paths = {placeholder_root, placeholder_result}
        if harness == "codex":
            kwargs["output_file"] = placeholder_result
            # The scratchpad --add-dir is not decoration. Under
            # `-s workspace-write` the session scratchpad sits outside the
            # writable root, so a unit told to write there exits 0 having
            # written nothing -- a silent failure references/codex-dispatch.md
            # documents. Rendering the command WITHOUT it would hand the reader
            # a command the surrounding prose promises is complete.
            kwargs["add_dirs"] = [placeholder_scratch]
            allowed_paths.add(placeholder_scratch)
        argv = [str(part) for part in build_argv(entry, placeholder_root, **kwargs)]
        unexpected_paths = [
            part for part in argv if os.path.isabs(part) and part not in allowed_paths
        ]
        if unexpected_paths:
            raise ValueError(
                "adapter returned an unexpected absolute path in rendered argv"
            )
        return shlex.join(argv)
    except Exception as exc:  # noqa: BLE001 -- optional/version-skewed adapter degrades
        return _command_fallback(
            backend,
            notes,
            f"{type(exc).__name__}: {exc}",
        )


def _render_model_entries(
    entries: Mapping[str, Mapping[str, str]],
    harness: str,
    out: List[str],
) -> None:
    models = [entry for entry in entries.values() if entry.get("harness") == harness]
    if not models:
        return
    out.append("**Models.**")
    for entry in models:
        line = f"- `{entry['id']}`"
        if entry.get("effort"):
            line += f"; default effort `{entry['effort']}`"
        out.append(line)
    out.append("")


def render_backends(
    config: Dict[str, Any],
    detected: List[Tuple[Dict[str, Any], bool, str]],
    out: List[str],
    *,
    model_entries: Optional[Mapping[str, Mapping[str, str]]] = None,
    harness_status: Optional[Mapping[str, Tuple[bool, str]]] = None,
    command_text_provider: Callable[[Dict[str, Any]], Optional[str]] = adapter_command_text_provider,
) -> None:
    model_entries = model_entries or {}
    harness_status = harness_status or {}
    out.append("## Dispatch backends")
    out.append("")
    if not detected and not model_entries:
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
    rendered_ids = set()

    if not available:
        out.append("None of the configured backends detected on this machine.")
        out.append("")

    for backend, reason in available:
        bid = str(backend.get("id", "?"))
        rendered_ids.add(bid)
        title = f"### {backend.get('name', bid)} (`{bid}`)"
        out.append(title)
        out.append("")
        out.append(f"*Detected: {reason}.*")
        out.append("")
        # Rendered BEFORE the mechanics, because it decides whether the reader
        # should be composing a launch at all. A backend that is present but
        # must not be chosen by the decision tree has no other way to say so:
        # every other field here describes HOW to drive it, and a reader who
        # has reached the command has already decided to.
        if backend.get("selection"):
            out.append(f"**Selection.** {fold(backend['selection'])}")
            out.append("")
        if backend.get("prefer_for"):
            out.append(f"**Prefer for.** {fold(backend['prefer_for'])}")
            out.append("")
        caps = backend.get("capabilities") or {}
        if caps:
            for key in CAPABILITY_KEYS:
                if key in caps and caps[key] not in (None, ""):
                    out.append(f"- {key}: {fold(caps[key])}")
            out.append("")
        _render_model_entries(model_entries, bid, out)
        command_text = command_text_provider(backend)
        if command_text:
            out.append("```")
            out.append(fold(command_text))
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

    # A registry may name a supported harness for which the machine half has
    # no hand-authored record. Show an identity-only section for that harness,
    # using the same command detector as configured records. No dispatch
    # command is invented here, and routing resolution skips its models: a
    # backend a row prefers but nobody can drive is worse than an absent one.
    for harness, (ok, reason) in harness_status.items():
        if not ok or harness in rendered_ids:
            continue
        rendered_ids.add(harness)
        out.append(f"### {harness.title()} (`{harness}`)")
        out.append("")
        out.append(f"*Detected: {reason}.*")
        out.append("")
        _render_model_entries(model_entries, harness, out)
        out.append(
            "**Not dispatchable.** This harness appears in the model registry but "
            "has no `backends[]` record, so no launch mechanics exist here and "
            "routing rows do not resolve its models. Add a `backends[]` record "
            "(see references/configuration.md) to make it a dispatch target."
        )
        out.append("")


def render_capacity(config: Dict[str, Any], out: List[str]) -> None:
    capacity = config.get("capacity") or {}
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
                    "cheaper pool), and hold the highest-cost model for units that genuinely meet its bar."
                )
        out.append("")
        out.append(
            "These windows are ACCOUNT-wide, not per-model -- Claude Code exposes no "
            "per-model breakdown, so they cannot tell you a specific model is spent."
        )
    out.append("")


def discover_consult_seats(
    self_ref: Optional[str], project_root: Path
) -> Tuple[Optional[Any], str, Optional[str]]:
    """Discover seats when the optional library exposes the seats frontier.

    The seats section is enabling: an absent or stale optional library makes no
    claim, so those cases return without a renderable result. A failure after
    the frontier is available is different because the requested discovery did
    run and must be disclosed in the policy.
    """
    if not self_ref:
        return None, "self_not_provided", None

    try:
        import llm_scripting_kit as model_kit  # noqa: PLC0415
    except ImportError as exc:
        return None, "library_absent", str(exc)

    discover = getattr(model_kit, "discover_seats", None)
    if not callable(discover):
        return None, "library_too_old", None

    try:
        return discover(self_ref, project_root=str(project_root)), "available", None
    except Exception as exc:  # noqa: BLE001 -- discovery errors are rendered
        return None, "discovery_failed", f"{type(exc).__name__}: {exc}"


def render_consult_seats(
    result: Optional[Any], status: str, detail: Optional[str], out: List[str]
) -> None:
    """Render the requested consult seats result, including explicit failures."""
    if status in {"self_not_provided", "library_absent", "library_too_old"}:
        return

    out.append("## Consult seats")
    out.append("")
    if status == "discovery_failed":
        error_class = detail.split(":", 1)[0] if detail else "Exception"
        out.append(f"seats unavailable: {error_class}")
        out.append("")
        return
    if result is None:
        out.append("seats unavailable: Exception")
        out.append("")
        return

    seats = list(getattr(result, "seats", ()) or ())
    if seats:
        for seat in seats:
            harness = getattr(seat, "harness", None) or "?"
            out.append(
                f"{seat.relation} {seat.endpoint} ({seat.band}, {harness})"
            )
    else:
        out.append("none reachable -- decide and say so")
    self_seat = result.self
    out.append(f"self: {self_seat.endpoint} ({self_seat.band})")

    unclassified = [
        str(getattr(entry, "endpoint", entry))
        for entry in (getattr(result, "unclassified", ()) or ())
    ]
    if unclassified:
        out.append("unclassified: " + ", ".join(unclassified))
    out.append("")


def explain_consult_seats(
    result: Optional[Any], status: str, detail: Optional[str]
) -> None:
    """Print the seats diagnostic that is intentionally absent from guidance."""
    if status == "self_not_provided":
        print("seats  skipped   --self was not provided")
    elif status == "library_absent":
        print(
            "seats  skipped   llm_scripting_kit is absent; install with "
            "`claude plugin install llm-scripting-kit@plugins-kit`"
        )
    elif status == "library_too_old":
        print(
            "seats  skipped   llm_scripting_kit lacks discover_seats; owner "
            "version is 0.28.0; update with `claude plugin update "
            "llm-scripting-kit@plugins-kit`"
        )
    elif status == "discovery_failed":
        error_class = detail.split(":", 1)[0] if detail else "Exception"
        print(f"seats  error     {error_class}: {detail or 'no detail'}")
    elif status == "available" and result is not None:
        print(f"seats  available self={result.self.endpoint}")
        unknown = [
            str(getattr(seat, "endpoint", seat))
            for seat in (getattr(result, "probe_unknown", ()) or ())
        ]
        if unknown:
            print("seats  probe-unknown " + ", ".join(unknown))

# Schema-1 and retired decision keys. The decision half was reshaped in schema 3 and none of
# these is read any more, so an override written against schema 1 deep-merges
# cleanly and then contributes nothing. Silence there is the worst outcome: the
# user's policy is not in force and nothing says so.
LEGACY_SCHEMA_1_KEYS = (
    "tiers",
    "default_tier",
    "default_backend",
    "backend_selection",
    "implementation",
    "pool_economics",
    "ladders",
    "rungs",
    "backend",
)


def retired_capacity_keys(config: Dict[str, Any]) -> List[str]:
    """Return capacity paths whose old routing meaning is no longer supported."""
    capacity = config.get("capacity")
    if isinstance(capacity, dict) and "tier_overrides" in capacity:
        return ["capacity.tier_overrides"]
    return []


def legacy_schema_keys(config: Dict[str, Any]) -> List[str]:
    """Schema-1 keys present in the merged config, in declaration order."""
    return [k for k in LEGACY_SCHEMA_1_KEYS if k in config]


def render(
    config: Dict[str, Any],
    provenance: List[Tuple[str, Path, str]],
    self_ref: Optional[str] = None,
) -> str:
    out: List[str] = ["# Orchestration policy", ""]
    detected = detect_all(config)
    backend_names = {
        str(b.get("id")): str(b.get("name") or b.get("id")) for b, _, _ in detected
    }
    project_root = _project_root_from_provenance(provenance)
    model_entries, degradation_notes = discover_model_definitions(project_root)
    harness_status = detect_harnesses(config, detected, model_entries)
    # The notes are rendered into the policy, not merely into --explain. A
    # degraded render is INDISTINGUISHABLE from a healthy one -- rows simply
    # vanish -- while this document tells the reader that the models listed are
    # the only ones that exist. Silence therefore turns a missing or
    # version-skewed shared lib into a false claim about the machine
    # (plugins/CLAUDE.md, "Optional use of another plugin").
    command_text_provider = partial(
        adapter_command_text_provider,
        model_entries=model_entries,
        notes=degradation_notes,
    )
    rendered_commands = _rendered_backend_command_texts(detected, command_text_provider)
    routable_backend_ids = _routable_backend_ids(detected, rendered_commands)
    routes, _routing_notes = resolve_routing_models(
        config,
        model_entries,
        harness_status,
        routable_backend_ids,
    )
    render_decision_tree(config, routable_backend_ids, backend_names, out, routes)
    seat_result, seat_status, seat_detail = discover_consult_seats(
        self_ref, project_root
    )
    render_consult_seats(seat_result, seat_status, seat_detail, out)
    render_backends(
        config,
        detected,
        out,
        model_entries=model_entries,
        harness_status=harness_status,
        command_text_provider=lambda backend: rendered_commands.get(_backend_id(backend)),
    )
    render_capacity(config, out)
    out.append("---")
    out.append("")
    if degradation_notes:
        out.append("**Degraded render.** Some rows could not be resolved, so this")
        out.append("policy is INCOMPLETE -- treat a missing model or backend as unknown")
        out.append("rather than as absent from this machine:")
        out.append("")
        for note in degradation_notes:
            out.append(f"- {note}")
        out.append("")
    applied = [layer for layer, _, status in provenance if status_is_applied(status)]
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
            if status_is_applied(status) and layer != "shipped"
        ]
        out.append("")
        out.append(
            "**Stale override -- NOT IN FORCE.** A layer sets schema-1 key(s) "
            + ", ".join(f"`{k}`" for k in stale)
            + ", which schema 3 no longer reads; those settings contribute nothing to "
            "the policy above. Port the decision to `routing` and keep any surviving "
            "vocabulary or procedure in the corresponding schema-3 sections -- see "
            "references/configuration.md."
            + (" Layer(s): " + "; ".join(overrides) + "." if overrides else "")
        )
    retired_capacity = retired_capacity_keys(config)
    if retired_capacity:
        out.append("")
        out.append(
            "**Stale override -- NOT IN FORCE.** "
            + ", ".join(f"`{key}`" for key in retired_capacity)
            + " is retired in schema 3 and does not affect routing or capacity."
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
        help="Print the four layer paths and exit",
    )
    parser.add_argument(
        "--self",
        dest="self_ref",
        help="Registry endpoint alias or model id for this agent",
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
        model_entries, model_notes = discover_model_definitions(project_root)
        harness_status = detect_harnesses(config, detected, model_entries)
        command_notes: List[str] = []
        command_text_provider = partial(
            adapter_command_text_provider,
            model_entries=model_entries,
            notes=command_notes,
        )
        rendered_commands = _rendered_backend_command_texts(detected, command_text_provider)
        routes, routing_notes = resolve_routing_models(
            config,
            model_entries,
            harness_status,
            _routable_backend_ids(detected, rendered_commands),
        )
        for harness, (ok, reason) in harness_status.items():
            print(f"harness  {'available' if ok else 'MISSING':9} {harness}: {reason}")
        for note in model_notes:
            print(f"model    note      {note}")
        for note in command_notes:
            print(f"command  note      {note}")
        for note in routing_notes:
            print(f"routing  note      {note}")
        seat_result, seat_status, seat_detail = discover_consult_seats(
            args.self_ref, project_root
        )
        explain_consult_seats(seat_result, seat_status, seat_detail)
        for route in routes:
            targets = ", ".join(model["target"] for model in route["models"])
            shape = "+".join(route["shape"]) or "default"
            print(f"routing  row       {route['number']}: {shape} -> {targets}")
        print()
        print(yaml.safe_dump(config, sort_keys=False, allow_unicode=False, width=100))
        return 0

    text = render(config, provenance, self_ref=args.self_ref)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
