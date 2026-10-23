"""Drift guard for the identical run_review_lane.py wrappers.

git-kit and p4-kit run the same reviewer lanes with the same prompts against the
same endpoints. Their scripts therefore stay byte-identical wrappers, the way
bootstrap_guard.py is. The seam-calling implementation is shared in
llm_kit.dispatch.review_lane, which each kit installs as the pinned
katsoft-llm-kit package; the wrappers only set up bootstrap and print an
absent-library diagnostic.

The seam-calling implementation cannot live in bootstrap_lib with the rest of
the shared review core, because it would make `openai` a transitive requirement
of the bootstrap plugin itself. tests/bootstrap/test_dependency_completeness.py
enforces that boundary. The VCS-neutral, LLM-neutral half (prompts, issue
schema, dispatch classification) lives in bootstrap_lib.code_review.lane_prompts.

This test lives in tests/bootstrap/ rather than either kit's directory for the
same reason test_skill_drift.py does: the invariant is the shared review
contract, and neither kit owns it.
"""

import ast
import json
import runpy
import sys
import tomllib
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
        # The parser owner shipped in 0.113.0 (git-kit, p4-kit); that stays the
        # minimum below. git-kit and p4-kit also launch skill scripts through
        # the guarded BOOTSTRAP_PYTHON form (interpreter contract, 0.120.0),
        # read the AGENTS.md ancestor chain (0.133.0) and the
        # review_exclusions.yaml axis (0.159.0). All three consumers of the
        # katsoft-llm-kit package require 0.160.0, the release that revokes a
        # shared-library link the manifest no longer declares and stamps a venv
        # against the released-package target of its requirement. llm-kit's
        # describe/run call bootstrap_lib.model_declaration (parse, CORE_IDS),
        # first shipped in bootstrap 0.129.0.
        expected = {
            "git-kit": "0.160.0",
            "p4-kit": "0.160.0",
            "llm-kit": "0.160.0",
        }
        minimum = {"git-kit": "0.113.0", "p4-kit": "0.113.0",
                   "llm-kit": "0.129.0"}

        def version(text):
            return tuple(int(part) for part in text.split("."))

        for kit, floor in expected.items():
            manifest = json.loads(
                (REPO_ROOT / "plugins" / kit / "bootstrap.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["requires_bootstrap"] == floor
            assert version(floor) >= version(minimum[kit])

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

    @pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parts[-3])
    def test_copy_probes_no_owner_version(self, path: Path) -> None:
        """The katsoft-llm-kit pin guarantees the lane's flags and signatures."""
        source = path.read_text(encoding="utf-8")
        for probe in ("_parse_args", "run_lane", "0.1.9", "REFUSE"):
            assert probe not in source, (
                f"{path} probes the owner ({probe!r}); the pinned "
                "katsoft-llm-kit requirement replaces every signature probe"
            )

    def test_absent_owner_prints_a_diagnostic_and_exits_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        bootstrap_guard = types.ModuleType("bootstrap_guard")
        bootstrap_guard.reexec_under_plugin_venv = lambda _plugin: None
        bootstrap_guard.require_bootstrap = lambda *_args, **_kwargs: None
        monkeypatch.setitem(sys.modules, "bootstrap_guard", bootstrap_guard)
        # Make the package unimportable regardless of what the environment
        # installs: drop cached modules and front the import system with a
        # finder that reports llm_kit itself as missing.
        for name in [n for n in sys.modules if n.split(".")[0] == "llm_kit"]:
            monkeypatch.delitem(sys.modules, name)

        class _Absent:
            @staticmethod
            def find_spec(name, path=None, target=None):
                if name == "llm_kit":
                    raise ModuleNotFoundError("No module named 'llm_kit'", name=name)
                return None

        monkeypatch.setattr(sys, "meta_path", [_Absent(), *sys.meta_path])
        monkeypatch.setattr(sys, "argv", [str(COPIES[0])])
        monkeypatch.syspath_prepend(str(COPIES[0].parent))

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(COPIES[0]), run_name="__main__")

        assert excinfo.value.code == 2
        assert "katsoft-llm-kit package is not installed" in capsys.readouterr().err


_SEAM_OWNERS = frozenset({"llm_kit", "llm_scripting_kit"})


class TestNoSeamImportLeakedIntoBootstrapLib:
    def test_bootstrap_lib_does_not_import_the_completion_seam(self) -> None:
        """The reason the wrapper is identical in both kits.

        Checked as an IMPORT, not a substring: the engine and shared_lib name
        llm_kit (and the legacy llm_scripting_kit) as a shared-library STRING
        (they are libs bootstrap links), and lane_prompts names the owner in
        prose explaining why it does not import it. None of those is a
        dependency.

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
                if any(name.split(".")[0] in _SEAM_OWNERS for name in names):
                    offenders.append(path.relative_to(REPO_ROOT))
                    break
        assert not offenders, (
            "bootstrap_lib must not import llm_kit or llm_scripting_kit -- it would make "
            f"openai a transitive bootstrap dependency: {offenders}"
        )


class TestOwnerDependenciesAreDeclared:
    """What the wrapper's llm_kit import reaches must be declared per kit.

    The completeness scan cannot see it: the wrapper imports the owner by
    string, so the scan never follows it. llm_kit.dispatch.review_lane imports
    PyYAML at load time and the openai SDK when a transport entry dispatches.
    Both come from the katsoft-llm-kit package (the config extra in the
    dependencies, the sdk extra in the optional endpoint-dispatch group), and
    the shared-library import names bootstrap_lib only: llm_kit is installed,
    not linked.
    """

    @pytest.mark.parametrize("kit", ["git-kit", "p4-kit"])
    def test_pyproject_and_manifest_declare_the_owner_reach(self, kit: str) -> None:
        plugin = REPO_ROOT / "plugins" / kit
        project = tomllib.loads(
            (plugin / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        assert project["requires-python"].startswith(">=3.12")
        assert "pyyaml" in project["dependencies"]
        assert "katsoft-llm-kit[config]>=1.1,<2" in project["dependencies"]
        assert "katsoft-contracts>=1.0,<2" in project["dependencies"]
        assert project["optional-dependencies"]["endpoint-dispatch"] == [
            "katsoft-llm-kit[sdk]>=1.1,<2"
        ]
        manifest = json.loads((plugin / "bootstrap.json").read_text(encoding="utf-8"))
        assert manifest["shared_lib_imports"] == ["bootstrap_lib"]
        assert "install" not in json.dumps(manifest.get("plugins", []))
