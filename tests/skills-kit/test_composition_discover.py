"""Tests for the composition subject enumerator, discover_composition.py.

The composition subject rule is settled by owner decision (2026-08-12,
generation-deficiencies-and-plan.md P2): a directory is processed -- gets a
document COMPOSED -- when it, OR anything beneath it, holds code. That is
strictly wider than the COVERAGE subject rule (direct code only,
discover_coverage.py), and conflating the two sets is the bug the plan calls
out by name.

`discover_composition.py` is pure path arithmetic over one recursive walk
(`discover_coverage.walk_tree`): the walk
returns the leaves (directories directly holding code) and the composition set
is those leaves plus every one of their ancestors up to the named root. These
tests pin:

  * a leaf is both a coverage and a composition subject;
  * a code-free ancestor of a leaf (the `godot/` case) is a composition
    subject but not a coverage one;
  * a subtree with no code anywhere is neither;
  * VCS-ignored code does not pull its ancestors into the composition set --
    this is the regression test for the defect fixed while moving `walk_tree`
    (the pre-move copy never consulted `ignored_paths` at all);
  * an ignored directory is itself excluded, even when it directly holds code;
  * the rule is FILESYSTEM-defined, not defined over any generation output --
    a parent is a composition subject regardless of what its children would
    eventually decide to write.

Loaded via importlib under a unique module name, matching the sibling
discover_* test files -- the md-domain scripts directory ships several
discover_*.py and a bare import would collide in pytest's module cache.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain" / "scripts"
)
COVERAGE_PATH = SCRIPTS_DIR / "discover_coverage.py"
COMPOSITION_PATH = SCRIPTS_DIR / "discover_composition.py"
VCS_IGNORE_PATH = SCRIPTS_DIR / "vcs_ignore.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cov = _load("comp_discover_coverage", COVERAGE_PATH)
vcs = _load("comp_discover_vcs_ignore", VCS_IGNORE_PATH)
comp = _load("comp_discover_composition", COMPOSITION_PATH)


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mkrepo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


def _names(paths) -> set[str]:
    return {Path(p).name for p in paths}


@pytest.fixture(autouse=True)
def _clear_vcs_detection_cache():
    """Detection is cached per directory for the life of the process; a test
    that creates a repository where a previous test saw none must not inherit
    the old answer. discover_coverage.py imports its OWN instance of
    vcs_ignore through the scripts dir (a plain `import vcs_ignore`, resolved
    via sys.path rather than our importlib-under-an-alias loader), so that
    instance is a different module object from `vcs` above and must be
    cleared separately -- exactly the pattern test_coverage_discover.py uses.
    """
    import sys as _sys

    def _clear():
        vcs.clear_cache()
        other = _sys.modules.get("vcs_ignore")
        if other is not None and other is not vcs:
            other.clear_cache()

    _clear()
    yield
    _clear()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )


@pytest.fixture
def git_repo(tmp_path):
    """A real git worktree with one committed baseline."""
    repo = tmp_path / "wt"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(repo)], check=True, capture_output=True, text=True
    )
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _write(repo / "README.md", "readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


class TestCompositionSubjects:
    """`composition_subjects` is pure path arithmetic: every leaf plus every
    ancestor of a leaf, up to and including the named root."""

    def test_single_leaf_pulls_in_every_ancestor(self, tmp_path):
        root = tmp_path / "repo"
        leaf = root / "a" / "b" / "c"

        subjects = comp.composition_subjects([leaf], root)

        assert [str(p) for p in subjects] == sorted(
            str(p) for p in [root, root / "a", root / "a" / "b", leaf]
        )

    def test_two_leaves_share_a_common_ancestor_once(self, tmp_path):
        root = tmp_path / "repo"
        leaf1 = root / "engine" / "src"
        leaf2 = root / "engine" / "tests"

        subjects = comp.composition_subjects([leaf1, leaf2], root)

        assert _names(subjects) == {"repo", "engine", "src", "tests"}

    def test_no_leaves_yields_no_subjects_not_even_the_root(self, tmp_path):
        root = tmp_path / "repo"

        assert comp.composition_subjects([], root) == []


class TestCoverageVsCompositionSubjectSets:
    """The distinction the whole enumerator exists to produce."""

    def test_directory_with_direct_code_is_both_coverage_and_composition(
        self, tmp_path
    ):
        """A leaf is a subject under BOTH rules."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "engine" / "physics.js")

        subject = comp.build_subject(repo)

        assert str(repo / "engine") in subject["coverageSubjects"]
        assert str(repo / "engine") in subject["compositionSubjects"]

    def test_code_free_ancestor_is_composition_only(self, tmp_path):
        """The godot/ case: no direct code, but a code-bearing descendant."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "godot" / "project.godot")  # not a code extension
        _write(repo / "godot" / "scripts" / "player.gd")

        subject = comp.build_subject(repo)

        godot = str(repo / "godot")
        assert godot in subject["compositionSubjects"]
        assert godot not in subject["coverageSubjects"]
        assert godot in subject["codeFreeCompositionSubjects"]

    def test_announced_scope_count_is_composition_not_coverage(self, tmp_path):
        """SKILL.md announces `compositionSubjects` as the tree-scale scope
        count, because the analyze -> generate chain runs for a code-free
        intermediate directory too (it is composed from its children even
        though it is never assessed). `coverageSubjects` under-counts by
        exactly those code-free composition subjects.
        """
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "godot" / "project.godot")
        _write(repo / "godot" / "scripts" / "player.gd")

        subject = comp.build_subject(repo)

        assert len(subject["compositionSubjects"]) > len(subject["coverageSubjects"])

    def test_subtree_with_no_code_anywhere_is_neither(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "docs" / "notes.md")
        _write(repo / "docs" / "assets" / "diagram.svg")

        subject = comp.build_subject(repo)

        assert subject["compositionSubjects"] == []
        assert subject["coverageSubjects"] == []


class TestVcsIgnoreExcludesFromComposition:
    """The regression coverage for the defect fixed while moving `walk_tree`:
    the original pre-move copy never consulted `ignored_paths` at
    all, so code inside an ignored directory could pull its ancestors into
    scope. `walk_tree` now applies the project's VCS ignore rules DURING the
    descent, which prunes an ignored subtree before it is entered.
    """

    def test_code_only_under_an_ignored_path_does_not_pull_in_ancestors(
        self, git_repo
    ):
        _write(git_repo / ".gitignore", "build/\n")
        _write(git_repo / "engine" / "build" / "generated.c")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add gitignore")

        subject = comp.build_subject(git_repo)

        # engine/ has no code except under the ignored build/ subdirectory, so
        # neither engine/ nor build/ may become a composition subject.
        assert str(git_repo / "engine") not in subject["compositionSubjects"]
        assert str(git_repo / "engine" / "build") not in subject["compositionSubjects"]

    def test_sibling_unignored_code_still_pulls_in_the_shared_ancestor(
        self, git_repo
    ):
        """The negative case above is not just "engine/ is always excluded" --
        a REAL sibling code file still earns engine/ its place normally."""
        _write(git_repo / ".gitignore", "build/\n")
        _write(git_repo / "engine" / "build" / "generated.c")
        _write(git_repo / "engine" / "physics.c")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add gitignore")

        subject = comp.build_subject(git_repo)

        assert str(git_repo / "engine") in subject["compositionSubjects"]
        assert str(git_repo / "engine") in subject["coverageSubjects"]
        assert str(git_repo / "engine" / "build") not in subject["compositionSubjects"]


class TestIgnoredDirectoryItself:
    """An ignored directory is neither a subject nor an input, even when it
    directly holds code -- it is pruned at the point it would be entered."""

    def test_ignored_directory_with_direct_code_is_excluded(self, git_repo):
        _write(git_repo / ".gitignore", "vendor_scripts/\n")
        _write(git_repo / "vendor_scripts" / "lib.py")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add gitignore")

        subject = comp.build_subject(git_repo)

        vendored = str(git_repo / "vendor_scripts")
        assert vendored not in subject["compositionSubjects"]
        assert vendored not in subject["coverageSubjects"]
        assert not any(
            e["path"] == vendored and e["reason"] != cov.SKIP_IGNORED
            for e in subject["skipped"]
        )
        assert any(
            e["path"] == vendored and e["reason"] == cov.SKIP_IGNORED
            for e in subject["skipped"]
        )

    def test_ignored_directory_does_not_appear_even_as_an_ancestor(self, git_repo):
        """Belt-and-suspenders on the same fact from the other direction: an
        ignored directory that ALSO has a (likewise-ignored) code-bearing
        child must not surface via the child-to-ancestor path arithmetic
        either -- the whole ignored subtree is pruned before descent.
        """
        _write(git_repo / ".gitignore", "vendor_scripts/\n")
        _write(git_repo / "vendor_scripts" / "sub" / "lib.py")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add gitignore")

        subject = comp.build_subject(git_repo)

        assert str(git_repo / "vendor_scripts") not in subject["compositionSubjects"]
        assert str(git_repo / "vendor_scripts" / "sub") not in subject["compositionSubjects"]


class TestRuleIsFilesystemDefinedNotOutputDefined:
    """A directory whose children would all take the generation null branch is
    still a composition subject, because the rule is evaluated over the
    FILESYSTEM (does code exist beneath here) and never over what a
    generation run eventually decided to write. The enumerator has no notion
    of a "null branch" at all -- these tests demonstrate that no document,
    report, or other generation artifact needs to exist for a directory to be
    correctly enumerated.
    """

    def test_parent_is_a_composition_subject_with_no_documents_anywhere(
        self, tmp_path
    ):
        repo = _mkrepo(tmp_path / "repo")
        # Two child subtrees with code, no CLAUDE.md anywhere in the tree --
        # nothing has ever been composed or written.
        _write(repo / "bots" / "lib" / "spawn.py")
        _write(repo / "bots" / "runner" / "loop.py")

        subject = comp.build_subject(repo)

        assert str(repo / "bots") in subject["compositionSubjects"]
        assert str(repo / "bots") in subject["codeFreeCompositionSubjects"]
        assert subject["claudeMdPaths"] == []

    def test_re_enumerating_is_idempotent_regardless_of_prior_runs(self, tmp_path):
        """Running the enumerator twice over the same tree, with a CLAUDE.md
        now present (as if a previous generation run had written one), yields
        the identical composition set -- documents are not an input to this
        rule."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "bots" / "lib" / "spawn.py")

        before = comp.build_subject(repo)["compositionSubjects"]

        _write(repo / "bots" / "CLAUDE.md", "# bots\n")

        after = comp.build_subject(repo)["compositionSubjects"]

        assert before == after


