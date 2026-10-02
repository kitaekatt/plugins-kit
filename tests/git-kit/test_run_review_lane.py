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


def _run_lane_with_effort(*, effort: str, **_kw) -> int:
    return 0


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
    review_lane.run_lane = _run_lane_with_effort

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


def test_claimed_file_falls_through_to_main_when_probe_symbol_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_parse_args` is PRIVATE to llm-scripting-kit. An owner that renames or
    drops it must not be misdiagnosed as 'too old' -- that is a false refusal
    in the opposite direction from the bug the probe was added to fix. When
    the probe symbol is absent, the wrapper must fall through to main() and
    let the real argument parse produce whatever error it produces (the
    status quo for an owner without the probe at all)."""
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort
    # Deliberately NO _parse_args attribute on this fake owner module.
    review_lane.main = lambda: 42
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

    assert code == 42
    stderr = capsys.readouterr().err
    assert "too old" not in stderr


def test_mechanical_finding_probe_refuses_old_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort

    def old_parse(argv):
        parser = argparse.ArgumentParser()
        parser.add_argument("--lane", required=True)
        parser.add_argument("--model", required=True)
        parser.add_argument("--chunk", required=True)
        return parser.parse_args(argv)

    review_lane._parse_args = old_parse
    review_lane.main = lambda: 42
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(sys, "argv", [str(_SCRIPT), "--mechanical-scan-ran"])

    code = _run_wrapper(monkeypatch, package, review_lane)
    assert code != 0
    assert "mechanical scan finding support" in capsys.readouterr().err


def test_chunk_index_probe_refuses_old_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort

    def old_parse(argv):
        parser = argparse.ArgumentParser()
        parser.add_argument("--lane", required=True)
        parser.add_argument("--model", required=True)
        parser.add_argument("--chunk", required=True)
        parser.add_argument("--bundle")
        return parser.parse_args(argv)

    review_lane._parse_args = old_parse
    review_lane.main = lambda: 42
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--lane", "x", "--model", "y",
            "--bundle", "b.json", "--chunk-index", "0",
        ],
    )

    code = _run_wrapper(monkeypatch, package, review_lane)

    assert code != 0
    stderr = capsys.readouterr().err
    assert "too old" in stderr
    assert "0.49.0" in stderr


def test_chunk_index_falls_through_to_main_when_probe_symbol_is_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort
    review_lane.main = lambda: 42
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--lane", "x", "--model", "y",
            "--bundle", "b.json", "--chunk-index", "0",
        ],
    )

    code = _run_wrapper(monkeypatch, package, review_lane)

    assert code == 42
    stderr = capsys.readouterr().err
    assert "too old" not in stderr


def test_chunk_index_probe_accepts_a_current_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort

    def new_parse(argv):
        parser = argparse.ArgumentParser()
        parser.add_argument("--lane", required=True)
        parser.add_argument("--model", required=True)
        parser.add_argument("--chunk", default=None)
        parser.add_argument("--bundle")
        parser.add_argument("--chunk-index", type=int, default=None)
        return parser.parse_args(argv)

    review_lane._parse_args = new_parse
    review_lane.main = lambda: 0
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--lane", "x", "--model", "y",
            "--bundle", "b.json", "--chunk-index", "0",
        ],
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 0
    assert capsys.readouterr().err == ""


def test_wrapper_passes_through_to_shared_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort
    review_lane.main = lambda: 17
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)

    assert _run_wrapper(monkeypatch, package, review_lane) == 17


def test_pass_through_prints_no_warning_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Migration step 5: routing is silent on the success path.

    The review skill chooses an entry with `llm-scripting-kit describe`
    before it calls this wrapper, and skipping an entry is silent (model
    declaration directions 13 and 16). So a lane that dispatches adds nothing
    of the wrapper's own to stderr: stderr stays the channel for a refusal or
    a failure, which the skill reports as a failed lane.
    """
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = _run_lane_with_effort
    review_lane.main = lambda: 0
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
    monkeypatch.setattr(
        sys, "argv", [str(_SCRIPT), "--lane", "x", "--model", "sol", "--chunk", "z"]
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def _fake_owner(run_lane):
    package = types.ModuleType("llm_scripting_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_scripting_kit.review_lane")
    review_lane.run_lane = run_lane
    return package, review_lane


def test_owner_without_effort_support_is_refused_naming_0_59_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def old_run_lane(lane, model, chunk, mechanical_check_phrases=None):
        raise AssertionError("must not dispatch")

    package, review_lane = _fake_owner(old_run_lane)
    review_lane.main = lambda: old_run_lane("x", "y", "z")
    monkeypatch.setattr(
        sys, "argv",
        [str(_SCRIPT), "--lane", "x", "--model", "y", "--chunk", "z",
         "--effort", "high"],
    )

    code = _run_wrapper(monkeypatch, package, review_lane)

    assert code != 0
    stderr = capsys.readouterr().err
    assert "too old" in stderr
    assert "0.60.0" in stderr
    assert "--effort" in stderr


def test_effort_reaches_run_lane_through_the_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, str] = {}

    def run_lane(*, effort: str, **_kw) -> int:
        seen["effort"] = effort
        return 0

    package, review_lane = _fake_owner(run_lane)

    def main() -> int:
        argv = sys.argv[1:]
        return run_lane(effort=argv[argv.index("--effort") + 1])

    review_lane.main = main
    monkeypatch.setattr(
        sys, "argv",
        [str(_SCRIPT), "--lane", "x", "--model", "y", "--chunk", "z",
         "--effort", "xhigh"],
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 0
    assert seen == {"effort": "xhigh"}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("shape", ["absent", "uninspectable"])
def test_unconfirmable_effort_support_refuses_instead_of_falling_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], shape: str
) -> None:
    package, review_lane = _fake_owner(None)
    if shape == "absent":
        del review_lane.run_lane
    else:
        review_lane.run_lane = print  # builtin with no inspectable signature
        monkeypatch.setattr(
            "inspect.signature",
            lambda *_a, **_k: (_ for _ in ()).throw(ValueError("no signature")),
        )
    review_lane.main = lambda: pytest.fail("main must not run without effort")
    monkeypatch.setattr(
        sys, "argv",
        [str(_SCRIPT), "--lane", "x", "--model", "y", "--chunk", "z",
         "--effort", "high"],
    )

    code = _run_wrapper(monkeypatch, package, review_lane)

    assert code != 0
    assert "0.60.0" in capsys.readouterr().err
