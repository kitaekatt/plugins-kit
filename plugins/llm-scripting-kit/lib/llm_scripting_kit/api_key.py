"""Resolve an endpoint's API key from the canonical sources.

Precedence (highest to lowest), for the endpoint's ``key_env`` variable:

1. the ``<key_env>`` environment variable
2. ``<project_root>/.local-data/plugins-kit/llm-scripting-kit/.env``
   (per-project override -- canonical)
3. ``<project_root>/.local-data/llm-scripting-kit/.env`` (the superseded
   project location, still read; see below)
4. ``~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`` (user default)
5. the endpoint's configured ``key_file`` (a path whose entire, stripped
   content is the key), if the endpoint's config declares one

Layer 5 is the escape hatch for a credential that already exists as a
bare-value file -- e.g. one materialized by secrets-kit -- so it does not also
need to be copied into a ``.env``. It sits at the bottom deliberately: every
higher layer is something the user set explicitly (an env var, or a ``.env``
they wrote), and those must keep winning so adding this layer cannot change
what any already-working machine resolves. Because it is consulted last, it is
looked up lazily -- only once layers 1-4 have all missed -- so a normal
resolution never pays the cost of loading the endpoint's config. See
``_read_key_file`` and ``_default_endpoint_key_file``.

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
from typing import Callable, Optional

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

    ``source`` is one of ``"env"``, ``"project"``, ``"user"``, ``"key_file"``,
    or ``"missing"``. When ``key`` is ``None``, ``source`` is always
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


def _read_key_file(key_file: str) -> Optional[KeyLookupResult]:
    """Read a bare-value key file: the whole, stripped content is the key.

    Returns ``None`` -- never raises -- when the file is missing, unreadable,
    or empty after stripping, so a keyless endpoint (no ``key_file``
    configured, or one pointing nowhere) stays a supported configuration and
    every other action in a consuming skill keeps working without a key.

    Multi-line content also resolves to ``None``: a bare-value file holds one
    value, and CR/LF would reach an Authorization header.

    An endpoint declaring ``key_env: null`` is KEYLESS, and ``key_file`` is
    inert for it -- callers short-circuit before this layer is reached.
    """
    path = Path(key_file).expanduser()
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError: a key_file that
        # is binary or not UTF-8 is unreadable in the sense this contract
        # means, and must resolve to no key rather than raise.
        return None
    content = content.strip()
    if not content:
        return None
    if "\n" in content or "\r" in content:
        # A bare-value credential file holds exactly one value. Multi-line
        # content means the path names something else (a .env, a PEM, a file
        # holding two keys); using it would embed CR/LF in an Authorization
        # header. Resolve to no key rather than send that.
        return None
    return KeyLookupResult(key=content, source="key_file", source_path=path)


def _default_endpoint_key_file(project_root: Optional[Path]) -> Optional[str]:
    """Lazily resolve ``key_file`` for the unnamed (default) endpoint.

    Called only when layers 1-4 already missed, so the endpoint=None fast
    path stays config-free for the common case -- a key that already
    resolves. Never raises: a config that fails to resolve here simply means
    no key_file layer, not a lookup failure.
    """
    try:
        # Imported inside the try, not above it. This function's contract is
        # "never raises", and a consuming plugin's venv need not carry every
        # optional dependency the model layer might one day import -- git-kit
        # and p4-kit deliberately omit openai. An ImportError here means no
        # key_file layer, exactly like any other config failure.
        from .models import resolve_endpoint  # noqa: PLC0415

        resolved = resolve_endpoint(
            None, project_root=str(project_root) if project_root is not None else None
        )
    except Exception:
        # Deliberately broad. Any config failure means "no key_file layer",
        # never a lookup failure: the endpoint=None path did not load config
        # before this layer existed, so a malformed config must keep
        # resolving to source="missing" exactly as it did.
        return None
    # IDENTITY GUARD. For endpoint=None, layers 1-4 resolve the hardcoded
    # API_KEY_ENV, while resolve_endpoint(None) follows the config's
    # `default_endpoint:`. If those disagree, returning this endpoint's
    # key_file would hand one provider's credential back labelled as
    # another's -- and make_openai_client(endpoint=None) pins the base_url to
    # the default endpoint, so it would be transmitted to the wrong host.
    # Only answer when the two identities agree.
    if resolved.get("key_env") != API_KEY_ENV:
        return None
    key_file = resolved.get("key_file")
    return key_file if isinstance(key_file, str) and key_file.strip() else None


def _resolve_key(
    key_env: str,
    project_root: Optional[Path],
    key_file_provider: Optional[Callable[[], Optional[str]]] = None,
) -> KeyLookupResult:
    """Resolve a specific ``key_env`` variable through the standard layers.

    ``key_file_provider``, when given, is called ONLY if layers 1-4 all miss
    -- it names layer 5, the endpoint's configured ``key_file``. Keeping it
    lazy means a resolution that already succeeds on a higher layer never
    pays for loading the endpoint's config, and guarantees this new layer
    cannot change the result on any machine where a higher layer already
    resolves a key.
    """
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

    if key_file_provider is not None:
        key_file = key_file_provider()
        if key_file:
            file_result = _read_key_file(key_file)
            if file_result is not None:
                return file_result

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
        # Lazy: only loads config if layers 1-4 miss (see
        # _default_endpoint_key_file), so the fast path stays config-free.
        key_file_provider = lambda: _default_endpoint_key_file(project_root)  # noqa: E731
    else:
        # Named endpoint: consult the config for its key_env. Imported lazily so
        # the default path stays config-free (and to avoid an import cycle).
        from .models import resolve_endpoint  # noqa: PLC0415

        resolved = resolve_endpoint(
            endpoint, project_root=str(project_root) if project_root is not None else None
        )
        key_env = resolved["key_env"]
        resolved_key_file = resolved.get("key_file")
        key_file_provider = lambda: resolved_key_file  # noqa: E731
    return _resolve_key(key_env, project_root, key_file_provider)
