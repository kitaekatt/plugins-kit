"""Resolve the shared code-review profile configuration.

The review skills own their explanatory prose. This module owns the executable
profile data and resolves sparse user and project overrides over the shipped
defaults:

    shipped defaults
      -> ~/.claude/config/review_profiles.yaml
      -> <project_root>/.claude/review_profiles.yaml

Mappings merge by key. Profile and reviewer records merge by their identity
field, while ordinary lists replace the lower-precedence value. The result is
validated before it is returned so callers can resolve the profile before any
review fan-out starts.

Every model a layer names is an ENTRY stating both an id and an effort:

    model:
    - {id: sol,  effort: high}
    - {id: opus, effort: high}

A reviewer's ``model`` is an ordered list of entries; a validator reason is a
list holding exactly one. A layer that states a reviewer or a validator reason
must state it completely, in that layer. Each gap is a FINDING, and
``resolve_config`` raises one error listing every finding across every layer.
``main --check`` prints the findings without rendering a table.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

from bootstrap_lib import model_declaration
from bootstrap_lib.code_review import lane_prompts


CONFIG_NAME = "review_profiles.yaml"
_SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = _SCRIPT_DIR / "defaults" / CONFIG_NAME

PathLike = str | Path
Provenance = tuple[str, Path, str]

TOP_LEVEL_FIELDS = frozenset({"profiles"})
PROFILE_FIELDS = frozenset(
    {"id", "selection", "reviewers", "validator_models", "disabled"}
)
SELECTION_FIELDS = frozenset({"data_only_extensions"})
REVIEWER_FIELDS = frozenset({"name", "model", "disabled"})
ENTRY_FIELDS = frozenset({"id", "effort"})
REQUIRED_PROFILE_FIELDS = frozenset({"selection", "reviewers", "validator_models"})

# A field a reviewer record used to carry at lane level. It is reported as a
# completeness finding with a targeted message rather than as an unknown field,
# so a layer written against the old shape is told where the value now goes.
_REMOVED_LANE_EFFORT = "effort"

# --------------------------------------------------------------------------
# effort: the reasoning budget one model entry runs under
# --------------------------------------------------------------------------
# Every model entry states its effort. It is a fixed MENU rather than the
# free-form string an id is: an id may name an endpoint this library knows
# nothing about, but effort is a vocabulary the dispatchers define, so a typo
# ("lo", "minimal") would otherwise resolve to an agent or a flag value that
# does not exist and fail at dispatch, far from the configuration line that
# caused it.
#
# A Claude entry dispatches to a per-level reviewer AGENT, because the Agent
# tool has no effort parameter -- effort is set in an agent definition's
# frontmatter. git-kit and p4-kit each ship the agents as
# `<kit>:review-lane-<level>`. Any other entry passes its effort to the lane
# tool. The generator and its tests import this constant by name.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(ValueError):
    """A review-profile layer is unreadable, malformed, or invalid."""


# The longer name is useful to callers that want to distinguish this module's
# errors from the bootstrap engine's other configuration errors. Keep the
# short alias as well because it matches bootstrap_lib.config_resolve.
ReviewProfilesConfigError = ConfigError


class IncompleteConfigError(ConfigError):
    """Every layer is well formed, but at least one states an incomplete entry.

    ``findings`` holds one line per gap, in layer order, so a caller can print
    all of them rather than the first.
    """

    def __init__(self, findings: Sequence[str]) -> None:
        self.findings = list(findings)
        super().__init__(
            f"{len(self.findings)} review-profile finding(s); every model entry "
            "states an id and an effort:\n" + "\n".join(self.findings)
        )


def _require_yaml() -> Any:
    """Import PyYAML at the boundary where a YAML file is read or rendered."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment dependency
        raise ConfigError(
            "PyYAML is required to resolve review profiles but is not importable. "
            "Install the declared bootstrap dependency before running the resolver."
        ) from exc
    return yaml


def _fail(source: Path | str, location: str, message: str) -> NoReturn:
    """Raise one consistently actionable configuration error."""
    raise ConfigError(f"{source}: {location}: {message}")


def _home_path(home: PathLike | None) -> Path:
    """Return the configured home, or the process user's home."""
    return Path.home() if home is None else Path(home).expanduser()


def user_config_path(home: PathLike | None = None) -> Path:
    """Return the portable user override path."""
    return _home_path(home) / ".claude" / "config" / CONFIG_NAME


