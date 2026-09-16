"""Shared loader for hue-kit's hyphenated scripts.

scene-layers.py imports requests/urllib3 at module scope for bridge I/O, and
neither is a plugins-kit test dependency (they are provisioned into the plugin's
own venv by bootstrap). The functions under test here are pure -- colour/scene
diffing and hashing, no network -- so the import is satisfied with minimal
stubs rather than by pulling HTTP libraries into the test environment.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = (Path(__file__).resolve().parent.parent.parent
            / "plugins" / "hue-kit" / "scripts")


@pytest.fixture(autouse=True)
def _pin_bridge_env(monkeypatch):
    """M25 hygiene: pin HUE_BRIDGE_IP to a TEST-NET address (RFC 5737,
    192.0.2.0/24 -- reserved for documentation, never routable) for every
    test in this package, via monkeypatch so it is restored after EACH test
    instead of leaking into the rest of the suite. Function-scoped and
    autouse: it still covers tests that call hue_cli.__wrapped__() directly
    (bypassing pytest's fixture request), since autouse fixtures apply to
    every test collected in this directory regardless of what it requests."""
    monkeypatch.setenv("HUE_BRIDGE_IP", "192.0.2.1")


@pytest.fixture(autouse=True, scope="package")
def _isolate_sys_state():
    """M25 hygiene: the fixtures below load two files by path, which mutates
    global interpreter state to do it -- hue_kit_cli.py's own module-level
    `sys.path.insert(0, ...)`, plus sys.modules entries for both loaded
    modules and their requests/urllib3/bootstrap_guard stubs. Restore both at
    the end of THIS package (tests/hue-kit has an __init__.py, so a
    scope="package" fixture is exactly one package), not the whole session --
    a session-wide restore did nothing between tests/hue-kit and whatever
    test directory ran next, which is where the leak actually bit."""
    previous_path = list(sys.path)
    previous_modules = set(sys.modules.keys())
    yield
    sys.path[:] = previous_path
    for name in set(sys.modules.keys()) - previous_modules:
        sys.modules.pop(name, None)


def _install_bridge_io_stubs() -> None:
    """Satisfy `import requests` / `import urllib3` without the real packages."""
    if "requests" not in sys.modules:
        requests = types.ModuleType("requests")
        requests.Session = object
        sys.modules["requests"] = requests
    if "urllib3" not in sys.modules:
        urllib3 = types.ModuleType("urllib3")
        urllib3.exceptions = types.SimpleNamespace(InsecureRequestWarning=Warning)
        urllib3.disable_warnings = lambda *a, **k: None
        sys.modules["urllib3"] = urllib3


@pytest.fixture(scope="package")
def scene_layers():
    """The scene-layers.py module, loaded by path (the filename is hyphenated
    and intentionally so -- scene-meta-groups.py is located by PATH at runtime)."""
    _install_bridge_io_stubs()
    path = _SCRIPTS / "scene-layers.py"
    spec = importlib.util.spec_from_file_location("hue_scene_layers", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hue_scene_layers"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="package")
def hue_cli():
    """The hue_kit_cli.py module, loaded with its bootstrap guard neutralised.

    hue_kit_cli imports bootstrap_guard and CALLS reexec_under_plugin_venv at
    module scope, so importing it in-process would try to re-exec the test
    runner under the plugin venv. Stub the module out first -- same technique
    conftest already uses for requests/urllib3, and for the same reason: the
    function under test is pure process-plumbing and needs none of it. Restore
    the prior guard immediately after loading; the CLI retains its bound stubs.

    Fixture hygiene (M25): the loaded module's `_resolve_bridge_ip()` reads
    HUE_BRIDGE_IP -- pinned by the autouse `_pin_bridge_env` fixture above,
    not here, so it is restored after each test rather than leaking into the
    rest of the suite. BRIDGE_IP_CACHE / PAIRED_KEY_FILE are left as the
    module computes them (under `data_dir("hue-kit")`, stubbed to
    Path("/nonexistent") below -- test_cli_loader_isolation.py asserts that
    literal path against the undecorated fixture function): neither resolves
    to an existing file, so `_resolve_bridge_ip()` never reads or writes a
    real cache/key file. A test that needs a writable path repoints these two
    attributes itself via `monkeypatch.setattr(hue_cli, ..., tmp_path / ...)`
    -- never `tempfile.mkdtemp`, which leaves a directory behind."""
    _install_bridge_io_stubs()
    guard = types.ModuleType("bootstrap_guard")
    guard.require_bootstrap = lambda *a, **k: None
    guard.reexec_under_plugin_venv = lambda *a, **k: None
    guard.data_dir = lambda *a, **k: Path("/nonexistent")
    missing = object()
    previous_guard = sys.modules.get("bootstrap_guard", missing)
    sys.modules["bootstrap_guard"] = guard
    try:
        path = _SCRIPTS / "hue_kit_cli.py"
        spec = importlib.util.spec_from_file_location("hue_kit_cli_undertest", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["hue_kit_cli_undertest"] = module
        spec.loader.exec_module(module)
    finally:
        if previous_guard is missing:
            sys.modules.pop("bootstrap_guard", None)
        else:
            sys.modules["bootstrap_guard"] = previous_guard
    return module
