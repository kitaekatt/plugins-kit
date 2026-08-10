"""Fixtures for p4-kit tests."""

import os
import sys

import pytest

# prepare_review.py calls reexec_under_plugin_venv("p4-kit") at import time,
# which os.execv's away the pytest process itself and yields a false green: a
# bare `pytest tests/p4-kit` printed NOTHING and exited 0, having run zero of
# its 164 tests. It only appeared to work in a full-suite run because
# tests/awesome-kit/conftest.py is collected first and happens to set this var.
# Mirrors awesome-kit / git-kit / unreal-kit; see plugins/CLAUDE.md.
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "p4-kit")
)

scripts_path = os.path.join(PLUGIN_ROOT, "scripts")
for p in (scripts_path, PLUGIN_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def plugin_root():
    """Path to the p4-kit plugin."""
    return PLUGIN_ROOT