def project_config_path(project_root: PathLike) -> Path:
    """Return the project override path."""
    return Path(project_root).expanduser() / ".claude" / CONFIG_NAME


def layer_paths(
    project_root: PathLike,
    *,
    home: PathLike | None = None,
) -> list[tuple[str, Path]]:
    """Return shipped, user, and project paths in increasing precedence."""
    return [
        ("shipped", DEFAULTS_PATH),
        ("user", user_config_path(home)),
        ("project", project_config_path(project_root)),
    ]


def load_layer(path: PathLike) -> dict[str, Any] | None:
    """Load one YAML layer, returning ``None`` when the path is absent."""
    source = Path(path).expanduser()
    if not source.exists():
        return None

    yaml = _require_yaml()
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read review-profile config layer {source}: {exc}") from exc
    except UnicodeError as exc:
        raise ConfigError(
            f"review-profile config layer {source} is not valid UTF-8: {exc}"
        ) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"malformed YAML in review-profile config layer {source}: {exc}") from exc

    if data is None:
        return {}
    if not isinstance(data, dict):
        _fail(source, "top level", f"must be a mapping, got {type(data).__name__}")
    return data


def _validate_known_fields(
    value: Mapping[Any, Any],
    allowed: frozenset[str],
    source: Path | str,
    location: str,
) -> None:
    """Reject typos and prose fields outside the executable schema."""
    unknown = [key for key in value if not isinstance(key, str) or key not in allowed]
    if unknown:
        names = ", ".join(repr(key) for key in unknown)
        known = ", ".join(sorted(allowed))
        _fail(source, location, f"unknown field(s): {names}; known fields: {known}")


def _validate_nonempty_string(value: Any, source: Path | str, location: str) -> None:
    """Require a string containing at least one non-whitespace character."""
    if not isinstance(value, str) or not value.strip():
        _fail(source, location, "must be a non-empty string")


def _validate_disabled(value: Mapping[str, Any], source: Path | str, location: str) -> bool:
    """Validate and return a record's optional disabled flag."""
    if "disabled" not in value:
        return False
    if not isinstance(value["disabled"], bool):
        _fail(source, f"{location}.disabled", "must be a boolean")
    return value["disabled"]


def _validate_effort(value: Any, source: Path | str, location: str) -> None:
    """Validate an entry's effort against the fixed level menu.

    Unlike an id this is a closed vocabulary (see ``EFFORT_LEVELS``), so an
    unknown level is rejected here rather than becoming a dispatch to an agent
    or a flag value that does not exist.
    """
    if not isinstance(value, str) or not value.strip():
        _fail(source, location, "must be a non-empty string")
    if value not in EFFORT_LEVELS:
        levels = ", ".join(repr(level) for level in EFFORT_LEVELS)
        _fail(source, location, f"unknown effort {value!r}; known levels: {levels}")


def _validate_entries(
    value: Any,
    source: Path | str,
    location: str,
    *,
    exactly_one: bool,
) -> None:
    """Validate the STRUCTURE of a model entry list.

    The list is ``[{id: <model>, effort: <level>}, ...]``. A bare string, or a
    string inside the list, is the retired shape; it passes structural
    validation so ``completeness_findings`` can report it as a finding next to
    every other gap, instead of the first one aborting the walk. Everything
    else that is not that shape is an error here:

    * a bare mapping. There is no single-entry shorthand, because a higher
      layer's mapping would merge key by key into a lower layer's and silently
      inherit its fields, where a list replaces wholesale.
    * an entry field other than ``id`` and ``effort``, an entry without an
      ``id``, or an ``effort`` outside ``EFFORT_LEVELS``.
    * a blank id, an empty list, or one id twice -- checked by
      ``model_declaration.parse`` over the ordered ids, so every plugin
      reading a declaration rejects the same shapes.
    * for a validator, any count other than one entry.
    """
    if isinstance(value, str):
        ids: list[Any] = [value]
    elif isinstance(value, dict):
        _fail(
            source,
            location,
            "must be a list of {id, effort} entries, got a mapping; write "
            "`[{id: <model>, effort: <level>}]` -- a bare mapping would merge "
            "key by key into the layer below instead of replacing it",
        )
    elif isinstance(value, list):
        ids = []
        for index, entry in enumerate(value):
            entry_location = f"{location}[{index}]"
            if isinstance(entry, str):
                ids.append(entry)
                continue
            if not isinstance(entry, dict):
                _fail(
                    source,
                    entry_location,
                    "must be an {id: <model>, effort: <level>} entry, got "
                    f"{type(entry).__name__}",
                )
            _validate_known_fields(entry, ENTRY_FIELDS, source, entry_location)
            if "id" not in entry:
                _fail(source, entry_location, "required field missing: id")
            if "effort" in entry:
                _validate_effort(entry["effort"], source, f"{entry_location}.effort")
            ids.append(entry["id"])
    else:
        _fail(
            source,
            location,
            "must be a list of {id: <model>, effort: <level>} entries, got "
            f"{type(value).__name__}",
        )

    try:
        parsed = model_declaration.parse(ids if isinstance(value, list) else ids[0])
    except model_declaration.DeclarationError as exc:
        where = location if exc.index is None else f"{location}[{exc.index}]"
        _fail(source, where, str(exc))
    if exactly_one and len(parsed) != 1:
        _fail(
            source,
            location,
            f"must hold exactly one entry, got {parsed!r}: a validator has no "
            "failover chain",
        )


