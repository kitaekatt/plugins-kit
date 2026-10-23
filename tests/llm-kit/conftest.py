"""Fixtures for llm-kit tests (importable package: llm_kit)."""

import os
import sys

# Entry points that call bootstrap_guard.reexec_under_plugin_venv() would
# os.execv the pytest process into the plugin venv on a provisioned machine and
# yield a false green. The skip variable makes the re-exec a no-op under tests;
# see plugins/CLAUDE.md, "Test gotcha".
os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "llm-kit")
)

# The katsoft-llm-kit source root for `llm_kit.*` (the plugin holds no library
# source). Do NOT add `scripts/`: it holds the repoint generator, which is loaded
# by path, never imported by name.
_LIB = os.path.normpath(
    os.path.join(PLUGIN_ROOT, os.pardir, os.pardir, "packages", "katsoft-llm-kit", "src")
)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

# tests/llm-kit/ itself, for `import repoint` from the test modules.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_ambient_user_state(monkeypatch):
    """No test reads the developer's own neutral user files through an ambient variable.

    The neutral directory follows ``XDG_CONFIG_HOME`` and three variables replace
    a default file; a value left in the environment would silently steer every
    test that does not set its own HOME. Tests that need one set it themselves.
    """
    for name in ("XDG_CONFIG_HOME", "LLM_KIT_CONSENT_FILE", "LLM_KIT_ENV_FILE", "MODEL_ENDPOINTS_REGISTRY"):
        monkeypatch.delenv(name, raising=False)
