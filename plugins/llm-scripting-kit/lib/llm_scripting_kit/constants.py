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
# ``<project_root>/.local-data/llm-scripting-kit/.env``; ``get_api_key`` checks
# the project file first and only falls back to USER_ENV_FILE.
USER_ENV_FILE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / ".env"
)


def project_env_file(project_root: Path) -> Path:
    """Per-project override location.

    Layered after env vars and before the user-scoped file in
    ``get_api_key`` precedence.
    """
    return Path(project_root) / ".local-data" / "llm-scripting-kit" / ".env"
