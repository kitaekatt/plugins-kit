"""Fixtures for yaml-data-editor-kit tests.

The fixture corpus deliberately uses a neutral catalogue vocabulary (product,
category, tier, label, measure). The plugin must learn no consuming project's
nouns, and a test fixture is as much a place for that to leak as the code is.
"""

import sys
from pathlib import Path
from typing import Callable

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "plugins" / "yaml-data-editor-kit"

# Make `lib/` importable as `yaml_data_editor_kit.*`.
_LIB = str(PLUGIN_ROOT / "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)


# The signature of the `write` fixture, named once so every test module can
# annotate the parameter it receives.
Writer = Callable[[str, str], Path]


@pytest.fixture
def plugin_root() -> Path:
    """Path to the yaml-data-editor-kit plugin."""
    return PLUGIN_ROOT


@pytest.fixture
def write(tmp_path: Path) -> Writer:
    """Write one text file under tmp_path, creating parents. Returns its path."""

    def _write(relative: str, text: str) -> Path:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    """Where a test's profile documents live, separate from its corpus."""
    path = tmp_path / "profile"
    path.mkdir(exist_ok=True)
    return path