def _no_effort(model_id: str) -> str:
    """Return the finding text for an entry written in the retired shape."""
    return (
        f"entry {model_id!r} states no effort; write "
        f"[{{id: {model_id}, effort: <level>}}]"
    )


def _entry_findings(value: Any, source: str, location: str) -> list[str]:
    """Return one finding per entry of ``value`` that states no effort.

    ``value`` must already have passed ``_validate_entries``; any other shape
    is an error, not a finding.
    """
    if isinstance(value, str):
        model_id = value.strip()
        return [f"{source}: {location} ({model_id}): {_no_effort(model_id)}"]
    if not isinstance(value, list):
        _fail(source, location, "is not a validated entry list; validate the layer first")
    findings: list[str] = []
    for index, entry in enumerate(value):
        where = f"{location}[{index}]"
        if isinstance(entry, str):
            model_id = entry.strip()
            findings.append(f"{source}: {where} ({model_id}): {_no_effort(model_id)}")
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            if "effort" not in entry:
                findings.append(f"{source}: {where} ({entry['id'].strip()}): missing effort")
        else:
            _fail(source, where, "is not a validated entry; validate the layer first")
    return findings


def completeness_findings(layer_data: Mapping[str, Any], source: PathLike) -> list[str]:
    """Return every completeness finding in one structurally valid layer.

    ``layer_data`` is a RAW layer as loaded, after ``_validate_layer`` (or the
    resolved-table validation) has accepted its structure. ``source`` labels
    each finding, conventionally ``"<layer> <path>"``. The walk collects ALL
    findings rather than stopping at the first:

    * every non-disabled reviewer record the layer states must state a
      ``model`` list in this layer, and every entry in it must state an effort;
    * every validator reason the layer states must state its one entry's
      effort;
    * a lane-level ``effort`` is the removed shape and is reported with the
      form that replaced it.

    Exempt: a reviewer record stating ``disabled: true`` needs no model, and
    nothing under a profile stating ``disabled: true`` is walked.
    """
    label = str(source)
    findings: list[str] = []
    for profile in layer_data.get("profiles", []):
        if profile.get("disabled") is True:
            continue
        profile_location = f"profiles[{profile['id']}]"
        for reviewer in profile.get("reviewers", []):
            where = f"{profile_location}.reviewers[{reviewer['name']}]"
            if _REMOVED_LANE_EFFORT in reviewer:
                findings.append(
                    f"{label}: {where}.effort: `effort` on a lane was removed: state "
                    "it on each model entry, e.g. `model: [{id: <model>, effort: <level>}]`"
                )
            if "model" in reviewer:
                findings.extend(_entry_findings(reviewer["model"], label, f"{where}.model"))
            elif reviewer.get("disabled") is not True:
                findings.append(
                    f"{label}: {where}.model: missing: a reviewer record a layer states "
                    "must state its whole model list in that layer, e.g. "
                    "`model: [{id: <model>, effort: <level>}]`"
                )
        for reason, value in (profile.get("validator_models") or {}).items():
            findings.extend(
                _entry_findings(value, label, f"{profile_location}.validator_models.{reason}")
            )
    return findings


def _records_by_name(records: Any, identity: str) -> dict[str, Mapping[str, Any]]:
    """Index already-validated records by their identity field."""
    if not isinstance(records, list):
        return {}
    return {
        str(record[identity]): record
        for record in records
        if isinstance(record, dict) and identity in record
    }


