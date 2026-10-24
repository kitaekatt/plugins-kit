"""Installed-package integration with the marketplace bootstrap source.

Off-fleet runtime and wheel identity tests live in katsoft-libs. These three
checks deliberately supply the actual plugin bootstrap implementation.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
import llm_kit

FLEET_LIB = Path(__file__).resolve().parents[2] / "plugins/bootstrap"

@pytest.fixture
def site():
    return Path(llm_kit.__file__).resolve().parent.parent

def package(site, body, *, fleet=False, env=None):
    prelude = "import sys, json\n"
    if fleet:
        prelude += f"sys.path.insert(0, {str(FLEET_LIB)!r})\n"
    else:
        prelude += "class Absent:\n    def find_spec(self, name, path=None, target=None):\n        if name == 'bootstrap_lib':\n            raise ModuleNotFoundError(name, name=name)\nsys.meta_path.insert(0, Absent())\n"
    child_env = dict(os.environ)
    for name in ("XDG_CONFIG_HOME", "MODEL_ENDPOINTS_REGISTRY", "LLM_KIT_ENV_FILE", "LLM_KIT_CONSENT_FILE"):
        child_env.pop(name, None)
    child_env.update(env or {})
    code = prelude + body + "\nprint(json.dumps({'ok': result, 'modules': [name for name in sys.modules if name.startswith('bootstrap_lib')]}))"
    result = subprocess.run([sys.executable, "-I", "-c", code], env=child_env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)

def test_fleet_is_true_when_bootstrap_lib_imports(site):
    out = package(site, (
        "from llm_kit import _runtime\n"
        "result = [_runtime.mode(), _runtime.fleet()]"
    ), fleet=True)
    assert out["ok"] == ["package", True]

def test_package_mode_fleet_allows_review_lane(site):
    """With bootstrap_lib importable, the gate that used to refuse in package mode passes."""
    out = package(site, (
        "from llm_kit import _runtime\n"
        "import llm_kit.dispatch.review_lane as lane\n"
        "_runtime.require_fleet('anything')\n"
        "result = [_runtime.mode(), lane.__name__]"
    ), fleet=True, env={"CLAUDE_PLUGIN_TEST": "1"})
    assert out["ok"] == ["package", "llm_kit.dispatch.review_lane"], out

def test_package_mode_fleet_uses_layered_registry(site, tmp_path):
    """With bootstrap_lib importable, package mode reads the fleet config layer."""
    home = tmp_path / "home"
    (home / ".claude" / "config").mkdir(parents=True)
    (home / ".claude" / "config" / "llm-scripting-kit.yaml").write_text(
        "default_endpoint: fleet-chosen\nendpoints:\n  fleet-chosen:\n    model: m\n",
        encoding="utf-8")
    body = (
        "from llm_kit.registry import models\n"
        "config = models.load_model_config()\n"
        "result = [config.get('default_endpoint'), 'fleet-chosen' in config['endpoints'],\n"
        "          config == models.DEFAULT_MODEL_CONFIG]"
    )
    fleet = package(site, body, fleet=True, env={"HOME": str(home)})
    assert fleet["ok"] == ["fleet-chosen", True, False], fleet
    off = package(site, body, env={"HOME": str(home)})
    # shipped defaults, layers ignored; the shipped baseline names no default_endpoint (no shipped default)
    assert off["ok"] == [None, False, True], off
    assert off["modules"] == []
