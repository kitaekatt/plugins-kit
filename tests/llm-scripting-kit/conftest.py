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


@pytest.fixture(autouse=True)
def _isolated_host_state(tmp_path, monkeypatch):
    """Never let a test resolve to this host's real fleet-private files.

    HOME/USERPROFILE point at a fresh per-test temp directory, so the
    conventional ``~/.claude/config/model-endpoints.yaml`` and
    ``~/.claude/config/llm-scripting-kit.yaml`` locations (and the plugin data
    dir's ``.env`` / ``config.yaml``) resolve inside the sandbox rather than
    into a developer's real profile. Nothing exists there by default, which
    reproduces today's "absent registry / absent config" behavior exactly --
    an EXPLICIT ``MODEL_ENDPOINTS_REGISTRY`` override naming a missing file is
    loud by design (see ``model_endpoints.load_endpoint_registry``), so this
    fixture clears any such override inherited from the invoking shell rather
    than setting one of its own; a dangling override here would turn every
    test that does not care about the registry into a failure.

    A test that needs different behavior sets its own HOME / USERPROFILE /
    MODEL_ENDPOINTS_REGISTRY (via ``monkeypatch.setenv`` or ``delenv``) in its
    own body or its own fixture, which runs after this one and wins -- this
    fixture is a baseline, not an override any owning test cannot lift.
    """
    home = tmp_path / "_isolated_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MODEL_ENDPOINTS_REGISTRY", raising=False)