def _validate_selection(value: Any, source: Path | str, location: str) -> None:
    """Validate the executable selection mapping."""
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    _validate_known_fields(value, SELECTION_FIELDS, source, location)
    if "data_only_extensions" not in value:
        return
    extensions = value["data_only_extensions"]
    if not isinstance(extensions, list):
        _fail(source, f"{location}.data_only_extensions", "must be a list")
    for index, extension in enumerate(extensions):
        _validate_nonempty_string(
            extension,
            source,
            f"{location}.data_only_extensions[{index}]",
        )


def _validate_validator_models(value: Any, source: Path | str, location: str) -> None:
    """Validate reason-to-entry bindings.

    Reason keys are intentionally extensible. ``bug`` and ``claude_md`` are
    the shipped reasons; a new reason is an addressable mapping record and is
    appended by the normal mapping merge.

    Each value is a list holding exactly ONE ``{id, effort}`` entry. A
    validator is never endpoint-eligible and has no failover chain, so a
    second entry would be a preference nothing honours; it is refused rather
    than silently ignored.
    """
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    for reason, entries in value.items():
        if not isinstance(reason, str) or not reason.strip():
            _fail(source, f"{location} key {reason!r}", "must be a non-empty string")
        _validate_entries(entries, source, f"{location}.{reason}", exactly_one=True)


def _validate_reviewer(value: Any, source: Path | str, location: str) -> None:
    """Validate one reviewer record's structure.

    Whether the record states a model at all is a completeness question,
    answered by ``completeness_findings``.
    """
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    # Checked before the generic unknown-field message so a layer written
    # against the retired boolean is told what replaced it.
    if "peer_when_available" in value:
        _fail(
            source,
            f"{location}.peer_when_available",
            "was removed: state the preference as an ordered model priority "
            "list instead, e.g. `model: [<name>, <name>]`",
        )
    # A lane-level `effort` is the removed shape. It is left to
    # completeness_findings, which names the per-entry form that replaced it.
    _validate_known_fields(
        {key: item for key, item in value.items() if key != _REMOVED_LANE_EFFORT},
        REVIEWER_FIELDS,
        source,
        location,
    )
    if "name" not in value:
        _fail(source, location, "required field missing: name")
    _validate_nonempty_string(value["name"], source, f"{location}.name")
    _validate_disabled(value, source, location)
    if "model" in value:
        _validate_entries(value["model"], source, f"{location}.model", exactly_one=False)


def _validate_reviewers(value: Any, source: Path | str, location: str) -> None:
    """Validate a profile's reviewer record list and its identities."""
    if not isinstance(value, list):
        _fail(source, location, f"must be a list, got {type(value).__name__}")
    seen: set[str] = set()
    for index, reviewer in enumerate(value):
        reviewer_location = f"{location}[{index}]"
        if not isinstance(reviewer, dict):
            _fail(
                source,
                reviewer_location,
                f"must be a mapping, got {type(reviewer).__name__}",
            )
        name = reviewer.get("name")
        if isinstance(name, str) and name.strip():
            if name in seen:
                _fail(source, location, f"duplicate reviewer name: {name!r}")
            seen.add(name)
        _validate_reviewer(reviewer, source, reviewer_location)


def _validate_profile(
    value: Any,
    source: Path | str,
    location: str,
    *,
    existing: Mapping[str, Any] | None,
    complete: bool,
) -> None:
    """Validate one profile, with sparse fields allowed for known patches."""
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    _validate_known_fields(value, PROFILE_FIELDS, source, location)
    if "id" not in value:
        _fail(source, location, "required field missing: id")
    _validate_nonempty_string(value["id"], source, f"{location}.id")
    disabled = _validate_disabled(value, source, location)

    required = REQUIRED_PROFILE_FIELDS if complete or existing is None else frozenset()
    if not disabled:
        missing = sorted(field for field in required if field not in value)
        if missing:
            _fail(
                source,
                location,
                "required field(s) missing: " + ", ".join(missing),
            )

    if "selection" in value:
        _validate_selection(value["selection"], source, f"{location}.selection")
    if "reviewers" in value:
        _validate_reviewers(value["reviewers"], source, f"{location}.reviewers")
    if "validator_models" in value:
        _validate_validator_models(
            value["validator_models"],
            source,
            f"{location}.validator_models",
        )


