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


def test_claimed_file_probe_names_0_37_0_when_owner_lacks_support(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reproduces the 0.29.0..0.36.x window: llm_scripting_kit.review_lane
    exists and main is callable, but review_lane._parse_args first accepted
    --claimed-file in 0.37.0. The wrapper must refuse BEFORE calling main --
    naming 0.37.0, not the base 0.29.0 owner requirement -- rather than let
    argparse's raw exit(2) 'unrecognized arguments' escape and be read as
    'this lane is not endpoint-eligible' (the OTHER meaning of exit 2)."""
    import argparse

    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")

    def _old_parse_args(argv):
        # Mirrors the real pre-0.37.0 parser: no --claimed-file option.
        parser = argparse.ArgumentParser()
        parser.add_argument("--lane", required=True)
        parser.add_argument("--model", required=True)
        parser.add_argument("--chunk", required=True)
        return parser.parse_args(argv)

    review_lane._parse_args = _old_parse_args

    def _main_must_not_run():
        raise AssertionError("main must not run when the probe refuses first")

    review_lane.main = _main_must_not_run
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--lane", "x", "--model", "y", "--chunk", "z",
            "--claimed-file", "w",
        ],
    )

    code = _run_wrapper(monkeypatch, package, review_lane)

    assert code != 0
    stderr = capsys.readouterr().err
    assert "too old" in stderr
    assert "0.37.0" in stderr


def test_wrapper_passes_through_to_shared_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.main = lambda: 17
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)

    assert _run_wrapper(monkeypatch, package, review_lane) == 17
