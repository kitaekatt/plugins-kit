"""Invariant test: a plugin that ships a bootstrap.json must declare the
bootstrap plugin in its plugin.json dependencies (bare string, per CLAUDE.md
"Plugin dependencies on bootstrap").

The rule itself lives in scripts/check_bootstrap_dependency.py and is loaded
from there rather than reimplemented, because that script is what the
pre-commit hook runs -- a second copy here could pass while the gate that
actually blocks the commit disagreed. This test is the spec; the hook is the
enforcement (see the script's header for why suite-only invariants lose).
"""

import importlib.util
import json
from pathlib import Path

_SCRIPT = (Path(__file__).resolve().parents[2] / "scripts"
           / "check_bootstrap_dependency.py")


def _load_checker():
    spec = importlib.util.spec_from_file_location(
        "check_bootstrap_dependency", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _plugin(root, name, *, bootstrap_json=True, deps=None, plugin_json=True):
    d = root / name
    (d / ".claude-plugin").mkdir(parents=True)
    if bootstrap_json:
        (d / "bootstrap.json").write_text("{}")
    if plugin_json:
        manifest = {"name": name, "version": "0.1.0"}
        if deps is not None:
            manifest["dependencies"] = deps
        (d / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))
    return d


def test_real_tree_has_no_outliers():
    assert _checker.find_outliers() == []


def test_real_tree_discovery_is_not_vacuous():
    # The clean result above must come from actually checking plugins.
    with_manifest = [
        d for d in _checker.PLUGINS_DIR.iterdir()
        if (d / "bootstrap.json").is_file()
    ]
    assert len(with_manifest) >= 5


def test_missing_dependency_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit", deps=[])
    (out,) = _checker.find_outliers(tmp_path)
    assert "some-kit" in out and "does not declare" in out


def test_absent_dependencies_field_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit")
    assert len(_checker.find_outliers(tmp_path)) == 1


def test_bare_string_dependency_passes(tmp_path):
    _plugin(tmp_path, "some-kit", deps=["bootstrap"])
    assert _checker.find_outliers(tmp_path) == []


def test_bootstrap_itself_is_exempt(tmp_path):
    _plugin(tmp_path, "bootstrap")
    assert _checker.find_outliers(tmp_path) == []


def test_plugin_without_bootstrap_json_is_out_of_scope(tmp_path):
    _plugin(tmp_path, "cache-kit-like", bootstrap_json=False)
    assert _checker.find_outliers(tmp_path) == []


def test_missing_plugin_json_is_an_outlier(tmp_path):
    _plugin(tmp_path, "some-kit", plugin_json=False)
    (out,) = _checker.find_outliers(tmp_path)
    assert "no .claude-plugin/plugin.json" in out
