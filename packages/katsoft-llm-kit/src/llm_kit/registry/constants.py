"""Default-endpoint values and canonical credential file locations.

``BASE_URL`` / ``API_KEY_ENV`` are the built-in default endpoint's
(``openrouter``) values, re-exported here as stable aliases so existing
endpoint-less callers keep resolving OpenRouter exactly as before.
"""

import os
import sys
from pathlib import Path

from .user_paths import neutral_project_env_file

# Default endpoint (``openrouter``) values. Aliases of
# DEFAULT_MODEL_CONFIG["endpoints"]["openrouter"] in models.py.
BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"

# LEGACY user-scoped credential file, read only when the neutral file is absent.
# Bootstrap-engine plugin data dirs follow the layout
# ``~/.claude/plugins/data/<marketplace>/<plugin>/``, so the namespace is the
# plugin name, ``llm-scripting-kit``. Keys for multiple endpoints coexist as
# separate lines in a key file.
#
# The neutral user key file is ``$XDG_CONFIG_HOME/llm-kit/.env`` (see
# :mod:`llm_kit.registry.user_paths`), and ``$LLM_KIT_ENV_FILE`` replaces the
# whole user layer with one explicit file. ``get_api_key`` reads the override or
# the neutral file first and falls back to USER_ENV_FILE only when neither
# exists; ``account set-key`` writes the override or the neutral file, never
# this one. The per-project files are listed by :func:`project_env_files`.
USER_ENV_FILE = (
    Path.home() / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-scripting-kit" / ".env"
)

# The CLI resolution contract, as the literal ``~/``-prefixed display string an
# agent-facing message tells a reader to run (USER_ENV_FILE above is a resolved
# Path for code; this is text, so the two are deliberately separate). The
# version-free plugin-venv console script needs no PATH entry or env var and
# resolves from any shell; a bare ``llm-kit`` resolves only inside a Claude
# Code session. The ``plugins-kit`` marketplace segment is part of the
# contract.
#
# This is the one value in the module that names the llm-kit plugin rather than
# the legacy one (design: "Persisted-state compatibility"): the credential and
# config paths above and below keep the legacy ``llm-scripting-kit`` namespace
# until the old plugin is deleted, but the command an agent is told to run is
# the llm-kit venv and executable.
_CLI_LEAF = "Scripts/llm-kit.exe" if sys.platform == "win32" else "bin/llm-kit"
CLI_COMMAND = "~/.claude/plugins/data/plugins-kit/llm-kit/.venv/" + _CLI_LEAF

# The same console script, spelled against the redirectable data root. A session
# that redirects bootstrap's data tree (scripts/claude_plugin_test.py exports
# CLAUDE_BOOTSTRAP_DATA_ROOT) provisions this plugin's dev venv there, so the
# production path in CLI_COMMAND would run the installed release instead of the
# working copy. The form is the one the skill text uses; a reader expands it in
# a shell.
REDIRECTED_CLI_COMMAND = (
    '"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"'
    "/plugins-kit/llm-kit/.venv/" + _CLI_LEAF
)


def cli_command() -> str:
    """The console-script command an agent-facing message tells a reader to run.

    :data:`REDIRECTED_CLI_COMMAND` when ``CLAUDE_BOOTSTRAP_DATA_ROOT`` is set
    (a redirected session), otherwise the human contract :data:`CLI_COMMAND`.
    Evaluated per call, because the redirect is an environment fact of the
    running process rather than of the module's import.
    """
    if os.environ.get("CLAUDE_BOOTSTRAP_DATA_ROOT"):
        return REDIRECTED_CLI_COMMAND
    return CLI_COMMAND


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
    """Both plugin-era project-layer key locations, HIGHEST precedence first."""
    return [project_env_file(project_root), legacy_project_env_file(project_root)]


def layered_project_env_files(project_root: Path) -> list:
    """Every project-layer key location a lookup reads, HIGHEST precedence first.

    ``<project_root>/.llm-kit/.env`` (neutral), then :func:`project_env_files`.
    """
    return [neutral_project_env_file(project_root), *project_env_files(project_root)]
