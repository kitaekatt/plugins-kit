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
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence


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
REQUIRED_PROFILE_FIELDS = frozenset({"selection", "reviewers", "validator_models"})


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
    """
    if not isinstance(value, dict):
        _fail(source, location, f"must be a mapping, got {type(value).__name__}")
    for reason, model in value.items():
        if not isinstance(reason, str) or not reason.strip():
            _fail(source, f"{location} key {reason!r}", "must be a non-empty string")
        _validate_nonempty_string(model, source, f"{location}.{reason}")


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
    _validate_known_fields(value, REVIEWER_FIELDS, source, location)
    if "name" not in value:
        _fail(source, location, "required field missing: name")
    _validate_nonempty_string(value["name"], source, f"{location}.name")
    disabled = _validate_disabled(value, source, location)

    if "model" in value:
        _validate_nonempty_string(value["model"], source, f"{location}.model")
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


def _validate_resolved(value: Mapping[str, Any], source: Path | str) -> None:
    """Validate the merged table, including required fields of active records."""
    _validate_known_fields(value, TOP_LEVEL_FIELDS, source, "top level")
    if "profiles" not in value:
        _fail(source, "top level", "required field missing: profiles")
    profiles = value["profiles"]
    if not isinstance(profiles, list):
        _fail(source, "profiles", f"must be a list, got {type(profiles).__name__}")

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


def validate_config(value: Mapping[str, Any], source: PathLike = "<resolved>") -> None:
    """Validate a complete, active review-profile table.

    Layer loading uses the same field checks but permits sparse patches for
    identities already supplied by lower layers. This public function is for a
    fully resolved table and therefore requires every active field.
    """
    if not isinstance(value, dict):
        _fail(source, "top level", f"must be a mapping, got {type(value).__name__}")
    _validate_resolved(value, source)


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
    _validate_resolved(config, "resolved review profiles after disabled records")
    return config, provenance


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
                    {"name": reviewer["name"], "model": reviewer["model"]}
                    for reviewer in profile["reviewers"]
                ],
                "validator_models": {
                    reason: model
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
    except ConfigError as exc:
        print(f"review profiles config error: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(render(config, provenance))
    return 0


if __name__ == "__main__":
    sys.exit(main())
