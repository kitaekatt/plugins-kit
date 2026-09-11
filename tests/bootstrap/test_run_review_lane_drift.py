"""Drift guard for the identical run_review_lane.py wrappers.

git-kit and p4-kit run the same reviewer lanes with the same prompts against the
same endpoints. Their scripts therefore stay byte-identical wrappers, the way
bootstrap_guard.py is. The seam-calling implementation is shared in
llm_scripting_kit.review_lane; the wrappers only set up bootstrap and host its
REFUSE probe.

The seam-calling implementation cannot live in bootstrap_lib with the rest of
the shared review core, because it would make `openai` a transitive requirement
of the bootstrap plugin itself. tests/bootstrap/test_dependency_completeness.py
enforces that boundary. The VCS-neutral, LLM-neutral half (prompts, issue
schema, dispatch classification) lives in bootstrap_lib.code_review.lane_prompts.

This test lives in tests/bootstrap/ rather than either kit's directory for the
same reason test_skill_drift.py does: the invariant is the shared review
contract, and neither kit owns it.
"""

import argparse
import ast
import json
import runpy
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COPIES = [
    REPO_ROOT / "plugins" / kit / "scripts" / "run_review_lane.py"
    for kit in ("git-kit", "p4-kit")
]
PARSER_COPIES = [
    REPO_ROOT / "plugins" / kit / "scripts" / "parse_review_lane.py"
    for kit in ("git-kit", "p4-kit")
]


