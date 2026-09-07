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
string: an ordered priority list whose first resolving entry becomes the lane's
model, replaced wholesale by any higher layer that states one. See the model
priority notes below ``REVIEWER_FIELDS`` and ``apply_model_priority``.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import importlib
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, Sequence

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
REQUIRED_PROFILE_FIELDS = frozenset({"selection", "reviewers", "validator_models"})

# --------------------------------------------------------------------------
# model priority lists: the optional llm-scripting-kit seats edge
# --------------------------------------------------------------------------
#
# A reviewer's `model` is either a string (one entry) or a non-empty ordered
# list of entries evaluated in order; the first entry that RESOLVES becomes the
# lane's model. Two entry kinds exist:
#
#   <name>        a plain Agent alias or endpoint id -- always resolves.
#   peer:<name>   resolves only when llm-scripting-kit is installed AND current
#                 enough to expose the frontier symbol below AND reports a
#                 reachable BESIDE seat for <name> -- an endpoint in the same
#                 tier but a different model family. It resolves to that seat's
#                 endpoint id.
#
# A reviewer reading the same change on a different family is the point; a
# second lane on the same family would agree with itself.
#
# The list is a PLAIN list, so a higher layer's `model` replaces it wholesale.
# That is the whole reason this replaced the former `peer_when_available`
# boolean: a by-name reviewer merge inherited the boolean, so a user override
# stating `model: fable` still ran on a peer seat.
#
# The edge is OPTIONAL (plugin-dev enabling.md). Without the owner a `peer:`
# entry simply does not resolve and the next entry does, so the rendered table
# states the model that will run and absence is silent. A resolved `peer:`
# entry is never silent: it is disclosed on stderr, one line per lane.
PEER_ENTRY_PREFIX = "peer:"
PEER_SEATS_OWNER = "llm-scripting-kit"
PEER_SEATS_MARKETPLACE = "plugins-kit"
PEER_SEATS_MODULE = "llm_scripting_kit.seats"
PEER_SEATS_FRONTIER = "llm_scripting_kit.seats.discover_seats"
# The owner version that first shipped the frontier symbol. Named in the
# too-old diagnosis so the remedy is a version rather than a guess.
PEER_SEATS_FRONTIER_VERSION = "0.28.0"
# Per-seat reachability probe budget, stated here rather than inherited: a
# review must not stall behind seat discovery.
PEER_SEATS_TIMEOUT_S = 5.0
PEER_SEATS_RELATION = "BESIDE"


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
    if isinstance(value, str):
        return [value.strip()]
    if isinstance(value, list):
        return [entry.strip() for entry in value if isinstance(entry, str)]
    return []


def _validate_model(value: Any, source: Path | str, location: str) -> None:
    """Validate a reviewer's model: a string, or a non-empty ordered list.

    A bare string is exactly a one-entry list, `peer:` prefix included -- so
    `model: peer:opus` is legal and simply has nothing to fall back to when no
    seat is reachable, which is reported as a configuration error at resolution.
    """
    if isinstance(value, str):
        entries: list[tuple[Any, str]] = [(value, location)]
    elif isinstance(value, list):
        if not value:
            _fail(source, location, "must be a non-empty list of strings, or a string")
        entries = [(entry, f"{location}[{index}]") for index, entry in enumerate(value)]
    else:
        _fail(
            source,
            location,
            "must be a string or a non-empty list of strings, got "
            f"{type(value).__name__}",
        )

    for entry, entry_location in entries:
        _validate_nonempty_string(entry, source, entry_location)
        text = entry.strip()
        if text.startswith(PEER_ENTRY_PREFIX) and not text[len(PEER_ENTRY_PREFIX):].strip():
            _fail(
                source,
                entry_location,
                f"a {PEER_ENTRY_PREFIX!r} entry must name a model, e.g. "
                f"'{PEER_ENTRY_PREFIX}opus'",
            )


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
    # Checked before the generic unknown-field message so a layer written
    # against the retired boolean is told what replaced it.
    if "peer_when_available" in value:
        _fail(
            source,
            f"{location}.peer_when_available",
            "was removed: state the preference as an ordered model priority "
            f"list instead, e.g. `model: [{PEER_ENTRY_PREFIX}<name>, <name>]`",
        )
    _validate_known_fields(value, REVIEWER_FIELDS, source, location)
    if "name" not in value:
        _fail(source, location, "required field missing: name")
    _validate_nonempty_string(value["name"], source, f"{location}.name")
    disabled = _validate_disabled(value, source, location)

    if "model" in value:
        _validate_model(value["model"], source, f"{location}.model")
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


