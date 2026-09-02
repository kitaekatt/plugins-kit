"""Drift guard for the vendored run_review_lane.py copies.

git-kit and p4-kit run the same reviewer lanes with the same prompts against the
same endpoints, so their endpoint-dispatch runner is ONE file copied
byte-for-byte, the way bootstrap_guard.py is. The shared review pipeline's other
half already learned this lesson the hard way: a fix landed in one kit's
SKILL.md and never reached the other, which is why
scripts/gen_code_review_skills.py exists.

The runner cannot live in bootstrap_lib with the rest of the shared review core,
because it calls llm_scripting_kit -- which would make `openai` a transitive
requirement of the bootstrap plugin itself, the one every other plugin depends
on. tests/bootstrap/test_dependency_completeness.py enforces that boundary. So
the VCS-neutral, LLM-neutral half (prompts, issue schema, dispatch
classification) lives in bootstrap_lib.code_review.lane_prompts and is imported
by both copies; only the seam-calling half is vendored.

This test lives in tests/bootstrap/ rather than either kit's directory for the
same reason test_skill_drift.py does: the invariant is the shared review
contract, and neither kit owns it.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COPIES = [
    REPO_ROOT / "plugins" / kit / "scripts" / "run_review_lane.py"
    for kit in ("git-kit", "p4-kit")
]


class TestVendoredCopiesMatch:
    def test_every_copy_exists(self) -> None:
        for path in COPIES:
            assert path.is_file(), f"vendored runner missing: {path}"

    def test_copies_are_byte_identical(self) -> None:
        first, *rest = [path.read_bytes() for path in COPIES]
        for path, body in zip(COPIES[1:], rest):
            assert body == first, (
                f"{path} drifted from {COPIES[0]} -- the runner is vendored "
                f"byte-for-byte; copy it across rather than editing one side"
            )


class TestCopiesStayCopyable:
    """The properties that let two identical files serve two plugins."""

    @pytest.mark.parametrize("path", COPIES, ids=lambda p: p.parts[-3])
    def test_no_copy_names_its_own_plugin(self, path: Path) -> None:
        """A hardcoded plugin id is what would force the copies apart.

        The script reads its plugin from its own location instead, so the two
        files can stay identical while re-execing into different venvs.
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


class TestNoSeamImportLeakedIntoBootstrapLib:
    def test_bootstrap_lib_does_not_import_the_completion_seam(self) -> None:
        """The reason this file is vendored at all.

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
