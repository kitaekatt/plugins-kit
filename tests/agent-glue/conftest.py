"""Shared setup for the agent-glue test suite (repo convention: tests live in
tests/<plugin>/, because the plugin dir is the publish unit).

agent-glue is dev-only (published: false) and its dependencies (pydantic,
jinja2, jsonschema) are not part of the repo-root dev extra. When they are not
importable, the whole suite is skipped at collection time instead of erroring
the full-suite run. To run these tests, install the plugin's deps, e.g.:

    uv run --project plugins/agent-glue --extra dev pytest tests/agent-glue/ -v
"""

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "plugins" / "agent-glue"
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_REQUIRED = ("pydantic", "yaml", "jinja2", "jsonschema")
_MISSING = [m for m in _REQUIRED if importlib.util.find_spec(m) is None]

# Skip collection of the test modules (they import agent_glue_lib, which
# hard-imports the missing packages) rather than failing the full suite.
collect_ignore_glob = ["core/*"] if _MISSING else []
