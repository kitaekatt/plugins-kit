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

A reviewer's ``model`` is one of those ordinary lists when it is not a bare
string: an ordered priority list whose first entry becomes the lane's model,
replaced wholesale by any higher layer that states one. What is left of
that list survives into the resolved table as ``model_fallbacks``. See the model
priority notes below ``REVIEWER_FIELDS`` and ``apply_model_priority``.
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
REVIEWER_FIELDS = frozenset(
    {"name", "model", "model_fallbacks", "effort", "disabled"}
)
REQUIRED_PROFILE_FIELDS = frozenset({"selection", "reviewers", "validator_models"})

# --------------------------------------------------------------------------
# effort: the reasoning budget one lane runs under
# --------------------------------------------------------------------------
# `effort` is OPTIONAL on a reviewer record. Absent, the lane inherits the
# invoking session's effort -- the pre-existing behavior, kept as the default so
# an unstated effort is never a silent change.
#
# It is deliberately a fixed MENU rather than the free-form string `model` is.
# A model may name an endpoint id this library knows nothing about, but effort
# is a vocabulary the Agent tool defines, so a typo ("lo", "minimal") would
# otherwise resolve to an agent that does not exist and fail at dispatch, far
# from the configuration line that caused it.
#
# An effort-carrying lane dispatches to a per-level reviewer AGENT instead of to
# `general-purpose`, because the Agent tool has no effort parameter -- effort is
# set in an agent definition's frontmatter. git-kit and p4-kit each ship the
# agents as `<kit>:review-lane-<level>`, and the skill's step-6 dispatch rule
# maps a resolved level to that name. Model is unaffected: a call-site `model`
# overrides an agent definition's frontmatter, so the profile keeps owning model
# exactly as it did, and the agent contributes effort and nothing else.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# --------------------------------------------------------------------------
# model priority lists
# --------------------------------------------------------------------------
#
# A reviewer's `model` is a model declaration in the shared format
# (bootstrap_lib.model_declaration; plugin-dev references/model-declaration.md):
# a string (one entry) or a non-empty ordered list naming no entry twice. Every
# entry names a plain Agent alias or endpoint id, and the first entry always
# becomes the lane's model.
#
# The list is a PLAIN list, so a higher layer's `model` replaces it wholesale.
#
# --------------------------------------------------------------------------
# model_fallbacks: what is left of the list once the first entry is chosen
# --------------------------------------------------------------------------
#
# Resolution answers "which model does this lane START on". Whether the model
# WORKS is only learned at dispatch, where an endpoint can be out of credits,
# rate limited, or withdrawn -- none of which this module can see. So the
# entries after the first are carried into the resolved table rather than
# discarded: a caller whose dispatch fails already holds the order the user
# asked for, instead of having to re-resolve the configuration mid-review to
# learn what to try next. Whether it falls over is the caller's decision; what
# it may fall over TO is this module's answer. An empty chain is stated rather
# than left out: "nothing left to try" is an answer.


class ConfigError(ValueError):
    """A review-profile layer is unreadable, malformed, or invalid."""


# The longer name is useful to callers that want to distinguish this module's
# errors from the bootstrap engine's other configuration errors. Keep the
# short alias as well because it matches bootstrap_lib.config_resolve.
ReviewProfilesConfigError = ConfigError


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


def _model_entries(value: Any) -> list[str]:
    """Return a model's ordered entries, treating a string as a one-entry list."""
    try:
        return model_declaration.parse(value)
    except model_declaration.DeclarationError:
        return []


def _parse_declaration(value: Any, source: Path | str, location: str) -> list[str]:
    """Parse a model declaration through the shared validator.

    The grammar -- a list of ids, a scalar read as a one-element list, no empty
    list, no repeated id -- is bootstrap_lib.model_declaration's, so every
    plugin reading a declaration rejects the same shapes. The error is
    re-raised as this module's ``ConfigError`` with the entry's location.
    """
    try:
        return model_declaration.parse(value)
    except model_declaration.DeclarationError as exc:
        where = location if exc.index is None else f"{location}[{exc.index}]"
        _fail(source, where, str(exc))


def _validate_model(value: Any, source: Path | str, location: str) -> None:
    """Validate a reviewer's model declaration: an ordered list of ids.

    A bare string is exactly a one-entry list.
    """
    _parse_declaration(value, source, location)


