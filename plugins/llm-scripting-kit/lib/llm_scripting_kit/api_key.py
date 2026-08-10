"""Resolve an endpoint's API key from the canonical sources.

Precedence (highest to lowest), for the endpoint's ``key_env`` variable:

1. the ``<key_env>`` environment variable
2. ``<project_root>/.local-data/plugins-kit/llm-scripting-kit/.env``
   (per-project override -- canonical)
3. ``<project_root>/.local-data/llm-scripting-kit/.env`` (the superseded
   project location, still read; see below)
4. ``~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`` (user default)

Layers 2 and 3 are the same layer in two places. Through 0.6.5 only layer 3
existed, which left the key path asymmetric with the user layer and with the
project CONFIG layer (``.local-data/plugins-kit/llm-scripting-kit/config.yaml``,
bootstrap's generic ``<marketplace>/<plugin>`` project namespace) -- so a key
file placed at the config path, by analogy, was silently ignored. Layer 2 is
now canonical and matches config; layer 3 keeps every already-placed file
working. A key resolved from layer 3 sets ``KeyLookupResult.legacy_location``
and prints a one-time notice to stderr, so the mismatch is visible instead of
silent.

The default endpoint (``openrouter``) uses ``OPENROUTER_API_KEY``; a named
endpoint uses its own ``key_env``. Keys for multiple endpoints coexist as
separate ``KEY=VALUE`` lines in the same ``.env`` file.

``get_api_key`` returns a small dataclass that records both the key value
and where it was sourced from, so consumers can log the source path on
startup for debugging credential confusion.
"""

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import (
    API_KEY_ENV,
    USER_ENV_FILE,
    legacy_project_env_file,
    project_env_file,
)
from .env_file import read_env_file

# Paths already reported by _warn_legacy_location, so a process that resolves
# the key repeatedly emits the notice once rather than per call.
_LEGACY_NOTICE_SEEN = set()


@dataclass(frozen=True)
class KeyLookupResult:
    """Result of an API key lookup.

    ``source`` is one of ``"env"``, ``"project"``, ``"user"``, or
    ``"missing"``. When ``key`` is ``None``, ``source`` is always
    ``"missing"`` and ``source_path`` is ``None``.

    ``legacy_location`` is True only when ``source`` is ``"project"`` and the
    key came from the superseded project path that omits the ``plugins-kit``
    marketplace segment (see the module docstring). It is a signal, not an
    error -- the key resolved normally.
    """

    key: Optional[str]
    source: str
    source_path: Optional[Path]
    legacy_location: bool = False


def _warn_legacy_location(path: Path, canonical: Path) -> None:
    """Print the move-your-file notice once per path per process."""
    if path in _LEGACY_NOTICE_SEEN:
        return
    _LEGACY_NOTICE_SEEN.add(path)
    print(
        f"llm-scripting-kit: API key read from {path}, a superseded location. "
        f"The canonical project path is {canonical} -- the same "
        "<marketplace>/<plugin> namespace the project config.yaml uses. Move "
        "the file to silence this notice; the old path keeps working.",
        file=sys.stderr,
    )


def _resolve_key(key_env: str, project_root: Optional[Path]) -> KeyLookupResult:
    """Resolve a specific ``key_env`` variable through the standard layers."""
    env_value = os.environ.get(key_env)
    if env_value:
        return KeyLookupResult(key=env_value, source="env", source_path=None)

    root = Path(project_root) if project_root is not None else Path.cwd()
    project_file = project_env_file(root)
    project_key = read_env_file(project_file).get(key_env)
    if project_key:
        return KeyLookupResult(key=project_key, source="project", source_path=project_file)

    legacy_file = legacy_project_env_file(root)
    legacy_key = read_env_file(legacy_file).get(key_env)
    if legacy_key:
        _warn_legacy_location(legacy_file, project_file)
        return KeyLookupResult(
            key=legacy_key,
            source="project",
            source_path=legacy_file,
            legacy_location=True,
        )

    user_values = read_env_file(USER_ENV_FILE)
    user_key = user_values.get(key_env)
    if user_key:
        return KeyLookupResult(key=user_key, source="user", source_path=USER_ENV_FILE)

    return KeyLookupResult(key=None, source="missing", source_path=None)


def get_api_key(
    project_root: Optional[Path] = None,
    *,
    endpoint: Optional[str] = None,
) -> KeyLookupResult:
    """Resolve the API key for an endpoint.

    Args:
        project_root: Directory to check for a per-project ``.env`` override.
            Defaults to the current working directory when not provided.
        endpoint: Named endpoint whose ``key_env`` to resolve. ``None`` means
            the default endpoint (``openrouter``, ``OPENROUTER_API_KEY``) -- the
            fast path, identical to previous behavior with no config load.

    Returns:
        KeyLookupResult with the key, source label, and source path.
        ``key`` is ``None`` if no source has the key.
    """
    if endpoint is None:
        key_env = API_KEY_ENV
    else:
        # Named endpoint: consult the config for its key_env. Imported lazily so
        # the default path stays config-free (and to avoid an import cycle).
        from .models import resolve_endpoint  # noqa: PLC0415

        key_env = resolve_endpoint(
            endpoint, project_root=str(project_root) if project_root is not None else None
        )["key_env"]
    return _resolve_key(key_env, project_root)
