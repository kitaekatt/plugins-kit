"""Tests for the git-kit's thin endpoint-lane wrapper.

The wrapper owns bootstrap setup, the shared-library REFUSE probe, and
pass-through to llm_scripting_kit.review_lane.main. The seam itself is tested
in tests/llm-scripting-kit/test_review_lane.py.
"""

from __future__ import annotations

import runpy
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "git-kit" / "scripts" / "run_review_lane.py"
)


def _run_wrapper(monkeypatch: pytest.MonkeyPatch, package, review_lane):
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
    if review_lane is not None:
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    return excinfo.value.code


def test_absent_shared_library_refuses_with_install_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)

    code = _run_wrapper(monkeypatch, None, None)

    assert code != 0
    stderr = capsys.readouterr().err
    assert "not installed" in stderr
    assert "claude plugin install llm-scripting-kit@plugins-kit" in stderr
    assert "claude plugin update" not in stderr


def test_present_but_too_old_shared_library_refuses_with_update_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    old_review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", old_review_lane)

    code = _run_wrapper(monkeypatch, package, old_review_lane)

    assert code != 0
    stderr = capsys.readouterr().err
    assert "too old" in stderr
    assert "owner version 0.29.0" in stderr
    assert "claude plugin update llm-scripting-kit@plugins-kit" in stderr
    assert "claude plugin install" not in stderr


def test_wrapper_passes_through_to_shared_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.main = lambda: 17
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)

    assert _run_wrapper(monkeypatch, package, review_lane) == 17
