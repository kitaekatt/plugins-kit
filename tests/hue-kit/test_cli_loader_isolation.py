"""The CLI loader's bootstrap stub must not affect subsequent imports."""

import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def fresh_hue_loader(monkeypatch):
    """Load the real conftest, isolating every import side effect of its CLI."""
    aliases = ("bootstrap_guard", "hue_kit_cli_undertest", "requests", "urllib3")
    previous = {name: sys.modules[name] for name in aliases if name in sys.modules}
    monkeypatch.setattr(sys, "path", sys.path.copy())
    path = Path(__file__).with_name("conftest.py")
    spec = importlib.util.spec_from_file_location("hue_loader_regression", path)
    assert spec is not None and spec.loader is not None
    loader = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(loader)
        yield loader.hue_cli.__wrapped__
    finally:
        for name in aliases:
            if name in previous:
                sys.modules[name] = previous[name]
            else:
                sys.modules.pop(name, None)


@pytest.mark.parametrize("previously_present", [True, False], ids=["prior-entry", "absent"])
def test_cli_load_restores_guard_immediately(fresh_hue_loader, previously_present):
    previous = types.ModuleType("previous_bootstrap_guard")
    if previously_present:
        sys.modules["bootstrap_guard"] = previous
    else:
        sys.modules.pop("bootstrap_guard", None)

    cli = fresh_hue_loader()

    if previously_present:
        assert sys.modules["bootstrap_guard"] is previous
    else:
        assert "bootstrap_guard" not in sys.modules
    # Restoring the import cache must retain the stub functions already bound
    # into the CLI, without attempting bootstrap or bridge I/O.
    assert cli.require_bootstrap("hue-kit") is None
    assert cli.reexec_under_plugin_venv("hue-kit") is None
    assert cli.data_dir("hue-kit") == Path("/nonexistent")
    assert cli.DEFAULT_WORKDIR == Path("/nonexistent")


@pytest.mark.parametrize("previously_present", [True, False], ids=["prior-entry", "absent"])
def test_cli_load_error_restores_guard_and_propagates(
        fresh_hue_loader, previously_present, monkeypatch):
    previous = types.ModuleType("previous_bootstrap_guard")
    if previously_present:
        sys.modules["bootstrap_guard"] = previous
    else:
        sys.modules.pop("bootstrap_guard", None)
    failure = RuntimeError("distinct CLI loader failure")
    original_exec = importlib.machinery.SourceFileLoader.exec_module

    def fail_cli_load(loader, module):
        if module.__name__ == "hue_kit_cli_undertest":
            guard = sys.modules["bootstrap_guard"]
            assert guard is not previous
            assert guard.reexec_under_plugin_venv("hue-kit") is None
            raise failure
        return original_exec(loader, module)

    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", fail_cli_load)
    with pytest.raises(RuntimeError) as excinfo:
        fresh_hue_loader()

    assert excinfo.value is failure
    if previously_present:
        assert sys.modules["bootstrap_guard"] is previous
    else:
        assert "bootstrap_guard" not in sys.modules