class TestBuildSubjectShape:
    def test_shape(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "engine" / "main.c")

        subject = comp.build_subject(repo)

        assert set(subject) == {
            "root", "rootExclusion", "compositionSubjects", "coverageSubjects",
            "codeFreeCompositionSubjects", "claudeMdPaths", "skipped",
            "noisePruned",
        }

    def test_composition_set_contains_coverage_set(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "godot" / "scripts" / "player.gd")
        _write(repo / "godot" / "project.godot")

        subject = comp.build_subject(repo)

        assert set(subject["coverageSubjects"]) <= set(subject["compositionSubjects"])


class TestCli:
    def _run(self, *args, cwd: Path):
        return subprocess.run(
            [sys.executable, str(COMPOSITION_PATH), *args],
            capture_output=True, text=True, cwd=str(cwd),
        )

    def test_no_arguments_refuses_rather_than_scanning_the_repo(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")

        result = self._run(cwd=repo)

        assert result.returncode == 2
        assert "no whole-repo default" in result.stderr.lower()

    def test_missing_directory_is_an_error(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")

        result = self._run("nope", cwd=repo)

        assert result.returncode == 2
        assert "not a directory" in result.stderr.lower()

    def test_json_output_is_parseable(self, tmp_path):
        import json

        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "godot" / "scripts" / "player.gd")

        result = self._run(".", "--json", cwd=repo)

        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert any(
            Path(p).name == "godot" for p in payload["compositionSubjects"]
        )

    def test_text_output_names_the_empty_set_explicitly(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "docs" / "notes.md")

        result = self._run(".", cwd=repo)

        assert result.returncode == 0
        assert "NONE" in result.stdout


class TestWalkTreeStillWorksAfterTheMove:
    """`walk_tree` is no longer defined in a caller's own copy; it lives in
    discover_coverage.py and is imported from there. Its leaf-enumeration
    contract must be unchanged.
    """

    def test_leaf_enumeration_reaches_every_depth(self, tmp_path):
        root = _mkrepo(tmp_path / "repo")
        _write(root / "top.js")
        _write(root / "engine" / "physics.js")
        _write(root / "engine" / "solver" / "deep.js")
        _write(root / "ui" / "panel.js")
        _write(root / "docs" / "notes.md")

        leaves, _, _, _ = cov.walk_tree(root)

        assert {Path(p).name for p in leaves} == {"repo", "engine", "solver", "ui"}

    def test_walk_tree_now_also_applies_vcs_ignore(self, git_repo):
        """The defect fix lives in the shared primitive: an ignored directory's
        code must not produce a leaf.
        """
        _write(git_repo / ".gitignore", "build/\n")
        _write(git_repo / "engine" / "build" / "generated.c")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add gitignore")

        leaves, _, _, _ = cov.walk_tree(git_repo)

        assert str(git_repo / "engine" / "build") not in [str(p) for p in leaves]
        assert str(git_repo / "engine") not in [str(p) for p in leaves]
