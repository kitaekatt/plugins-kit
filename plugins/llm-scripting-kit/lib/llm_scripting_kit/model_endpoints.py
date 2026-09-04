"""Registry of OpenAI-compatible model endpoints declared in the user profile.

A *model endpoint* is any OpenAI-compatible server a harness or a script can
drive -- typically a locally hosted, keyless one (llama.cpp, ollama, vllm, LM
Studio), but a keyed vendor endpoint is expressible too. The endpoints
themselves are machine-specific facts (host names, ports, model ids), so this
module ships only the READER: the declarations live in a file the user owns.

Location, in order:

1. ``$MODEL_ENDPOINTS_REGISTRY`` when set -- an explicit path (``~`` expanded).
2. otherwise ``~/.claude/config/model-endpoints.yaml`` -- the convention. The
   expression is home-relative, so it is the same on every machine and names
   none.

Absence semantics differ by how the path was chosen, deliberately:

- CONVENTION path, file missing -> an EMPTY registry. The convention is probed
  on every machine, so absence is the normal not-opted-in state, never an
  error.
- OVERRIDE set, file missing -> :class:`EndpointRegistryError`. An explicit
  pointer that dangles is a misconfiguration and must be loud.
- EITHER path, file EXISTS but is unparseable or schema-invalid ->
  :class:`EndpointRegistryError` naming the path and the defect. Individual
  entries that cannot be classified or validated are skipped with notes, but a
  present registry that cannot be read must never read as a silent empty.

File schema (``version:`` exists for a true break; unknown keys are ignored so
the schema can grow additively)::

    version: 1
    default: <entry id>        # the entry used when a caller names none
    models:
      <entry id>:
        base_url: http://<host>:<port>/v1   # required, OpenAI-compatible base
        model: <model id>                   # required, what the server serves
        name: <human label>                 # optional
        context_window: <tokens>            # optional
        reasoning_effort: <effort>          # optional per-entry default
        key_env: <ENV VAR>                  # optional; omitted = keyless
      <harness entry id>:
        harness: <harness name>             # required instead of base_url
        model: <model id>                   # required, what the harness drives
        effort: <effort>                    # optional harness setting
        name: <human label>                 # optional

Entry ids are the endpoint names: ``llm_scripting_kit.models.resolve_endpoint``
injects each entry as a named endpoint, so ``resolve_endpoint("<entry id>")``
resolves it. The DEFAULT entry has no magic endpoint name -- it is reached
through this module's API, ``resolve_registry_entry(None)``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, Optional

#: Environment variable overriding the registry path.
REGISTRY_ENV = "MODEL_ENDPOINTS_REGISTRY"

#: The conventional location, relative to the user's home directory.
REGISTRY_RELATIVE_PATH = Path(".claude") / "config" / "model-endpoints.yaml"

# Entry kinds are deliberately strings: the registry is a YAML seam shared by
# consumers that do not need to import an enum just to decide which adapter can
# honour an entry.
TRANSPORT_KIND = "transport"
HARNESS_KIND = "harness"


def default_registry_path() -> Path:
    """The conventional registry path for the current user.

    Resolved per call rather than bound at import time: the home directory is
    a runtime fact (and tests redirect it), so a module constant would freeze
    whichever home happened to exist when the module was first imported.
    """
    return Path.home() / REGISTRY_RELATIVE_PATH


class EndpointRegistryError(Exception):
    """The registry exists but could not be read, or a requested entry is absent."""


class EndpointMetadataError(EndpointRegistryError):
    """An entry has invalid frontier classification metadata."""


@dataclass(frozen=True)
class EndpointEntry:
    """One declared model entry. ``key_env`` None means keyless.

    The first fields retain the original transport-entry order so callers that
    construct an ``EndpointEntry`` positionally keep working.  A harness entry
    has ``base_url`` set to None and carries its harness-specific fields.
    """

    id: str
    base_url: Optional[str]
    model: str
    name: Optional[str] = None
    context_window: Optional[int] = None
    reasoning_effort: Optional[str] = None
    key_env: Optional[str] = None
    kind: str = TRANSPORT_KIND
    harness: Optional[str] = None
    effort: Optional[str] = None
    tier: Optional[int] = None
    family: Optional[str] = None


@dataclass(frozen=True)
class EndpointRegistry:
    """The parsed registry. ``entries`` is empty when no registry exists.

    ``path`` is the file the entries came from, or None for the empty
    not-opted-in registry -- carried so a consumer can name the file in its own
    diagnostics.
    """

    default_id: Optional[str] = None
    entries: Dict[str, EndpointEntry] = field(default_factory=dict)
    path: Optional[Path] = None
    notes: list[str] = field(default_factory=list)


def harness_entry_message(entry_id: str, harness: Optional[str]) -> str:
    """The one wording for "this entry cannot serve as an HTTP endpoint".

    Both stores refuse a harness entry at their own boundary, and a reader who
    hits the refusal from either side must not have to recognize two sentences
    as the same fact. Defined here because ``models`` imports this module and
    not the reverse.
    """
    return (
        f"endpoint '{entry_id}' is a harness entry (harness: {harness or '<unknown>'}) "
        "and has no transport base_url; it cannot be used as an "
        "OpenAI-compatible endpoint"
    )


def _require_str(value: object, *, path: Path, entry_id: str, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}': entry '{entry_id}' has no "
            f"'{key}' (a non-empty string is required)"
        )
    return value.strip()


def _optional_int(value: object, *, path: Path, entry_id: str, key: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}': entry '{entry_id}' has a "
            f"non-integer '{key}' ({value!r})"
        )
    try:
        return int(value)
    except ValueError as exc:
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}': entry '{entry_id}' has a "
            f"non-integer '{key}' ({value!r})"
        ) from exc


def _optional_str(value: object, *, path: Path, entry_id: str, key: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}': entry '{entry_id}' has a "
            f"non-string '{key}' ({value!r})"
        )
    return value.strip()


def parse_classification_fields(
    raw: Mapping[str, object], *, source: str, entry_id: str
) -> "tuple[Optional[int], Optional[str]]":
    """Validate and return the optional frontier classification fields."""
    tier: Optional[int] = None
    if "tier" in raw:
        value = raw["tier"]
        if isinstance(value, bool) or not isinstance(value, int) or value not in range(1, 5):
            raise EndpointMetadataError(
                f"{source}: entry '{entry_id}' has invalid 'tier' ({value!r}); "
                "expected an integer from 1 to 4"
            )
        tier = value

    family: Optional[str] = None
    if "family" in raw:
        value = raw["family"]
        if not isinstance(value, str) or not value.strip():
            raise EndpointMetadataError(
                f"{source}: entry '{entry_id}' has invalid 'family' ({value!r}); "
                "expected a non-empty string"
            )
        family = value.strip()
    return tier, family


def _resolve_registry_path(env: Mapping[str, str]) -> "tuple[Path, bool]":
    """Return (path, explicit) -- ``explicit`` when the override chose it."""
    override = (env.get(REGISTRY_ENV) or "").strip()
    if override:
        return Path(os.path.expanduser(override)), True
    return default_registry_path(), False


def load_endpoint_registry(
    environ: Optional[Mapping[str, str]] = None,
) -> EndpointRegistry:
    """Read the model-endpoints registry. See the module docstring for the
    path resolution and the absence semantics.

    Raises:
        EndpointRegistryError: a dangling override path, an unparseable file,
            or a file whose top-level schema/default is invalid. Individual
            entry defects are recorded in ``EndpointRegistry.notes``.
    """
    env = os.environ if environ is None else environ
    path, explicit = _resolve_registry_path(env)

    if not path.is_file():
        if explicit:
            raise EndpointRegistryError(
                f"{REGISTRY_ENV} names '{path}', which is not a readable file"
            )
        return EndpointRegistry()

    try:
        import yaml  # noqa: PLC0415 -- optional at import time, required here
    except ImportError as exc:  # pragma: no cover - pyyaml is a declared dep
        raise EndpointRegistryError(
            f"cannot read model-endpoints registry '{path}': PyYAML is not "
            "installed in this interpreter"
        ) from exc

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 -- any read/parse defect is one error
        raise EndpointRegistryError(
            f"cannot read model-endpoints registry '{path}': {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}' is not a YAML mapping"
        )

    raw_models = data.get("models")
    if not isinstance(raw_models, dict):
        raise EndpointRegistryError(
            f"model-endpoints registry '{path}' has no 'models' map"
        )

    entries: Dict[str, EndpointEntry] = {}
    notes: list[str] = []
    for entry_id, raw in raw_models.items():
        key = str(entry_id)
        if not isinstance(raw, dict):
            notes.append(
                f"model-endpoints registry '{path}': entry '{key}' skipped; "
                "it is not a mapping"
            )
            continue
        has_base_url = "base_url" in raw
        has_harness = "harness" in raw
        if has_base_url and has_harness:
            notes.append(
                f"model-endpoints registry '{path}': entry '{key}' skipped; "
                "both 'base_url' and 'harness' are present (contradictory kinds)"
            )
            continue
        if not has_base_url and not has_harness:
            notes.append(
                f"model-endpoints registry '{path}': entry '{key}' skipped; "
                "unknown kind (neither 'base_url' nor 'harness' is present)"
            )
            continue

        if has_harness:
            try:
                tier, family = parse_classification_fields(
                    raw, source=f"model-endpoints registry '{path}'", entry_id=key
                )
                entries[key] = EndpointEntry(
                    id=key,
                    base_url=None,
                    model=_require_str(
                        raw.get("model"), path=path, entry_id=key, key="model"
                    ),
                    name=_optional_str(
                        raw.get("name"), path=path, entry_id=key, key="name"
                    ),
                    kind=HARNESS_KIND,
                    harness=_require_str(
                        raw.get("harness"), path=path, entry_id=key, key="harness"
                    ),
                    effort=_optional_str(
                        raw.get("effort"), path=path, entry_id=key, key="effort"
                    ),
                    tier=tier,
                    family=family,
                )
            except EndpointMetadataError:
                raise
            except EndpointRegistryError as exc:
                notes.append(f"{exc}; entry skipped")
            continue

        try:
            tier, family = parse_classification_fields(
                raw, source=f"model-endpoints registry '{path}'", entry_id=key
            )
            entries[key] = EndpointEntry(
                id=key,
                base_url=_require_str(
                    raw.get("base_url"), path=path, entry_id=key, key="base_url"
                ),
                model=_require_str(
                    raw.get("model"), path=path, entry_id=key, key="model"
                ),
                name=_optional_str(
                    raw.get("name"), path=path, entry_id=key, key="name"
                ),
                context_window=_optional_int(
                    raw.get("context_window"), path=path, entry_id=key, key="context_window"
                ),
                reasoning_effort=_optional_str(
                    raw.get("reasoning_effort"),
                    path=path,
                    entry_id=key,
                    key="reasoning_effort",
                ),
                key_env=_optional_str(
                    raw.get("key_env"), path=path, entry_id=key, key="key_env"
                ),
                kind=TRANSPORT_KIND,
                tier=tier,
                family=family,
            )
        except EndpointMetadataError:
            raise
        except EndpointRegistryError as exc:
            notes.append(f"{exc}; entry skipped")

    default_id = data.get("default")
    if default_id is not None:
        default_id = str(default_id)
        if default_id not in entries:
            # Two different failures reach here and they need different words.
            # The id may name no entry at all (a typo in `default:`), or it may
            # name an entry that IS in the file and was skipped by the loop
            # above. Saying "names no entry" for the second is false, and it
            # points the reader at the wrong line -- the notes list holds the
            # real defect but rides on a registry object this raise never
            # returns, so the diagnostic has to be carried into the message.
            skipped_note = next(
                (n for n in notes if f"entry '{default_id}'" in n or f"'{default_id}'" in n),
                None,
            )
            if default_id in {str(k) for k in raw_models}:
                detail = skipped_note or "the entry was skipped"
                raise EndpointRegistryError(
                    f"model-endpoints registry '{path}': default '{default_id}' "
                    f"names an entry that could not be loaded -- {detail}"
                )
            known = ", ".join(sorted(entries)) or "<none>"
            raise EndpointRegistryError(
                f"model-endpoints registry '{path}': default '{default_id}' "
                f"names no entry (known: {known})"
            )

    return EndpointRegistry(default_id=default_id, entries=entries, path=path, notes=notes)


def resolve_registry_entry(
    name: Optional[str] = None,
    *,
    registry: Optional[EndpointRegistry] = None,
) -> EndpointEntry:
    """Return one entry. ``name`` None means the registry's ``default`` entry.

    Pass ``registry`` to resolve against an already-loaded registry (skips the
    file read).

    Raises:
        EndpointRegistryError: no default declared, or an unknown id.
    """
    reg = registry if registry is not None else load_endpoint_registry()
    if name is None:
        if not reg.default_id:
            known = ", ".join(sorted(reg.entries)) or "<none>"
            raise EndpointRegistryError(
                "the model-endpoints registry declares no 'default' entry "
                f"(known entries: {known})"
            )
        entry = reg.entries[reg.default_id]
        if entry.kind == HARNESS_KIND:
            raise EndpointRegistryError(harness_entry_message(entry.id, entry.harness))
        return entry
    entry = reg.entries.get(name)
    if entry is None:
        known = ", ".join(sorted(reg.entries)) or "<none>"
        raise EndpointRegistryError(
            f"unknown model-endpoint entry '{name}' (known: {known})"
        )
    return entry


__all__ = [
    "REGISTRY_ENV",
    "harness_entry_message",
    "REGISTRY_RELATIVE_PATH",
    "TRANSPORT_KIND",
    "HARNESS_KIND",
    "default_registry_path",
    "EndpointEntry",
    "EndpointRegistry",
    "EndpointRegistryError",
    "EndpointMetadataError",
    "parse_classification_fields",
    "load_endpoint_registry",
    "resolve_registry_entry",
]
