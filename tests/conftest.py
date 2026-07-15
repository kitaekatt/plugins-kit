"""Shared fixtures for plugins-kit test suite."""

import json
import os
import sys

import pytest

BOOTSTRAP_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "plugins", "bootstrap")
)

# Resolved at import time, BEFORE any test can monkeypatch HOME, so the guard
# below always targets the developer's REAL settings file regardless of what a
# test does to the environment.
_REAL_USER_SETTINGS = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

# Bound at import time for the same reason, one layer deeper: tests swap
# sys.modules["winreg"] for a fake, and they do it with the SHARED monkeypatch
# fixture -- whose undo runs after the guard's teardown, not before. A plain
# `import winreg` in the guard would therefore get the test's fake and explode
# on its signature. Holding the real module here is immune to that.
try:
    import winreg as _real_winreg
except ImportError:  # non-Windows
    _real_winreg = None


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


def _read_windows_user_path():
    """(value, type) of HKCU\\Environment Path, or None off Windows / unset."""
    if sys.platform != "win32" or _real_winreg is None:
        return None
    try:
        with _real_winreg.OpenKey(
            _real_winreg.HKEY_CURRENT_USER, "Environment"
        ) as key:
            return _real_winreg.QueryValueEx(key, "Path")
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _guard_real_user_path(monkeypatch):
    """Regression guard: no test may mutate the real Windows User PATH.

    The registry is GLOBAL state -- it ignores the HOME/USERPROFILE redirection
    every other isolation in this suite relies on. So an engine run under a tmp
    HOME still resolves the REAL HKCU\\Environment, and check_path_entry happily
    appends `<tmp_home>\\.local\\share\\python-standalone\\python` to the
    developer's actual PATH. Permanently. Once per run. Those entries are unique
    (a fresh tmp dir each time), so nothing ever collapses them -- one machine
    had accumulated 27 dead pytest paths before this guard existed.

    Two layers, because the first one is only as good as the person who
    remembers it:

      * BOOTSTRAP_SKIP_REGISTRY is set for EVERY test (default-deny). The
        writers honor it; the handful of tests that exercise the write path
        delenv it themselves and mock winreg, which still works.
      * The snapshot below is the backstop that makes THIS bug structurally
        unable to recur: it catches a writer that ignores the env var, or a test
        that opts out and forgets to mock winreg. On a leak it restores the
        original value and fails at the source, rather than letting the damage
        silently outlive the run.

    Scope, stated plainly so nobody infers more than it delivers: the snapshot
    watches HKCU\\Environment "Path" and nothing else. font_check writes
    HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts and consults no
    env var, so NEITHER layer covers it -- it is simply not exercised against
    the real registry today (its win32 path is skipped, and install_font's tests
    stub register_fonts out). Widen the snapshot if that ever changes.

    Mirrors _guard_real_user_settings; see its docstring for the same shape
    applied to ~/.claude/settings.json.
    """
    monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
    before = _read_windows_user_path()
    yield
    after = _read_windows_user_path()
    if before == after:
        return
    with _real_winreg.OpenKey(
        _real_winreg.HKEY_CURRENT_USER, "Environment", 0,
        _real_winreg.KEY_READ | _real_winreg.KEY_WRITE,
    ) as key:
        if before is None:
            # There was no Path value at all and the test CREATED one --
            # _add_path_to_windows_registry has its own FileNotFoundError
            # fallback that does exactly that. Restoring here means DELETING
            # it: writing back an empty string would leave behind a value that
            # never existed, which is its own (quieter) form of the leak.
            _real_winreg.DeleteValue(key, "Path")
        else:
            value, value_type = before
            _real_winreg.SetValueEx(key, "Path", 0, value_type, value)
    pytest.fail(
        "test mutated the real Windows User PATH (HKCU\\Environment) -- the "
        "registry ignores HOME isolation, so this leaks a permanent, "
        "never-deduplicated entry into the developer's PATH (restored). Set "
        "BOOTSTRAP_SKIP_REGISTRY for any engine invocation in this test, or "
        "mock winreg if the test is exercising the write path itself."
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
