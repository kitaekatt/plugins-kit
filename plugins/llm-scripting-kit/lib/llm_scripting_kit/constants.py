"""Default-endpoint values and canonical credential file locations.

``BASE_URL`` / ``API_KEY_ENV`` are the built-in default endpoint's
(``openrouter``) values, re-exported here as stable aliases so existing
endpoint-less callers keep resolving OpenRouter exactly as before.
"""

from pathlib import Path

# Default endpoint (``openrouter``) values. Aliases of
# DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"] in models.py.
BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"

# User-scoped credential file. Bootstrap-engine plugin data dirs follow the
# layout ``~/.claude/plugins/data/<marketplace>/<plugin>/``, so the namespace is
# the plugin name, ``llm-scripting-kit``. Keys for multiple endpoints coexist as
# separate lines in this file.
#
# A developer who needs a different key per project can drop a file at
# ``<project_root>/.local-data/plugins-kit/llm-scripting-kit/.env``;
# ``get_api_key`` checks the project file first and only falls back to
# USER_ENV_FILE.
USER_ENV_FILE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / ".env"
)


def project_env_file(project_root: Path) -> Path:
    """Canonical per-project override location.

    ``<project_root>/.local-data/plugins-kit/llm-scripting-kit/.env`` -- the
    same ``<marketplace>/<plugin>`` namespacing bootstrap uses for the user
    layer (USER_ENV_FILE) and for every plugin's layered ``config.yaml``
    (``bootstrap_lib.config_resolve.standard_config_layers``). Key and config
    therefore resolve from the same project directory.

    Layered after env vars and before the user-scoped file in
    ``get_api_key`` precedence.
    """
    return Path(project_root) / ".local-data" / "plugins-kit" / "llm-scripting-kit" / ".env"


def legacy_project_env_file(project_root: Path) -> Path:
    """Superseded per-project location, kept readable so no existing file breaks.

    Through 0.6.5 the project key file lacked the ``plugins-kit`` marketplace
    segment, which made it asymmetric with both the user layer and the project
    config layer. ``get_api_key`` still reads this path, at LOWER precedence
    than :func:`project_env_file`, and flags the result
    (``KeyLookupResult.legacy_location``). Move the file up one namespace to
    silence the notice.
    """
    return Path(project_root) / ".local-data" / "llm-scripting-kit" / ".env"


def project_env_files(project_root: Path) -> list:
    """Both project-layer key locations, HIGHEST precedence first."""
    return [project_env_file(project_root), legacy_project_env_file(project_root)]