def _validate_model_fallbacks(value: Any, source: Path | str, location: str) -> None:
    """Validate a lane's fallback chain: a possibly-empty list of model names.

    ``apply_model_priority`` derives this field, but the resolved table is
    validated again on the way out and a caller may hand one back in, so the
    shape is checked here rather than trusted. Empty is legal and carries a
    claim of its own -- the lane has nothing left to try -- which is why the key
    is kept rather than dropped when the list is empty.
    """
    if not isinstance(value, list):
        _fail(
            source,
            location,
            f"must be a list of strings, got {type(value).__name__}",
        )
    for index, entry in enumerate(value):
        _validate_nonempty_string(entry, source, f"{location}[{index}]")


def _validate_effort(value: Any, source: Path | str, location: str) -> None:
    """Validate a reviewer's effort against the fixed level menu.

    Unlike ``model`` this is a closed vocabulary (see ``EFFORT_LEVELS``), so an
    unknown level is rejected here rather than becoming a dispatch to an agent
    that does not exist.
    """
    if not isinstance(value, str) or not value.strip():
        _fail(source, location, "must be a non-empty string")
    if value not in EFFORT_LEVELS:
        levels = ", ".join(repr(level) for level in EFFORT_LEVELS)
        _fail(source, location, f"unknown effort {value!r}; known levels: {levels}")


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
    """Validate reason-to-model bindings.

    Reason keys are intentionally extensible. ``bug`` and ``claude_md`` are
    the shipped reasons; a new reason is an addressable mapping record and is
    appended by the normal mapping merge.

    Each value is a model declaration naming exactly ONE id, written as a
    one-element list or, equivalently, a bare string. A validator is never
    endpoint-eligible and has no failover chain, so a second entry would be a
    preference nothing honours; it is refused rather than silently ignored.
    """
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    for reason, model in value.items():
        if not isinstance(reason, str) or not reason.strip():
            _fail(source, f"{location} key {reason!r}", "must be a non-empty string")
        entries = _parse_declaration(model, source, f"{location}.{reason}")
        if len(entries) != 1:
            _fail(
                source,
                f"{location}.{reason}",
                f"must name exactly one id, got {entries!r}: a validator has no "
                "failover chain",
            )


def _validator_model(value: Any) -> str:
    """Return a validator declaration's one id (a scalar or a one-element list)."""
    return model_declaration.parse(value)[0]


def _validate_reviewer(
    value: Any,
    source: Path | str,
    location: str,
    *,
    existing: Mapping[str, Any] | None,
    parent_disabled: bool,
    complete: bool,
) -> None:
    """Validate one reviewer, allowing sparse patches of known records."""
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
    _validate_known_fields(value, REVIEWER_FIELDS, source, location)
    if "name" not in value:
        _fail(source, location, "required field missing: name")
    _validate_nonempty_string(value["name"], source, f"{location}.name")
    disabled = _validate_disabled(value, source, location)

    if "model" in value:
        _validate_model(value["model"], source, f"{location}.model")
    if "model_fallbacks" in value:
        _validate_model_fallbacks(
            value["model_fallbacks"], source, f"{location}.model_fallbacks"
        )
    if "effort" in value:
        _validate_effort(value["effort"], source, f"{location}.effort")
    needs_model = (complete and not parent_disabled) or (existing is None and not parent_disabled)
    if needs_model and not disabled and "model" not in value:
        _fail(source, location, "required field missing: model")