def _validate_layer(
    value: Mapping[str, Any],
    source: Path | str,
    *,
    base: Mapping[str, Any],
) -> None:
    """Validate a sparse layer's structure before it participates in the merge."""
    _validate_known_fields(value, TOP_LEVEL_FIELDS, source, "top level")
    if "profiles" not in value:
        return
    profiles = value["profiles"]
    if not isinstance(profiles, list):
        _fail(source, "profiles", f"must be a list, got {type(profiles).__name__}")

    existing_profiles = _records_by_name(base.get("profiles"), "id")
    seen: set[str] = set()
    for index, profile in enumerate(profiles):
        location = f"profiles[{index}]"
        if not isinstance(profile, dict):
            _fail(source, location, f"must be a mapping, got {type(profile).__name__}")
        profile_id = profile.get("id")
        if isinstance(profile_id, str) and profile_id.strip():
            if profile_id in seen:
                _fail(source, "profiles", f"duplicate profile id: {profile_id!r}")
            seen.add(profile_id)
            existing = existing_profiles.get(profile_id)
        else:
            existing = None
        _validate_profile(
            profile,
            source,
            location,
            existing=existing,
            complete=False,
        )


def _validate_resolved(
    value: Mapping[str, Any],
    source: Path | str,
    *,
    require_runtime_coverage: bool = False,
) -> None:
    """Validate the merged table, including the completeness of active records."""
    _validate_known_fields(value, TOP_LEVEL_FIELDS, source, "top level")
    if "profiles" not in value:
        _fail(source, "top level", "required field missing: profiles")
    profiles = value["profiles"]
    if not isinstance(profiles, list):
        _fail(source, "profiles", f"must be a list, got {type(profiles).__name__}")
    if require_runtime_coverage and not profiles:
        _fail(source, "profiles", "must contain at least one active review profile")

    seen: set[str] = set()
    for index, profile in enumerate(profiles):
        location = f"profiles[{index}]"
        if not isinstance(profile, dict):
            _fail(source, location, f"must be a mapping, got {type(profile).__name__}")
        profile_id = profile.get("id")
        if isinstance(profile_id, str) and profile_id.strip():
            if profile_id in seen:
                _fail(source, "profiles", f"duplicate profile id: {profile_id!r}")
            seen.add(profile_id)
        _validate_profile(
            profile,
            source,
            location,
            existing=None,
            complete=True,
        )

    findings = completeness_findings(value, source)
    if findings:
        raise IncompleteConfigError(findings)

    if not require_runtime_coverage:
        return
    supported = sorted(lane_prompts.KNOWN_LANES - {"validator"})
    for index, profile in enumerate(profiles):
        location = f"profiles[{index}]"
        reviewers = profile["reviewers"]
        if not reviewers:
            _fail(
                source,
                f"{location}.reviewers",
                f"profile {profile['id']!r} must contain at least one active reviewer",
            )
        for reviewer_index, reviewer in enumerate(reviewers):
            name = reviewer["name"]
            if name not in lane_prompts.KNOWN_LANES - {"validator"}:
                _fail(
                    source,
                    f"{location}.reviewers[{reviewer_index}].name",
                    f"unknown reviewer lane {name!r}; supported reviewer lanes: {supported}",
                )


def validate_config(value: Mapping[str, Any], source: PathLike = "<resolved>") -> None:
    """Validate a complete, active review-profile table.

    Layer loading uses the same field checks but permits sparse patches for
    identities already supplied by lower layers. This public function is for a
    fully resolved table and therefore requires every active field and every
    entry's effort.
    """
    if not isinstance(value, dict):
        _fail(source, "top level", f"must be a mapping, got {type(value).__name__}")
    _validate_resolved(value, source, require_runtime_coverage=True)


