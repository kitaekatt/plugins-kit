"""standards_resolve -- resolve the layered skills-kit standards configuration.

skills-kit ships all its opinions on by default; a user tunes the OPTIONAL layer
(never the architectural spine, never the inoffensive integrity checks) through
version-controllable config that mirrors bootstrap.json layering:

    0  shipped_dir (the plugin's own standards/, optional -- unused until M5)
    1  <user_dir>/skills-kit/                    config.yaml + *-standards.md
    2  <user_dir>/skills-kit/config.local.yaml   per-machine config overlay
    3  <project>/.claude/skills-kit/             config.yaml + *-standards.md
    4  <project>/.claude/skills-kit/config.local.yaml  personal config overlay

<user_dir> = $CLAUDE_CONFIG_DIR else ~/.claude (the harness config dir). Config
merges later-wins (deep-merge); standards files UNION across layers (a later
layer appends, never replaces).

Self-contained by design: stdlib + pyyaml only, no bootstrap_lib import (that
would take a shared_lib_imports manifest change and break audit.py's graceful
bare-python degradation). When pyyaml is unavailable resolution degrades to
empty defaults plus a loud note, exactly like audit.py's contract-staged state
-- it never crashes. Malformed config, an un-tunable rule id, an unknown
threshold, an unknown adapter or adapter setting, or an invalid standards file
are LOUD (StandardsConfigError), never a silent {}.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import rule_catalog
from .audit import THRESHOLDS
from .document_walker import collect_yaml_units
from .schema_engine import validate
from .schema_registry import SCHEMAS_BY_ROOT

try:
    import yaml as _yaml
    HAVE_YAML = True
except ImportError:  # pragma: no cover - exercised only on a bare interpreter
    _yaml = None
    HAVE_YAML = False


#: The four file-type primitives a standards_set's `applies_to:` may name
#: (schemas/standards.py's owner-doc note; configuring-standards.md's
#: "Additive standards files" table). Defined once here, next to the
#: schema's note, so both stay in sync.
APPLIES_TO_PRIMITIVES = ("skill_md", "claude_md", "reference_doc", "plain_md")


class StandardsConfigError(Exception):
    """A resolved standards layer is malformed or names something un-tunable.

    Raised loudly rather than degrading to a silent empty config: a broken
    config file, an attempt to disable an architectural/inoffensive/unknown
    rule, an unknown threshold key, a bad value, or an invalid *-standards.md.
    """


#: The one adapter id this configuration surface knows, and the keys it takes.
#: An adapter is task-specific context admitted for the model-task pairs it was
#: MEASURED on. The shipped default admits nothing, so an unconfigured user gets
#: the behaviour of not having the adapter at all.
ADAPTER_MD_AUDIT_EVIDENCE_PACK = "md-audit-evidence-pack"
ADAPTER_KEYS: dict[str, set[str]] = {
    ADAPTER_MD_AUDIT_EVIDENCE_PACK: {"admitted_endpoints"},
}


@dataclass
class StandardsFile:
    """One authored *-standards.md, parsed and schema-validated."""

    path: Path
    applies_to: str
    criteria: list


@dataclass
class ResolvedStandards:
    """The merged view of every standards layer for one resolution.

    - disabled_rules: optional rule ids the config switched off.
    - thresholds: threshold OVERRIDES only (names present in the config); an
      absent name keeps audit.py's default.
    - standards_by_primitive: applies_to value -> the StandardsFiles governing
      it, unioned across layers in layer order.
    - adapters: adapter id -> that adapter's settings, for the ids in
      ADAPTER_KEYS. An absent adapter, or an absent key inside one, keeps the
      shipped default -- which for every adapter setting is EMPTY.
    - notes: loud-but-non-fatal diagnostics (e.g. pyyaml unavailable).
    """

    disabled_rules: set[str] = field(default_factory=set)
    thresholds: dict[str, int] = field(default_factory=dict)
    standards_by_primitive: dict[str, list[StandardsFile]] = field(default_factory=dict)
    adapters: dict[str, dict] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def adapter_admitted_endpoints(self, adapter_id: str) -> frozenset[str]:
        """The endpoint ids admitted for one adapter; empty when unconfigured."""
        settings = self.adapters.get(adapter_id) or {}
        return frozenset(settings.get("admitted_endpoints", ()))


def _config_dir() -> Path:
    """The harness config dir: $CLAUDE_CONFIG_DIR if set, else ~/.claude.

    A small local reimplementation of md-domain/scripts/discover_project_doc.py's
    _config_dir -- copied deliberately rather than imported, so the lib takes no
    dependency on a skill's scripts dir.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base updated by overlay, recursing into nested dicts (later-wins).

    Nested mappings merge key-by-key; every other shape (scalar, list, or a
    dict-vs-nondict mismatch) replaces wholesale.
    """
    result = dict(base)
    for key, val in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_config_file(path: Path) -> dict:
    """Load one config.yaml / config.local.yaml layer.

    Absent -> {} (skip silently). Empty -> {}. Malformed YAML or a non-mapping
    root -> StandardsConfigError naming the path (never a silent {}).
    """
    if not path.exists():
        return {}
    try:
        data = _yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StandardsConfigError(f"{path}: malformed YAML -- {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise StandardsConfigError(
            f"{path}: config root must be a mapping, got {type(data).__name__}"
        )
    return data


def _is_off(val) -> bool:
    """True when a rules: value means 'disable'. YAML `off` parses to False; a
    quoted "off"/"false" stays a string. Anything else is not a disable value."""
    if val is False:
        return True
    if isinstance(val, str) and val.strip().lower() in ("off", "false"):
        return True
    return False


def _validate_adapters(merged: dict) -> dict[str, dict]:
    """Validate the merged config's `adapters:` block and return its settings.

    An unknown adapter id, an unknown key inside a known adapter, or a value of
    the wrong shape raises rather than being ignored: a typo'd endpoint list
    would otherwise silently admit nothing, which looks exactly like the
    (correct, empty) default and hides the mistake.
    """
    raw = merged.get("adapters", {})
    if raw and not isinstance(raw, dict):
        raise StandardsConfigError(
            f"'adapters:' must be a mapping of adapter-id -> settings, got "
            f"{type(raw).__name__}"
        )
    adapters: dict[str, dict] = {}
    for adapter_id, settings in (raw or {}).items():
        if adapter_id not in ADAPTER_KEYS:
            raise StandardsConfigError(
                f"adapter '{adapter_id}' is not a known adapter; valid adapter "
                f"ids: {sorted(ADAPTER_KEYS)}"
            )
        if not isinstance(settings, dict):
            raise StandardsConfigError(
                f"adapter '{adapter_id}' must be a mapping of setting -> value, "
                f"got {type(settings).__name__}"
            )
        valid = ADAPTER_KEYS[adapter_id]
        extracted: dict = {}
        for key, val in settings.items():
            if key not in valid:
                raise StandardsConfigError(
                    f"adapter '{adapter_id}' has no setting '{key}'; valid "
                    f"settings: {sorted(valid)}"
                )
            if key == "admitted_endpoints":
                if not isinstance(val, list) or not all(
                    isinstance(name, str) and name.strip() for name in val
                ):
                    raise StandardsConfigError(
                        f"adapter '{adapter_id}': admitted_endpoints must be a "
                        f"list of non-empty endpoint id strings; got {val!r}"
                    )
                extracted[key] = [name.strip() for name in val]
            else:  # pragma: no cover - unreachable while every key is handled
                extracted[key] = val
        adapters[adapter_id] = extracted
    return adapters


def _validate_and_extract(merged: dict) -> tuple[set[str], dict[str, int]]:
    """Validate the merged config and extract disabled rules + threshold overrides.

    A rules: id that is not an optional rule raises, naming the id and its bucket
    (architectural/inoffensive/unknown). A thresholds: key not in audit's
    THRESHOLDS raises, naming the valid keys. Rule values accept only off/false;
    threshold values must be positive ints.
    """
    disabled: set[str] = set()
    rules = merged.get("rules", {})
    if rules and not isinstance(rules, dict):
        raise StandardsConfigError(
            f"'rules:' must be a mapping of rule-id -> off, got {type(rules).__name__}"
        )
    optional = set(rule_catalog.optional_rule_ids())
    for rid, val in (rules or {}).items():
        if rid not in optional:
            bucket = rule_catalog.BUCKETS.get(rid, "unknown")
            raise StandardsConfigError(
                f"rule '{rid}' is {bucket} and cannot be configured; only optional "
                f"rules are configurable (see rule_catalog.optional_rule_ids())"
            )
        if not _is_off(val):
            raise StandardsConfigError(
                f"rule '{rid}' accepts only 'off' (or false) to disable it; got {val!r}"
            )
        disabled.add(rid)

    thresholds: dict[str, int] = {}
    raw_th = merged.get("thresholds", {})
    if raw_th and not isinstance(raw_th, dict):
        raise StandardsConfigError(
            f"'thresholds:' must be a mapping of name -> int, got {type(raw_th).__name__}"
        )
    for name, val in (raw_th or {}).items():
        if name not in THRESHOLDS:
            raise StandardsConfigError(
                f"threshold '{name}' is not a valid threshold; valid keys: {sorted(THRESHOLDS)}"
            )
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise StandardsConfigError(
                f"threshold '{name}' must be a positive integer; got {val!r}"
            )
        thresholds[name] = val
    return disabled, thresholds


def _parse_standards_file(path: Path) -> StandardsFile:
    """Parse + schema-validate one *-standards.md; raise loudly on any defect."""
    text = path.read_text(encoding="utf-8")
    units, _, parse_error = collect_yaml_units(text)
    if parse_error is not None:
        _root, msg = parse_error
        raise StandardsConfigError(
            f"{path}: fenced YAML block failed to parse -- {msg}"
        )
    block: dict | None = None
    for root, data in units:
        if root == "standards_set":
            block = data
            break
    if block is None:
        raise StandardsConfigError(
            f"{path}: no fenced `standards_set:` YAML block found "
            "(a *-standards.md file must carry exactly one)"
        )
    fails, _checked = validate(block, SCHEMAS_BY_ROOT["standards_set"])
    if fails:
        detail = "; ".join(f"{p}: {m}" for p, m in fails)
        raise StandardsConfigError(
            f"{path}: standards_set block failed schema validation -- {detail}"
        )
    inner = block["standards_set"]
    applies_to = inner["applies_to"]
    if applies_to not in APPLIES_TO_PRIMITIVES:
        raise StandardsConfigError(
            f"{path}: applies_to '{applies_to}' is not one of the four "
            f"file-type primitives; valid values: {list(APPLIES_TO_PRIMITIVES)}"
        )
    return StandardsFile(
        path=path,
        applies_to=applies_to,
        criteria=inner.get("criteria", []),
    )


def resolve(project_root: Path | None, *, shipped_dir: Path | None = None) -> ResolvedStandards:
    """Resolve every standards layer into one ResolvedStandards.

    Layer order (lowest -> highest): shipped_dir (optional), <user_dir>/skills-kit
    (config.yaml then config.local.yaml), <project_root>/.claude/skills-kit
    (config.yaml then config.local.yaml). Config merges later-wins; standards
    files union across layers. Degrades to empty defaults + a note when pyyaml
    is unavailable.
    """
    notes: list[str] = []
    if not HAVE_YAML:
        notes.append(
            "pyyaml unavailable; standards resolution degraded to defaults "
            "(no config or standards files applied)"
        )
        return ResolvedStandards(notes=notes)

    user_layer = _config_dir() / "skills-kit"
    proj_layer = (project_root / ".claude" / "skills-kit") if project_root is not None else None

    # -- config layers (files), merged later-wins -----------------------------
    config_files: list[Path] = []
    if shipped_dir is not None:
        config_files.append(shipped_dir / "config.yaml")
    config_files.append(user_layer / "config.yaml")
    config_files.append(user_layer / "config.local.yaml")
    if proj_layer is not None:
        config_files.append(proj_layer / "config.yaml")
        config_files.append(proj_layer / "config.local.yaml")

    merged: dict = {}
    for cf in config_files:
        merged = _deep_merge(merged, _load_config_file(cf))

    disabled_rules, thresholds = _validate_and_extract(merged)
    adapters = _validate_adapters(merged)

    # -- standards files (dirs only; config.local.yaml carries no md) ---------
    standards_dirs: list[Path] = []
    if shipped_dir is not None:
        standards_dirs.append(shipped_dir)
    standards_dirs.append(user_layer)
    if proj_layer is not None:
        standards_dirs.append(proj_layer)

    standards_by_primitive: dict[str, list[StandardsFile]] = {}
    for d in standards_dirs:
        if not d.is_dir():
            continue
        for md_path in sorted(d.glob("*-standards.md")):
            sf = _parse_standards_file(md_path)
            standards_by_primitive.setdefault(sf.applies_to, []).append(sf)

    return ResolvedStandards(
        disabled_rules=disabled_rules,
        thresholds=thresholds,
        standards_by_primitive=standards_by_primitive,
        adapters=adapters,
        notes=notes,
    )