def _peer_seats_absent_diagnosis(reason: str) -> str:
    """Diagnose state 1: the owner plugin was never installed."""
    return (
        f"absent: the {PEER_SEATS_OWNER} plugin is not installed, so no peer "
        f"seat can be discovered. Install it with `claude plugin install "
        f"{PEER_SEATS_OWNER}@{PEER_SEATS_MARKETPLACE}` and start a new session "
        f"so bootstrap links its shared library. Underlying error: {reason}"
    )


def _peer_seats_too_old_diagnosis(reason: str) -> str:
    """Diagnose states 2 and 3: installed, but not current enough (or stale)."""
    return (
        f"too old or stale: {PEER_SEATS_OWNER} is importable but does not "
        f"expose {PEER_SEATS_FRONTIER}, which first shipped in "
        f"{PEER_SEATS_OWNER} {PEER_SEATS_FRONTIER_VERSION}. Update it with "
        f"`claude plugin update {PEER_SEATS_OWNER}@{PEER_SEATS_MARKETPLACE}` "
        f"and start a new session so bootstrap re-syncs its shared library. "
        f"Underlying error: {reason}"
    )


def _probe_discover_seats() -> tuple[Any | None, str | None]:
    """Return the frontier callable, or ``None`` and a diagnosis.

    Three runtime states have to be told apart and an ``import`` cannot do it
    (plugin-dev optional-plugin-dependencies.md): absent, too old, and stale
    after an uninstall. The package import answers "absent"; everything past it
    is diagnosed by probing the frontier symbol, never by module presence.

    The import is guarded with ``ImportError`` only. A broader handler here
    would swallow a syntax error in a half-synced copy and report it as an
    absent plugin.
    """
    try:
        importlib.import_module("llm_scripting_kit")
    except ImportError as exc:
        return None, _peer_seats_absent_diagnosis(str(exc))

    try:
        module = importlib.import_module(PEER_SEATS_MODULE)
    except ImportError as exc:
        return None, _peer_seats_too_old_diagnosis(str(exc))

    found = getattr(module, "discover_seats", None)
    if not callable(found):
        return None, _peer_seats_too_old_diagnosis(
            f"{PEER_SEATS_FRONTIER} is missing"
        )
    return found, None


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


def _first_beside_endpoint(result: Any) -> str | None:
    """Return the first reachable BESIDE seat's endpoint id, if any.

    Attributes are read defensively: the shared lib is linked by a ``.pth``
    that pins no version, so a shape change reaches this venv without the
    consumer asking for it. An unexpected shape degrades to "no seat", which
    leaves the stated model in place.
    """
    for seat in getattr(result, "seats", ()) or ():
        if getattr(seat, "relation", None) != PEER_SEATS_RELATION:
            continue
        endpoint = getattr(seat, "endpoint", None)
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint
    return None


class _PeerResolver:
    """Resolve ``peer:<name>`` entries, probing the owner at most once."""

    def __init__(
        self,
        *,
        project_root: PathLike | None,
        timeout: float | None,
        discover: Any | None,
    ) -> None:
        self._project_root = project_root
        self._timeout = timeout
        self._discover = discover
        self._probed = discover is not None
        self.available = discover is not None
        self.diagnostics: list[str] = []
        # One seat probe per distinct peer target within this call, memoizing
        # the reason too so a second lane naming the same target reports the
        # same cause. This is not a cache across invocations, which the
        # enabling contract forbids.
        self._seen: dict[str, tuple[str | None, str]] = {}

    def _ensure_discover(self) -> None:
        if self._probed:
            return
        self._probed = True
        self._discover, diagnosis = _probe_discover_seats()
        if self._discover is None:
            self.diagnostics.append(str(diagnosis))
        else:
            self.available = True

    def resolve(self, target: str) -> tuple[str | None, str]:
        """Return an endpoint id for ``target``, or ``None`` and a reason."""
        self._ensure_discover()
        if self._discover is None:
            return None, f"{PEER_SEATS_OWNER} is not available"
        if target in self._seen:
            return self._seen[target]

        reason = f"no reachable {PEER_SEATS_RELATION} seat"
        try:
            result = self._discover(
                target,
                project_root=(
                    None if self._project_root is None else str(self._project_root)
                ),
                timeout=(
                    PEER_SEATS_TIMEOUT_S if self._timeout is None else self._timeout
                ),
            )
            endpoint = _first_beside_endpoint(result)
        except Exception as exc:  # noqa: BLE001 - degrade, never fail a review
            endpoint = None
            reason = "seat discovery failed"
            self.diagnostics.append(
                f"seat discovery for {PEER_ENTRY_PREFIX}{target} failed, so "
                f"every entry naming it is skipped: {type(exc).__name__}: {exc}"
            )
        outcome = (endpoint, "" if endpoint else reason)
        self._seen[target] = outcome
        return outcome


