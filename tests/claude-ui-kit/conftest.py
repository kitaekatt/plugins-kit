"""Fixtures for claude-ui-kit tests."""

import os
import sys

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "claude-ui-kit")
)

# Make scripts/ importable for `import install_statusline`.
scripts_path = os.path.join(PLUGIN_ROOT, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)