class TestWrapperCopiesMatch:
    def test_every_copy_exists(self) -> None:
        for path in COPIES:
            assert path.is_file(), f"runner wrapper missing: {path}"

    def test_copies_are_byte_identical(self) -> None:
        first, *rest = [path.read_bytes() for path in COPIES]
        for path, body in zip(COPIES[1:], rest):
            assert body == first, (
                f"{path} drifted from {COPIES[0]} -- the runner wrappers must "
                f"stay byte-identical; update both sides together"
            )

    def test_consumers_require_the_parser_owner_version(self) -> None:
        expected = {
            "git-kit": "0.113.0",
            "p4-kit": "0.113.0",
            "llm-scripting-kit": "0.107.0",
        }
        for kit, floor in expected.items():
            manifest = json.loads(
                (REPO_ROOT / "plugins" / kit / "bootstrap.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["requires_bootstrap"] == floor

    def test_parser_copies_are_byte_identical(self) -> None:
        first, *rest = [path.read_bytes() for path in PARSER_COPIES]
        for path, body in zip(PARSER_COPIES[1:], rest):
            assert body == first, (
                f"{path} drifted from {PARSER_COPIES[0]} -- the parser wrappers "
                "must stay byte-identical; update both sides together"
            )

    @pytest.mark.parametrize("path", PARSER_COPIES, ids=lambda p: p.parts[-3])
    def test_parser_copy_dispatches_to_shared_main(
        self, path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bootstrap_guard = types.ModuleType("bootstrap_guard")
        bootstrap_guard.reexec_under_plugin_venv = lambda _plugin: None
        bootstrap_guard.require_bootstrap = lambda *_args, **_kwargs: None
        lane_output = types.ModuleType("bootstrap_lib.code_review.lane_output")
        lane_output.main = lambda: 17
        monkeypatch.setitem(sys.modules, "bootstrap_guard", bootstrap_guard)
        monkeypatch.setitem(
            sys.modules, "bootstrap_lib.code_review.lane_output", lane_output
        )

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(path), run_name="__main__")

        assert excinfo.value.code == 17


class TestCopiesStayIdentical:
    """The properties that let identical wrappers serve two plugins."""

    @pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parts[-3])
    def test_no_copy_names_its_own_plugin(self, path: Path) -> None:
        """A hardcoded plugin id is what would force the wrappers apart.

        The script reads its plugin from its own location instead, so the
        wrappers can stay identical while re-execing into different venvs.
        """
        source = path.read_text(encoding="utf-8")
        for literal in ('"git-kit"', "'git-kit'", '"p4-kit"', "'p4-kit'"):
            assert literal not in source, (
                f"{path} hardcodes {literal}; derive the plugin id from "
                f"__file__ so both copies stay byte-identical"
            )

    @pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parts[-3])
    def test_each_copy_sits_where_it_derives_its_plugin_from(self, path: Path) -> None:
        """`parents[1].name` must actually be the owning plugin directory."""
        assert path.parent.name == "scripts"
        assert path.parent.parent.name in {"git-kit", "p4-kit"}

    def test_bundle_probe_refuses_an_old_owner(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        package = types.ModuleType("llm_scripting_kit")
        package.__path__ = []
        review_lane = types.ModuleType("llm_scripting_kit.review_lane")

        def old_parse(argv):
            parser = argparse.ArgumentParser()
            parser.add_argument("--lane", required=True)
            parser.add_argument("--model", required=True)
            parser.add_argument("--chunk", required=True)
            return parser.parse_args(argv)

        review_lane._parse_args = old_parse
        review_lane.main = lambda: 42
        bootstrap_guard = types.ModuleType("bootstrap_guard")
        bootstrap_guard.reexec_under_plugin_venv = lambda _plugin: None
        bootstrap_guard.require_bootstrap = lambda *_args, **_kwargs: None
        monkeypatch.setitem(sys.modules, "bootstrap_guard", bootstrap_guard)
        monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
        monkeypatch.setattr(
            sys,
            "argv",
            [str(COPIES[0]), "--bundle", "bundle.json"],
        )
        monkeypatch.syspath_prepend(str(COPIES[0].parent))

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(COPIES[0]), run_name="__main__")

        assert excinfo.value.code != 0
        stderr = capsys.readouterr().err
        assert "prepared review bundle support" in stderr
        assert "0.42.0" in stderr

    def test_phrase_map_probe_refuses_owner_that_only_parses_bundle(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        package = types.ModuleType("llm_scripting_kit")
        package.__path__ = []
        review_lane = types.ModuleType("llm_scripting_kit.review_lane")

        def old_run_lane(*, lane, model, diff_text, mechanical_findings=None):
            return {}

        def old_parse(argv):
            parser = argparse.ArgumentParser()
            parser.add_argument("--lane")
            parser.add_argument("--model")
            parser.add_argument("--chunk")
            parser.add_argument("--bundle")
            parser.add_argument("--mechanical-scan-ran", action="store_true")
            parser.add_argument("--mechanical-finding", action="append")
            return parser.parse_args(argv)

        review_lane._parse_args = old_parse
        review_lane.run_lane = old_run_lane
        review_lane.main = lambda: 42
        bootstrap_guard = types.ModuleType("bootstrap_guard")
        bootstrap_guard.reexec_under_plugin_venv = lambda _plugin: None
        bootstrap_guard.require_bootstrap = lambda *_args, **_kwargs: None
        monkeypatch.setitem(sys.modules, "bootstrap_guard", bootstrap_guard)
        monkeypatch.setitem(sys.modules, "llm_scripting_kit", package)
        monkeypatch.setitem(sys.modules, "llm_scripting_kit.review_lane", review_lane)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                str(COPIES[0]),
                "--bundle", "bundle.json",
                "--mechanical-scan-ran",
            ],
        )
        monkeypatch.syspath_prepend(str(COPIES[0].parent))

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(COPIES[0]), run_name="__main__")

        assert excinfo.value.code != 0
        stderr = capsys.readouterr().err
        assert "bundle mechanical check phrase support" in stderr
        assert "0.43.0" in stderr


class TestNoSeamImportLeakedIntoBootstrapLib:
    def test_bootstrap_lib_does_not_import_the_completion_seam(self) -> None:
        """The reason the wrapper is identical in both kits.

        Checked as an IMPORT, not a substring: the engine and shared_lib name
        llm_scripting_kit as a shared-library STRING (it is one of the libs
        bootstrap publishes), and lane_prompts names it in prose explaining why
        it does not import it. None of those is a dependency.

        bootstrap_lib is linked into plugin venvs that never make an LLM call;
        an import here would make `openai` a bootstrap dependency. Asserted
        directly so the boundary fails loudly rather than as a puzzling
        dependency-completeness error in an unrelated plugin.
        """
        lib = REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib"
        offenders = []
        for path in sorted(lib.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name.split(".")[0] == "llm_scripting_kit" for name in names):
                    offenders.append(path.relative_to(REPO_ROOT))
                    break
        assert not offenders, (
            "bootstrap_lib must not reference llm_scripting_kit -- it would make "
            f"openai a transitive bootstrap dependency: {offenders}"
        )
