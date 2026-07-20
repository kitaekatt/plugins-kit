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


@pytest.fixture(scope="session")
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
