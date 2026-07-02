"""Shared fixtures for plugins-kit test suite."""

import json
import os

import pytest

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "plugins", "bootstrap")
)

# Resolved at import time, BEFORE any test can monkeypatch HOME, so the guard
# below always targets the developer's REAL settings file regardless of what a
# test does to the environment.
_REAL_USER_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


@pytest.fixture(autouse=True)
def _guard_real_user_settings():
    """Regression guard: no test may mutate the real ~/.claude/settings.json.

    A bootstrap engine run that is NOT HOME-isolated discovers the developer's
    real ~/.claude/plugins/installed_plugins.json, iterates the enabled plugins,
    and runs claude-ui-kit's install_statusline.py against the real
    ~/.claude/settings.json -- rewriting its statusLine to a pytest temp path.
    This fixture snapshots the file around every test; if a test changed it, the
    fixture restores the original bytes and fails, so the leak is caught at its
    source instead of silently corrupting the user's live config.
    """
    try:
        before = open(_REAL_USER_SETTINGS, "rb").read()
    except OSError:
        before = None
    yield
    try:
        after = open(_REAL_USER_SETTINGS, "rb").read()
    except OSError:
        after = None
    if before != after:
        if before is not None:
            with open(_REAL_USER_SETTINGS, "wb") as f:
                f.write(before)
        pytest.fail(
            f"test mutated the real {_REAL_USER_SETTINGS} -- a bootstrap engine "
            f"run was not HOME-isolated and leaked into the developer's home "
            f"(restored). Isolate HOME for every engine invocation in this test."
        )


@pytest.fixture
def bootstrap_root():
    """Path to the bootstrap plugin root."""
    return BOOTSTRAP_ROOT


@pytest.fixture
def data_dir(tmp_path):
    """Temporary data directory for bootstrap operations."""
    d = tmp_path / "data"
    d.mkdir()
    return str(d)


@pytest.fixture
def defaults_dir():
    """Path to bootstrap defaults directory."""
    return os.path.join(BOOTSTRAP_ROOT, "defaults")


@pytest.fixture
def manifest_file(tmp_path):
    """Write a bootstrap.json manifest to a temp dir and return its path."""

    def _write(manifest: dict) -> str:
        path = tmp_path / "bootstrap.json"
        path.write_text(json.dumps(manifest))
        return str(path)

    return _write


@pytest.fixture
def fake_plugin_root(tmp_path):
    """Create a fake plugin root directory with optional bootstrap.json."""

    def _create(name="test-plugin", manifest=None):
        root = tmp_path / "plugins" / name
        root.mkdir(parents=True)
        if manifest is not None:
            (root / "bootstrap.json").write_text(json.dumps(manifest))
        return str(root)

    return _create


@pytest.fixture
def fake_registry(tmp_path):
    """Create a fake installed_plugins.json registry."""

    def _create(plugins_dict):
        registry_path = tmp_path / "plugins" / "installed_plugins.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps({"plugins": plugins_dict}))
        return str(registry_path)

    return _create
