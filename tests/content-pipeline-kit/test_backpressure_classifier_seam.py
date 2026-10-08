"""The classify_backpressure edge is DEGRADE: disclosed, with distinct states."""
import sys
import types

import pytest

from content_pipeline.llm import platform


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(platform, "_BACKPRESSURE_WARNED", set())


def _block(monkeypatch, name):
    for mod in list(sys.modules):
        if mod == name or mod.startswith(name + "."):
            monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, name, None)  # import raises ImportError


def test_absent_lib_warns_with_install_command(monkeypatch):
    _block(monkeypatch, "llm_scripting_kit")
    with pytest.warns(RuntimeWarning, match="absent") as rec:
        assert platform._backpressure_classifier() is None
    assert "plugin install llm-scripting-kit" in str(rec[0].message)


def test_too_old_lib_warns_with_update_command_and_floor(monkeypatch):
    pkg = types.ModuleType("llm_scripting_kit")
    pkg.__path__ = []
    comp = types.ModuleType("llm_scripting_kit.completion")
    comp.__path__ = []
    halt = types.ModuleType("llm_scripting_kit.completion.halt")  # no symbol
    comp.halt = halt
    for n, m in (
        ("llm_scripting_kit", pkg),
        ("llm_scripting_kit.completion", comp),
        ("llm_scripting_kit.completion.halt", halt),
    ):
        monkeypatch.setitem(sys.modules, n, m)
    with pytest.warns(RuntimeWarning, match="predates") as rec:
        assert platform._backpressure_classifier() is None
    msg = str(rec[0].message)
    assert "plugin update llm-scripting-kit" in msg and "0.61.0" in msg


def test_warns_once_per_state(monkeypatch, recwarn):
    _block(monkeypatch, "llm_scripting_kit")
    platform._backpressure_classifier()
    platform._backpressure_classifier()
    assert len([w for w in recwarn if w.category is RuntimeWarning]) == 1
