"""Tests for the git-kit's thin endpoint-lane wrapper.

The wrapper owns bootstrap setup, the absent-library diagnostic, and
pass-through to llm_kit.dispatch.review_lane.main. llm_kit is the pinned
katsoft-llm-kit package (plugins/git-kit/pyproject.toml), so the wrapper probes
none of the lane's flags or signatures. The seam itself is tested in
tests/llm-kit.
"""

from __future__ import annotations

import argparse
import runpy
import sys
import types
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "git-kit" / "scripts" / "run_review_lane.py"
)


def _fake_owner(main):
    package = types.ModuleType("llm_kit")
    package.__path__ = []
    review_lane = types.ModuleType("llm_kit.dispatch.review_lane")
    review_lane.main = main
    return package, review_lane


def _run_wrapper(monkeypatch: pytest.MonkeyPatch, package, review_lane):
    if package is not None:
        monkeypatch.setitem(sys.modules, "llm_kit", package)
    if review_lane is not None:
        monkeypatch.setitem(sys.modules, "llm_kit.dispatch.review_lane", review_lane)
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(_SCRIPT), run_name="__main__")
    return excinfo.value.code


def _make_llm_kit_unimportable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import llm_kit`` fail the way an environment without it does."""

    class _Absent:
        @staticmethod
        def find_spec(fullname, path=None, target=None):
            if fullname == "llm_kit":
                raise ModuleNotFoundError("No module named 'llm_kit'", name="llm_kit")
            return None

    for name in [m for m in sys.modules if m.startswith("llm_kit")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_Absent, *sys.meta_path])


def test_absent_package_refuses_naming_the_package_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_llm_kit_unimportable(monkeypatch)

    code = _run_wrapper(monkeypatch, None, None)

    assert code == 2
    stderr = capsys.readouterr().err
    assert "katsoft-llm-kit" in stderr
    assert "not installed" in stderr
    assert "bootstrap run" in stderr
    # The retired shared-library remedy must not come back.
    assert "claude plugin install llm-kit" not in stderr
    assert "shared library" not in stderr


def test_only_an_absent_llm_kit_is_the_absent_diagnostic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing dependency OF llm_kit is a broken install, not an absent package."""
    package_dir = tmp_path / "llm_kit"
    (package_dir / "dispatch").mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "dispatch" / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "dispatch" / "review_lane.py").write_text(
        "import a_module_that_does_not_exist_anywhere\n", encoding="utf-8"
    )
    for name in [m for m in sys.modules if m.startswith("llm_kit")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(ModuleNotFoundError) as excinfo:
        runpy.run_path(str(_SCRIPT), run_name="__main__")

    assert excinfo.value.name == "a_module_that_does_not_exist_anywhere"
    assert "katsoft-llm-kit" not in capsys.readouterr().err


def test_wrapper_passes_through_to_shared_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package, review_lane = _fake_owner(lambda: 17)

    assert _run_wrapper(monkeypatch, package, review_lane) == 17


def test_pass_through_prints_no_warning_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Routing is silent on the success path.

    The review skill chooses an entry with `llm-kit models describe` before it
    calls this wrapper, and skipping an entry is silent. So a lane that
    dispatches adds nothing of the wrapper's own to stderr: stderr stays the
    channel for a refusal or a failure, which the skill reports as a failed lane.
    """
    package, review_lane = _fake_owner(lambda: 0)
    monkeypatch.setattr(
        sys, "argv", [str(_SCRIPT), "--lane", "x", "--model", "sol", "--chunk", "z"]
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_the_wrapper_probes_nothing_about_the_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pin guarantees the lane's flags and signatures.

    An owner whose parser rejects every flag below, and which has no run_lane at
    all, still reaches main: the wrapper refuses nothing on a guess about the
    owner's version. (A probing wrapper refused this owner as "too old".)
    """

    def old_parse(argv):
        parser = argparse.ArgumentParser()
        parser.add_argument("--lane", required=True)
        parser.add_argument("--model", required=True)
        parser.add_argument("--chunk", required=True)
        return parser.parse_args(argv)

    package, review_lane = _fake_owner(lambda: 42)
    review_lane._parse_args = old_parse
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT), "--lane", "x", "--model", "y", "--effort", "high",
            "--bundle", "b.json", "--chunk-index", "0", "--claimed-file", "w",
            "--mechanical-scan-ran", "--mechanical-finding", "{}",
        ],
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 42
    assert capsys.readouterr().err == ""


def test_effort_reaches_the_owner_main_untouched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, str] = {}

    def main() -> int:
        argv = sys.argv[1:]
        seen["effort"] = argv[argv.index("--effort") + 1]
        return 0

    package, review_lane = _fake_owner(main)
    monkeypatch.setattr(
        sys, "argv",
        [str(_SCRIPT), "--lane", "x", "--model", "y", "--chunk", "z",
         "--effort", "xhigh"],
    )

    assert _run_wrapper(monkeypatch, package, review_lane) == 0
    assert seen == {"effort": "xhigh"}
    assert capsys.readouterr().err == ""


def test_the_wrapper_binds_the_real_owner_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the in-repo llm_kit the one the wrapper imports, and bind its main."""
    owner_lib = _SCRIPT.parents[3] / "packages" / "katsoft-llm-kit" / "src"
    monkeypatch.syspath_prepend(str(owner_lib))
    for name in [m for m in sys.modules if m.startswith("llm_kit")]:
        monkeypatch.delitem(sys.modules, name)

    namespace = runpy.run_path(str(_SCRIPT), run_name="bind_only")

    from llm_kit.dispatch import review_lane

    assert namespace["_main"] is review_lane.main
