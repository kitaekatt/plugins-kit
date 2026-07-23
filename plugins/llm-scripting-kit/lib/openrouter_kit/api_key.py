"""Resolve an endpoint's API key from the canonical sources.

Precedence (highest to lowest), for the endpoint's ``key_env`` variable:

1. the ``<key_env>`` environment variable
2. ``<project_root>/.local-data/openrouter-kit/.env`` (per-project override)
3. ``~/.claude/plugins/data/plugins-kit/openrouter-kit/.env`` (user default)

The default endpoint (``openrouter``) uses ``OPENROUTER_API_KEY``; a named
endpoint uses its own ``key_env``. Keys for multiple endpoints coexist as
separate ``KEY=VALUE`` lines in the same ``.env`` file.

``get_api_key`` returns a small dataclass that records both the key value
and where it was sourced from, so consumers can log the source path on
startup for debugging credential confusion.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .constants import API_KEY_ENV, USER_ENV_FILE, project_env_file
from .env_file import read_env_file


@dataclass(frozen=True)
class KeyLookupResult:
    """Result of an API key lookup.

    ``source`` is one of ``"env"``, ``"project"``, ``"user"``, or
    ``"missing"``. When ``key`` is ``None``, ``source`` is always
    ``"missing"`` and ``source_path`` is ``None``.
    """

    key: Optional[str]
    source: str
    source_path: Optional[Path]


def _resolve_key(key_env: str, project_root: Optional[Path]) -> KeyLookupResult:
    """Resolve a specific ``key_env`` variable through the standard layers."""
    env_value = os.environ.get(key_env)
    if env_value:
        return KeyLookupResult(key=env_value, source="env", source_path=None)

    root = Path(project_root) if project_root is not None else Path.cwd()
    project_file = project_env_file(root)
    project_values = read_env_file(project_file)
    project_key = project_values.get(key_env)
    if project_key:
        return KeyLookupResult(key=project_key, source="project", source_path=project_file)

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