def apply_model_priority(
    config: Mapping[str, Any],
    *,
    project_root: PathLike | None = None,
    timeout: float | None = None,
    discover: Any | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Resolve every reviewer's model priority list to a single model.

    Entries are evaluated in order and the first that resolves wins: a plain
    name always resolves, a ``peer:<name>`` entry only when the owner reports a
    reachable ``BESIDE`` seat. Returns the resolved table, the disclosure lines
    a caller MUST surface, and diagnostic lines that stay out of the disclosure
    channel.

    Nothing raised by the probe or the owner escapes: a lane that cannot
    discover a seat falls through to its next entry, which is what the list
    already said would happen. A resolved ``peer:`` entry is disclosed by lane,
    so the table is never quietly different from what ran.

    ``discover`` injects the frontier callable for tests and for a caller that
    already holds one; when omitted the callable is probed for on demand, and
    only when an entry actually needs it. Probe results are deliberately not
    cached across calls -- consent to this disclosure ends with the skill
    invocation that produced it.

    Raises ``ConfigError`` when no entry of some lane's list resolves.
    """
    resolved = deepcopy(dict(config))
    peers = _PeerResolver(
        project_root=project_root, timeout=timeout, discover=discover
    )
    disclosures: list[str] = []

    for profile_id, reviewer in _reviewer_lanes(resolved):
        model = reviewer.get("model")
        entries = _model_entries(model)
        if not entries:
            continue
        lane = str(reviewer.get("name"))
        skipped: list[tuple[str, str]] = []
        chosen: str | None = None
        chosen_entry = ""

        for entry in entries:
            if not entry.startswith(PEER_ENTRY_PREFIX):
                chosen = entry
                chosen_entry = entry
                break
            target = entry[len(PEER_ENTRY_PREFIX):].strip()
            endpoint, reason = peers.resolve(target)
            if endpoint is not None:
                chosen = endpoint
                chosen_entry = entry
                break
            skipped.append((entry, reason))

        if chosen is None:
            _fail(
                "resolved review profiles",
                f"profile {profile_id!r} lane {lane!r} model",
                "no entry resolved: "
                + repr(entries)
                + ". A plain name always resolves -- add one as the last entry.",
            )

        reviewer["model"] = chosen
        if chosen_entry.startswith(PEER_ENTRY_PREFIX):
            disclosures.append(
                f"model-priority: profile {profile_id!r} lane {lane!r} runs on "
                f"{PEER_SEATS_OWNER} endpoint {chosen!r} -- priority entry "
                f"{chosen_entry!r} resolved to a reachable "
                f"{PEER_SEATS_RELATION} seat (same tier, different model "
                f"family) reported by {PEER_SEATS_FRONTIER}."
            )
        elif skipped and peers.available:
            # The owner is present and the probe ran, so the skip is a fact
            # about this fleet's seats rather than about a missing plugin --
            # which is why it is disclosed here and absence stays silent.
            detail = ", ".join(f"{entry!r} ({reason})" for entry, reason in skipped)
            disclosures.append(
                f"model-priority: profile {profile_id!r} lane {lane!r} skipped "
                f"priority entry {detail} and runs on {chosen!r}."
            )

    return resolved, disclosures, peers.diagnostics


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
                    {
                        "name": reviewer["name"],
                        "model": _projected_model(profile, reviewer),
                    }
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
    parser.add_argument(
        "--explain-peer-seats",
        action="store_true",
        help=(
            "Print why a `peer:<name>` priority entry did not resolve (absent "
            "vs too old or stale owner). Diagnostics only -- never part of the "
            "rendered table."
        ),
    )
    args = parser.parse_args(argv)
    project_root = Path(args.project_root).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if args.home else None

    try:
        config, provenance = resolve_config(project_root, home=home)
        config, disclosures, diagnostics = apply_model_priority(
            config, project_root=project_root
        )
    except ConfigError as exc:
        print(f"review profiles config error: {exc}", file=sys.stderr)
        return 1

    # Disclosures always reach stderr: a resolved peer entry that nobody was
    # told about would make the rendered table a false claim about what ran.
    # Diagnostics stay behind the flag -- an absent or too-old owner is silent,
    # because the table then states exactly the model the lane will use.
    for line in disclosures:
        print(line, file=sys.stderr)
    if args.explain_peer_seats:
        for line in diagnostics:
            print(f"model-priority: {line}", file=sys.stderr)

    sys.stdout.write(render(config, provenance))
    return 0


if __name__ == "__main__":
    sys.exit(main())