def merge_records(
    base: Sequence[Any],
    override: Sequence[Any],
    *,
    identity: str,
) -> list[Any]:
    """Patch known records by ``identity`` and append unknown records."""
    merged = deepcopy(list(base))
    index = {
        record[identity]: position
        for position, record in enumerate(merged)
        if isinstance(record, dict) and identity in record
    }
    for record in override:
        if not isinstance(record, dict) or identity not in record:
            raise ConfigError(
                f"cannot merge review-profile records: every record needs {identity!r}"
            )
        record_id = record[identity]
        if record_id in index:
            position = index[record_id]
            merged[position] = deep_merge(merged[position], record)
        else:
            index[record_id] = len(merged)
            merged.append(deepcopy(record))
    return merged


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge mappings and the two identity-addressed record lists."""
    result = deepcopy(dict(base))
    for key, value in override.items():
        current = result.get(key)
        if key == "profiles" and isinstance(current, list) and isinstance(value, list):
            result[key] = merge_records(current, value, identity="id")
        elif key == "reviewers" and isinstance(current, list) and isinstance(value, list):
            result[key] = merge_records(current, value, identity="name")
        elif isinstance(current, dict) and isinstance(value, dict):
            result[key] = deep_merge(current, value)
        else:
            result[key] = deepcopy(value)
    return result


def _without_disabled(value: Mapping[str, Any]) -> dict[str, Any]:
    """Remove disabled profiles/reviewers and their control fields."""
    result = {key: deepcopy(item) for key, item in value.items() if key != "profiles"}
    profiles: list[dict[str, Any]] = []
    for profile in value.get("profiles", []):
        if not isinstance(profile, dict) or profile.get("disabled") is True:
            continue
        cleaned_profile = {
            key: deepcopy(item) for key, item in profile.items() if key != "disabled"
        }
        reviewers: list[dict[str, Any]] = []
        for reviewer in cleaned_profile.get("reviewers", []):
            if not isinstance(reviewer, dict) or reviewer.get("disabled") is True:
                continue
            reviewers.append(
                {key: deepcopy(item) for key, item in reviewer.items() if key != "disabled"}
            )
        if "reviewers" in cleaned_profile:
            cleaned_profile["reviewers"] = reviewers
        profiles.append(cleaned_profile)
    result["profiles"] = profiles
    return result


def resolve_config(
    project_root: PathLike,
    *,
    home: PathLike | None = None,
) -> tuple[dict[str, Any], list[Provenance]]:
    """Resolve and validate all layers in increasing precedence order.

    A malformed or structurally invalid layer raises ``ConfigError`` at once.
    Completeness findings are collected across EVERY layer and raised together
    as one ``IncompleteConfigError``, so a caller sees every gap in one pass.

    ``home`` is an explicit seam for tests and embedding callers. When omitted,
    the user layer is exactly ``~/.claude/config/review_profiles.yaml``.
    """
    config: dict[str, Any] = {}
    provenance: list[Provenance] = []
    findings: list[str] = []
    for layer, path in layer_paths(project_root, home=home):
        data = load_layer(path)
        if data is None:
            if layer == "shipped":
                raise ConfigError(f"shipped review-profile defaults are missing: {path}")
            provenance.append((layer, path, "absent"))
            continue
        if not data:
            provenance.append((layer, path, "empty"))
            continue

        _validate_layer(data, path, base=config)
        findings.extend(completeness_findings(data, f"{layer} {path}"))
        config = deep_merge(config, data)
        provenance.append((layer, path, "applied"))

    if findings:
        raise IncompleteConfigError(findings)

    _validate_resolved(config, "resolved review profiles")
    config = _without_disabled(config)
    _validate_resolved(
        config,
        "resolved review profiles after disabled records",
        require_runtime_coverage=True,
    )
    return config, provenance


def _complete_entries(value: Any, location: str, *, exactly_one: bool) -> list[dict[str, str]]:
    """Return ``value`` as normalized ``{id, effort}`` entries, or raise.

    The ids are stripped by ``model_declaration.parse``. Anything other than a
    list of complete entries is refused: a caller that skipped resolution must
    not get a table a dispatcher would read with an effort missing.
    """
    if not isinstance(value, list) or not all(
        isinstance(entry, dict)
        and set(entry) == ENTRY_FIELDS
        and entry["effort"] in EFFORT_LEVELS
        for entry in value
    ):
        raise ConfigError(
            f"{location}: {value!r} is not a list of complete "
            "{id: <model>, effort: <level>} entries; resolve the configuration "
            "with resolve_config before projecting or rendering the table"
        )
    try:
        ids = model_declaration.parse([entry["id"] for entry in value])
    except model_declaration.DeclarationError as exc:
        raise ConfigError(f"{location}: {exc}") from exc
    if exactly_one and len(ids) != 1:
        raise ConfigError(f"{location}: must hold exactly one entry, got {ids!r}")
    return [
        {"id": model_id, "effort": entry["effort"]}
        for model_id, entry in zip(ids, value)
    ]


def apply_model_priority(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize every reviewer's and validator's entries in a resolved table.

    Each reviewer's ``model`` becomes its ordered ``[{id, effort}, ...]`` list
    and each validator reason its one-entry list, with ids stripped. An entry
    that is not complete raises ``ConfigError``.
    """
    resolved = deepcopy(dict(config))
    for profile in resolved.get("profiles", []):
        profile_location = f"profile {str(profile.get('id'))!r}"
        profile["validator_models"] = {
            reason: _complete_entries(
                entries,
                f"{profile_location} validator {reason!r}",
                exactly_one=True,
            )
            for reason, entries in profile["validator_models"].items()
        }
        for reviewer in profile["reviewers"]:
            reviewer["model"] = _complete_entries(
                reviewer["model"],
                f"{profile_location} lane {str(reviewer.get('name'))!r}",
                exactly_one=False,
            )
    return resolved