def _validate_reviewers(
    value: Any,
    source: Path | str,
    location: str,
    *,
    existing_profile: Mapping[str, Any] | None,
    parent_disabled: bool,
    complete: bool,
) -> None:
    """Validate a profile's reviewer record list and its identities."""
    if not isinstance(value, list):
        _fail(source, location, f"must be a list, got {type(value).__name__}")
    existing_reviewers = _records_by_name(
        existing_profile.get("reviewers") if existing_profile else None,
        "name",
    )
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
            existing = existing_reviewers.get(name)
        else:
            existing = None
        _validate_reviewer(
            reviewer,
            source,
            reviewer_location,
            existing=existing,
            parent_disabled=parent_disabled,
            complete=complete,
        )


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
        _validate_reviewers(
            value["reviewers"],
            source,
            f"{location}.reviewers",
            existing_profile=existing,
            parent_disabled=disabled,
            complete=complete,
        )
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
    """Validate a sparse layer before it participates in the merge."""
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
    """Validate the merged table, including required fields of active records."""
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
        if not require_runtime_coverage:
            continue
        reviewers = profile["reviewers"]
        if not reviewers:
            _fail(
                source,
                f"{location}.reviewers",
                f"profile {profile['id']!r} must contain at least one active reviewer",
            )
        supported = sorted(lane_prompts.KNOWN_LANES - {"validator"})
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
    fully resolved table and therefore requires every active field.
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

    ``home`` is an explicit seam for tests and embedding callers. When omitted,
    the user layer is exactly ``~/.claude/config/review_profiles.yaml``.
    """
    config: dict[str, Any] = {}
    provenance: list[Provenance] = []
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
        config = deep_merge(config, data)
        provenance.append((layer, path, "applied"))

    _validate_resolved(config, "resolved review profiles")
    config = _without_disabled(config)
    _validate_resolved(
        config,
        "resolved review profiles after disabled records",
        require_runtime_coverage=True,
    )
    return config, provenance


def _reviewer_lanes(
    config: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return (profile id, reviewer record) for every reviewer in the table."""
    lanes: list[tuple[str, dict[str, Any]]] = []
    for profile in config.get("profiles", []):
        if not isinstance(profile, dict):
            continue
        for reviewer in profile.get("reviewers", []):
            if not isinstance(reviewer, dict):
                continue
            lanes.append((str(profile.get("id")), reviewer))
    return lanes


def apply_model_priority(config: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve every reviewer's model priority list to a single model.

    The first entry of an ordered model declaration always becomes the lane's
    model. Each lane also gains ``model_fallbacks``, the remainder of its list,
    so a caller that finds the chosen model unusable at dispatch can fail over
    without re-resolving the configuration.
    """
    resolved = deepcopy(dict(config))
    # A validator declaration names exactly one id (validated at load), so it
    # resolves to that id; the table then carries one string per validator,
    # exactly as it carries one per reviewer lane.
    for profile in resolved.get("profiles", []):
        if isinstance(profile, dict) and isinstance(profile.get("validator_models"), dict):
            profile["validator_models"] = {
                reason: _validator_model(model)
                for reason, model in profile["validator_models"].items()
            }

    for _profile_id, reviewer in _reviewer_lanes(resolved):
        model = reviewer.get("model")
        entries = _model_entries(model)
        if not entries:
            continue
        reviewer["model"] = entries[0]
        reviewer["model_fallbacks"] = entries[1:]

    return resolved


def _projected_model(profile: Mapping[str, Any], reviewer: Mapping[str, Any]) -> str:
    """Return a lane's resolved model, refusing to project an unresolved list.

    The stdout table is what the runner dispatches from, so a priority list
    must never reach it: a list would be read as an endpoint id and dispatched
    as one. Every projecting caller resolves first.
    """
    model = reviewer["model"]
    if isinstance(model, str):
        return model
    raise ConfigError(
        f"profile {str(profile.get('id'))!r} lane {str(reviewer.get('name'))!r}: "
        f"model is still a priority list ({model!r}); call apply_model_priority "
        "before projecting or rendering the table"
    )


def canonical_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return only executable fields in deterministic key order."""
    active = _without_disabled(value)
    profiles: list[dict[str, Any]] = []
    for profile in active.get("profiles", []):
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
                    # `effort` is omitted when unset rather than projected as a
                    # null: absent means "inherit the session's effort", and a
                    # rendered `effort: null` would read as a stated level.
                    # `model_fallbacks` takes the opposite treatment and is
                    # rendered even when empty, because the agent reading this
                    # table asks it a question -- what may this lane fall over
                    # to -- and an absent key would leave "nothing" and "this
                    # table does not say" spelled identically.
                    {
                        "name": reviewer["name"],
                        "model": _projected_model(profile, reviewer),
                        "model_fallbacks": list(
                            reviewer.get("model_fallbacks", ())
                        ),
                        **(
                            {"effort": reviewer["effort"]}
                            if "effort" in reviewer
                            else {}
                        ),
                    }
                    for reviewer in profile["reviewers"]
                ],
                "validator_models": {
                    reason: _validator_model(model)
                    for reason, model in profile["validator_models"].items()
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
    """CLI entry point for resolving review profiles."""
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
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if args.home else None

    try:
        config, provenance = resolve_config(project_root, home=home)
        config = apply_model_priority(config)
    except ConfigError as exc:
        print(f"review profiles config error: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(render(config, provenance))
    return 0


if __name__ == "__main__":
    sys.exit(main())
