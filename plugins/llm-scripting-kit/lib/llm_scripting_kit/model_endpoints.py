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
  :class:`EndpointRegistryError` naming the path and the defect. A present
  registry that cannot be read must never read as a silent empty.

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


def default_registry_path() -> Path:
    """The conventional registry path for the current user.

    Resolved per call rather than bound at import time: the home directory is
    a runtime fact (and tests redirect it), so a module constant would freeze
    whichever home happened to exist when the module was first imported.
    """
    return Path.home() / REGISTRY_RELATIVE_PATH


class EndpointRegistryError(Exception):
    """The registry exists but could not be read, or an entry was not found."""


@dataclass(frozen=True)
class EndpointEntry:
    """One declared endpoint. ``key_env`` None means keyless."""

    id: str
    base_url: str
    model: str
    name: Optional[str] = None
    context_window: Optional[int] = None
    reasoning_effort: Optional[str] = None
    key_env: Optional[str] = None


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
            or a file whose schema is invalid.
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
    for entry_id, raw in raw_models.items():
        key = str(entry_id)
        if not isinstance(raw, dict):
            raise EndpointRegistryError(
                f"model-endpoints registry '{path}': entry '{key}' is not a mapping"
            )
        entries[key] = EndpointEntry(
            id=key,
            base_url=_require_str(raw.get("base_url"), path=path, entry_id=key, key="base_url"),
            model=_require_str(raw.get("model"), path=path, entry_id=key, key="model"),
            name=_optional_str(raw.get("name"), path=path, entry_id=key, key="name"),
            context_window=_optional_int(
                raw.get("context_window"), path=path, entry_id=key, key="context_window"
            ),
            reasoning_effort=_optional_str(
                raw.get("reasoning_effort"), path=path, entry_id=key, key="reasoning_effort"
            ),
            key_env=_optional_str(raw.get("key_env"), path=path, entry_id=key, key="key_env"),
        )

    default_id = data.get("default")
    if default_id is not None:
        default_id = str(default_id)
        if default_id not in entries:
            known = ", ".join(sorted(entries)) or "<none>"
            raise EndpointRegistryError(
                f"model-endpoints registry '{path}': default '{default_id}' "
                f"names no entry (known: {known})"
            )

    return EndpointRegistry(default_id=default_id, entries=entries, path=path)


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
        return reg.entries[reg.default_id]
    entry = reg.entries.get(name)
    if entry is None:
        known = ", ".join(sorted(reg.entries)) or "<none>"
        raise EndpointRegistryError(
            f"unknown model-endpoint entry '{name}' (known: {known})"
        )
    return entry


__all__ = [
    "REGISTRY_ENV",
    "REGISTRY_RELATIVE_PATH",
    "default_registry_path",
    "EndpointEntry",
    "EndpointRegistry",
    "EndpointRegistryError",
    "load_endpoint_registry",
    "resolve_registry_entry",
]