def canonical_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return only executable fields in deterministic key order."""
    active = _without_disabled(value)
    profiles: list[dict[str, Any]] = []
    for profile in active.get("profiles", []):
        profile_location = f"profile {str(profile.get('id'))!r}"
        selection = profile["selection"]
        selection_projection: dict[str, Any] = {}
        if "data_only_extensions" in selection:
            selection_projection["data_only_extensions"] = list(
                selection["data_only_extensions"]
            )
        profiles.append(
            {
                "id": profile["id"],
                "selection": selection_projection,
                "reviewers": [
                    {
                        "name": reviewer["name"],
                        "model": _complete_entries(
                            reviewer["model"],
                            f"{profile_location} lane {str(reviewer['name'])!r}",
                            exactly_one=False,
                        ),
                    }
                    for reviewer in profile["reviewers"]
                ],
                "validator_models": {
                    reason: _complete_entries(
                        entries,
                        f"{profile_location} validator {reason!r}",
                        exactly_one=True,
                    )
                    for reason, entries in profile["validator_models"].items()
                },
            }
        )
    return {"profiles": profiles}


def render_projection(value: Mapping[str, Any]) -> str:
    """Render the canonical executable table as YAML, including its newline."""
    yaml = _require_yaml()
    return yaml.safe_dump(
        canonical_projection(value),
        sort_keys=False,
        allow_unicode=False,
        width=100,
    )


def render_provenance(provenance: Sequence[Provenance]) -> str:
    """Render applied-layer names and absent override creation paths."""
    applied = [layer for layer, _path, status in provenance if status.startswith("applied")]
    lines = ["Layers applied: " + (", ".join(applied) if applied else "none") + "."]
    absent = [
        f"{layer} ({path})"
        for layer, path, status in provenance
        if status == "absent"
    ]
    if absent:
        lines.extend(["", "To change this policy, create: " + "; ".join(absent) + "."])
    return "\n".join(lines)


def render(value: Mapping[str, Any], provenance: Sequence[Provenance] | None = None) -> str:
    """Render the YAML projection and, when supplied, its provenance."""
    table = render_projection(value).rstrip("\n")
    if provenance is None:
        return table + "\n"
    return table + "\n---\n\n" + render_provenance(provenance) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for resolving review profiles.

    Both modes share one exit contract: 1 when the layers hold completeness
    findings (every finding on stderr, one per line), 2 when a layer is
    malformed or invalid. Without ``--check`` a clean resolve prints the table
    and its provenance; with ``--check`` it prints no table and exits 0.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project-root",
        default=str(Path.cwd()),
        help="Project root for the project config layer (default: cwd)",
    )
    parser.add_argument(
        "--home",
        help="Override the home root used for the user layer (for isolated callers/tests)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report every incomplete entry across all layers on stderr and print "
            "no table. Exit 0 complete, 1 findings, 2 malformed or invalid layer."
        ),
    )
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if args.home else None

    try:
        config, provenance = resolve_config(project_root, home=home)
        config = apply_model_priority(config)
    except IncompleteConfigError as exc:
        print(f"review profiles config error: {exc}", file=sys.stderr)
        return 1
    except ConfigError as exc:
        print(f"review profiles config error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        sys.stdout.write(
            "review profiles: complete. " + render_provenance(provenance).splitlines()[0] + "\n"
        )
        return 0
    sys.stdout.write(render(config, provenance))
    return 0


if __name__ == "__main__":
    sys.exit(main())
