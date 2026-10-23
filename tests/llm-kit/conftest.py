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
