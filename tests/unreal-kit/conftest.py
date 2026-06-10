"""Fixtures and environment guards for unreal-kit tests.

Several unreal-kit host-side scripts call
``bootstrap_guard.reexec_under_plugin_venv("unreal-kit")`` at module top
(see plugins/CLAUDE.md "Shared-lib scripts must re-exec under the plugin
venv"). Importing those modules under pytest must NOT re-exec the test
process into the plugin venv, so set the loop-guard env var before any
test module imports them. On macOS/Linux the re-exec often happens to
no-op anyway (both venvs symlink the same uv-managed CPython, so
``Path(sys.executable).resolve()`` matches), but on Windows venvs copy
python.exe and the resolve check genuinely differs -- without this guard
the test run would execv away.
"""

import os

os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")
