"""Fixtures for llm-scripting-kit tests (importable package: llm_scripting_kit)."""

import os
import sys

import pytest

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "llm-scripting-kit")
)

# Make `lib/` importable as `llm_scripting_kit.*` and the plugin root importable
# for `custom_bootstrap`.
# lib/ for `llm_scripting_kit.*` package imports; PLUGIN_ROOT for `custom_bootstrap`.
# Do NOT add `scripts/` -- the CLI file shadows the `llm_scripting_kit` package
# name during test collection.
lib_path = os.path.join(PLUGIN_ROOT, "lib")
for p in (lib_path, PLUGIN_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def plugin_root():
    """Path to the llm-scripting-kit plugin."""
    return PLUGIN_ROOT
