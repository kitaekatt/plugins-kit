"""Lazy-imported OpenAI SDK client pointed at an OpenAI-compatible endpoint.

The SDK is an optional dependency. Consumers that only need ``get_api_key``
or ``check_account`` do not pay the import cost; consumers that want a
ready-made Chat Completions client call ``make_openai_client``.
"""

from pathlib import Path
from typing import Any, Optional

from .api_key import get_api_key
from .constants import BASE_URL

# Placeholder sent to a KEYLESS endpoint. The OpenAI SDK requires a truthy
# api_key, while a keyless server ignores the Authorization header entirely, so
# a constant stand-in is the honest way to satisfy the SDK without inventing a
# credential the user has to manage.
KEYLESS_API_KEY = "keyless"


def make_openai_client(
    api_key: Optional[str] = None,
    *,
    project_root: Optional[Path] = None,
    endpoint: Optional[str] = None,
) -> Any:
    """Return an ``openai.OpenAI`` client configured for an endpoint.

    Args:
        api_key: Explicit key. When None, ``get_api_key`` is invoked to
            resolve from environment or .env files.
        project_root: Forwarded to ``get_api_key`` when ``api_key`` is None.
        endpoint: Named endpoint to target. ``None`` uses the default endpoint
            (``openrouter`` at ``BASE_URL``) -- identical to previous behavior.
            A KEYLESS endpoint (one resolving with ``key_env`` None) skips key
            resolution and passes ``KEYLESS_API_KEY``, which the server ignores.

    Returns:
        An ``openai.OpenAI`` instance with ``base_url`` set to the endpoint's
        Chat Completions endpoint.

    Raises:
        ImportError: If the ``openai`` package is not installed.
        RuntimeError: If no API key can be resolved from any source.
    """
    keyless = False
    if endpoint is None:
        base_url = BASE_URL
        key_env_hint = "OPENROUTER_API_KEY"
    else:
        from .models import resolve_endpoint  # noqa: PLC0415

        ep = resolve_endpoint(
            endpoint, project_root=str(project_root) if project_root is not None else None
        )
        base_url = ep["base_url"]
        key_env_hint = ep["key_env"]
        keyless = ep["key_env"] is None

    if api_key is None and keyless:
        # Keyless endpoint: never consult get_api_key -- there is no variable
        # to resolve, and asking would fail on a None key_env.
        api_key = KEYLESS_API_KEY

    if api_key is None:
        result = get_api_key(project_root, endpoint=endpoint)
        if result.key is None:
            raise RuntimeError(
                f"No API key found for endpoint '{endpoint or 'openrouter'}'. "
                f"Set {key_env_hint} or run `llm-scripting-kit set-key"
                + ("`." if endpoint is None else f" --endpoint {endpoint}`.")
            )
        api_key = result.key

    try:
        from openai import OpenAI  # noqa: PLC0415
    except ImportError as e:
        raise ImportError(
            "The 'openai' package is required for make_openai_client. "
            "Install it via `pip install openai` (or pull it as an extra "
            "via `llm-scripting-kit[sdk]`)."
        ) from e

    return OpenAI(api_key=api_key, base_url=base_url)
