"""Fixtures for content-pipeline-kit tests."""

import os
import sys

import pytest

PLUGIN_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "plugins", "content-pipeline-kit")
)

# Make `lib/` importable as `content_pipeline.*`.
lib_path = os.path.join(PLUGIN_ROOT, "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)


@pytest.fixture
def plugin_root():
    """Path to the content-pipeline-kit plugin."""
    return PLUGIN_ROOT
