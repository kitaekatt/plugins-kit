"""Tests for the coverage verb's scripts/discover_coverage.py.

discover_coverage.py resolves the coverage subject -- (one directory's own direct
code files, its ambient CLAUDE.md chain) -- and applies the STRUCTURAL
exclusions. It is the mechanical half of the verb and decides nothing about what
the code means, so it is fully testable ahead of the analysis criteria.

Three behaviours are pinned here because they are the ones the design turns on
and the ones a plausible-looking implementation gets wrong:

  * The code-file set is NON-RECURSIVE. A child directory is its own subject;
    composing a parent reads its children's finished CLAUDE.md files, not their
    source. A recursive subject made every ancestor re-derive its descendants'
    facts, and the old contract is pinned inverted in
    TestNonRecursiveSubject.
  * The ambient chain INCLUDES a CLAUDE.md at the directory itself, and it still
    walks UPWARD. The document lanes' resolver deliberately starts at the
    target's PARENT (the target being the CLAUDE.md itself); reusing that
    convention here would drop the single most ambient file for a directory
    subject.
  * A directory with NO ambient CLAUDE.md returns an EMPTY chain rather than
    climbing to something that does not load for it. That null case is the one
    the whole verb exists to surface, so it must be representable.

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
DISCOVER_PATH = SCRIPTS_DIR / "discover_coverage.py"
VCS_IGNORE_PATH = SCRIPTS_DIR / "vcs_ignore.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cov = _load("cov_discover", DISCOVER_PATH)
vcs = _load("cov_vcs_ignore", VCS_IGNORE_PATH)


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _mkrepo(root: Path) -> Path:
    """Create a directory that reads as a repository root to find_project_root."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


def _reasons(subject: dict) -> set[str]:
    return {entry["reason"] for entry in subject["skipped"]}


def _skipped_names(subject: dict) -> set[str]:
    return {Path(entry["path"]).name for entry in subject["skipped"]}


class TestIsCodeFile:
    def test_source_extensions_are_code(self):
        assert cov.is_code_file(Path("main.c"))
        assert cov.is_code_file(Path("engine.cpp"))
        assert cov.is_code_file(Path("api.py"))
        assert cov.is_code_file(Path("app.ts"))

    def test_markdown_is_not_code(self):
        assert not cov.is_code_file(Path("CLAUDE.md"))
        assert not cov.is_code_file(Path("README.md"))


class TestAmbientChain:
    def test_includes_claude_md_at_the_subtree_root(self, tmp_path):
        """The subtree's OWN CLAUDE.md is the most ambient file it has."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "src" / "CLAUDE.md", "# src\n")

        chain = cov.ambient_chain(repo / "src")

        assert len(chain) == 1
        assert chain[0].name == "CLAUDE.md"
        assert chain[0].parent.name == "src"

    def test_collects_ancestors_root_most_first(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "CLAUDE.md", "# root\n")
        _write(repo / "engine" / "CLAUDE.md", "# engine\n")
        (repo / "engine" / "src").mkdir(parents=True, exist_ok=True)

        chain = cov.ambient_chain(repo / "engine" / "src")

        # engine/src has no CLAUDE.md of its own; the two ancestors load for it,
        # outermost first.
        assert [str(p.parent.name) for p in chain] == ["repo", "engine"]

    def test_null_chain_is_representable(self, tmp_path):
        """A subtree nothing covers returns an empty chain, not a climbed guess."""
        repo = _mkrepo(tmp_path / "repo")
        (repo / "engine" / "src").mkdir(parents=True, exist_ok=True)

        assert cov.ambient_chain(repo / "engine" / "src") == []

    def test_sibling_subtree_claude_md_is_not_ambient(self, tmp_path):
        """The distinction the whole investigation turns on."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "sandbox" / "CLAUDE.md", "# sandbox\n")
        (repo / "engine").mkdir(parents=True, exist_ok=True)

        chain = cov.ambient_chain(repo / "engine")

        assert chain == []

    def test_walk_stops_at_nested_repo_boundary(self, tmp_path):
        """A nested repo's chain is its own, never the outer repo's."""
        outer = _mkrepo(tmp_path / "outer")
        _write(outer / "CLAUDE.md", "# outer\n")
        inner = _mkrepo(outer / "vendored" / "inner")
        _write(inner / "CLAUDE.md", "# inner\n")

        chain = cov.ambient_chain(inner)

        assert [p.parent.name for p in chain] == ["inner"]
        assert all("outer" != p.parent.name for p in chain)

    def test_perforce_workspace_resolves_its_ancestors(self, tmp_path):
        """A P4 workspace has no .git anywhere; the chain must still climb.

        A git-only root walk returns None here, which the guard below treats as
        "no project" and turns into a self-only chain -- so every ancestor
        CLAUDE.md a Perforce project actually loads went unseen, and the
        already-ambient-suppressed criterion had nothing to suppress against.
        """
        ws = tmp_path / "workspace"
        (ws / "Source" / "Editor" / "Private").mkdir(parents=True, exist_ok=True)
        _write(ws / ".p4config.txt", "P4CLIENT=ws\n")
        _write(ws / "CLAUDE.md", "# workspace\n")
        _write(ws / "Source" / "CLAUDE.md", "# source\n")
        _write(ws / "Source" / "Editor" / "CLAUDE.md", "# editor\n")

        assert not (ws / ".git").exists()

        chain = cov.ambient_chain(ws / "Source" / "Editor")
        assert [p.parent.name for p in chain] == ["workspace", "Source", "Editor"]

        # A deeper directory with no CLAUDE.md of its own still sees all three.
        deep = cov.ambient_chain(ws / "Source" / "Editor" / "Private")
        assert [p.parent.name for p in deep] == ["workspace", "Source", "Editor"]

    def test_rootless_directory_still_does_not_climb(self, tmp_path):
        """No marker of any kind: the walk must not leave the named tree.

        The guard exists so a directory outside every project cannot pick up an
        unrelated ancestor's CLAUDE.md (a home directory's, say). Widening the
        marker set must not weaken it.
        """
        loose = tmp_path / "loose" / "src"
        loose.mkdir(parents=True, exist_ok=True)
        _write(tmp_path / "loose" / "CLAUDE.md", "# not a project\n")

        assert cov.ambient_chain(loose) == []

    def test_walk_stops_at_nested_perforce_boundary(self, tmp_path):
        """The nested-project boundary holds for a non-git marker too."""
        outer = _mkrepo(tmp_path / "outer")
        _write(outer / "CLAUDE.md", "# outer\n")
        inner = outer / "vendored" / "inner"
        inner.mkdir(parents=True, exist_ok=True)
        _write(inner / ".p4config.txt", "P4CLIENT=inner\n")
        _write(inner / "CLAUDE.md", "# inner\n")

        chain = cov.ambient_chain(inner)

        assert [p.parent.name for p in chain] == ["inner"]


class TestWalkDirectory:
    def test_collects_code_files_and_ignores_docs(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "util.py")
        _write(root / "README.md")

        code_files, skipped, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c", "util.py"}
        assert skipped == []

    def test_vendored_directories_are_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "node_modules" / "dep.js")
        _write(root / "vendor" / "lib.c")

        code_files, skipped, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_VENDORED}
        assert _skipped_names({"skipped": skipped}) == {"node_modules", "vendor"}

    def test_generated_directories_are_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "generated" / "schema_pb2.py")

        code_files, skipped, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_GENERATED}

    def test_nested_repository_is_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _mkrepo(root / "submodule")
        _write(root / "submodule" / "inner.c")

        code_files, skipped, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_NESTED_REPO}

    def test_noise_directories_are_pruned_without_itemizing(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "__pycache__" / "main.cpython-312.pyc")
        _write(root / ".venv" / "lib.py")

        code_files, skipped, noise, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert skipped == []
        assert noise == 2

    def test_walk_reads_no_file_contents(self, tmp_path, monkeypatch):
        """Structural exclusions are applied before anything is read."""
        root = tmp_path / "src"
        _write(root / "main.c", "int main(void){return 0;}\n")

        def _explode(*args, **kwargs):
            raise AssertionError("walk_directory must not read file contents")

        monkeypatch.setattr(Path, "read_text", _explode)
        monkeypatch.setattr(Path, "read_bytes", _explode)

        code_files, _, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)
class TestSymlinks:
    """The whole containment/alias problem DISSOLVES under a non-recursive
    subject: a linked directory is never descended into, so it can neither pull
    foreign code in nor emit one real directory's files twice, whatever it
    resolves to. These tests pin that dissolution -- if a later change
    reintroduces descent, all four fail rather than the symlink defects
    reappearing silently.
    """

    def test_symlink_to_an_outside_directory_contributes_nothing(self, tmp_path):
        outside = tmp_path / "outside"
        _write(outside / "foreign.c")
        root = tmp_path / "src"
        _write(root / "main.c")
        (root / "linked").symlink_to(outside, target_is_directory=True)

        code_files, _, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}

    def test_circular_symlink_is_not_fatal(self, tmp_path):
        """A loop used to reach Path.resolve(), which raises RuntimeError rather
        than OSError on ELOOP. Nothing resolves a child directory any more, so
        the loop is simply never followed.
        """
        root = tmp_path / "src"
        _write(root / "main.c")
        (root / "loop_a").symlink_to(root / "loop_b", target_is_directory=True)
        (root / "loop_b").symlink_to(root / "loop_a", target_is_directory=True)

        code_files, _, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.c"}

    def test_symlink_staying_inside_is_still_not_part_of_the_subject(self, tmp_path):
        """Old contract: an inside-resolving link WAS followed and its files
        collected. Under the settled model `real/` is its own subject and
        `alias/` names the same one -- neither belongs to this directory.
        """
        root = tmp_path / "src"
        _write(root / "real" / "kept.c")
        (root / "alias").symlink_to(root / "real", target_is_directory=True)

        code_files, _, _, _ = cov.walk_directory(root)

        assert code_files == []

    def test_alias_of_one_real_directory_yields_no_duplicates(self, tmp_path):
        """Two names for one real directory must not emit its files twice -- now
        true by construction rather than by a visited-set.
        """
        root = tmp_path / "src"
        _write(root / "own.c")
        _write(root / "real" / "kept.c")
        (root / "alias").symlink_to(root / "real", target_is_directory=True)

        code_files, _, _, _ = cov.walk_directory(root)

        assert [p.name for p in code_files] == ["own.c"]


class TestNonRecursiveSubject:
    """The subject is ONE DIRECTORY'S OWN code files.

    The old contract was recursive: `discover_coverage.py <dir>` on a directory
    holding 4 direct files reported 125, because it swept the whole subtree. Every
    ancestor therefore re-derived its descendants' facts from source, and any
    de-duplication downstream compared facts against copies of themselves. Under
    the settled model a child directory is its own subject and reaches its parent
    through its own finished CLAUDE.md, never through this walk.
    """

    def test_code_bearing_children_contribute_nothing(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "app.js")
        _write(root / "index.js")
        _write(root / "engine" / "physics.js")
        _write(root / "engine" / "solver" / "deep.js")
        _write(root / "ui" / "panel.js")

        code_files, _, _, _ = cov.walk_directory(root)

        assert [p.name for p in code_files] == ["app.js", "index.js"]

    def test_directory_whose_code_is_all_in_children_is_an_empty_subject(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "engine" / "physics.js")
        _write(root / "README.md")

        code_files, _, _, unknown = cov.walk_directory(root)

        assert code_files == []
        # Empty, not a discovery failure: nothing unaccounted-for was seen HERE.
        assert unknown == {}

    def test_unknown_extensions_are_not_collected_from_children(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "here.xyz")
        _write(root / "child" / "there.xyz")
        _write(root / "child" / "deeper" / "elsewhere.xyz")

        _, _, _, unknown = cov.walk_directory(root)

        assert unknown == {".xyz": 1}

    def test_the_ambient_chain_still_walks_upward(self, tmp_path):
        """Only the CODE set stopped recursing. Ambience is unchanged."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "CLAUDE.md", "# root\n")
        _write(repo / "src" / "CLAUDE.md", "# src\n")
        _write(repo / "src" / "app.js")
        _write(repo / "src" / "engine" / "physics.js")

        subject = cov.build_subject(repo / "src")

        assert [Path(p).parent.name for p in subject["ambientClaudeMdPaths"]] == [
            "repo", "src",
        ]
        assert [Path(p).name for p in subject["codeFiles"]] == ["app.js"]


class TestExtensionCoverage:
    """CODE_DATA_EXT additions (.mjs/.cjs/.gd/.tscn) and the unknownExtensions
    signal that catches the NEXT missing extension instead of dropping it with
    no trace. This is the defect the whole change exists to fix: a well-formed
    subtree of an unrecognized language used to read as codeFiles: [] with
    nothing to say why.
    """

    def test_gdscript_only_subtree_yields_non_empty_code_files(self, tmp_path):
        root = tmp_path / "godot_project"
        _write(root / "player.gd")
        _write(root / "player.gd.uid")  # unrelated unknown ext, not code

        code_files, _, _, unknown = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"player.gd"}
        assert unknown.get(".uid") == 1

    def test_dotfiles_and_convention_files_do_not_read_as_unknown(self, tmp_path):
        """A code-free directory holding only repo convention files is an EMPTY
        subject, not a discovery failure. Path(".gitignore").suffix and
        Path("LICENSE").suffix are both "", so without the name-based exemption
        every such directory would trip the lane's refusal rule and report a
        failure where the honest answer is "no code here".
        """
        root = tmp_path / "docs_only"
        _write(root / ".gitignore")
        _write(root / ".editorconfig")
        _write(root / "LICENSE")
        _write(root / "CHANGELOG")
        _write(root / "README.md")

        code_files, _, _, unknown = cov.walk_directory(root)

        assert code_files == []
        assert unknown == {}

    def test_extensionless_non_convention_file_is_still_reported(self, tmp_path):
        """Makefile/Dockerfile plausibly ARE code, so they stay counted -- the
        exemption above is a narrow name list, not a blanket pass for every
        extensionless file.
        """
        root = tmp_path / "buildable"
        _write(root / "Makefile")
        _write(root / "Dockerfile")

        code_files, _, _, unknown = cov.walk_directory(root)

        assert code_files == []
        assert unknown == {"": 2}

    def test_godot_scene_extension_is_recognized_as_code(self, tmp_path):
        root = tmp_path / "godot_project"
        _write(root / "main.tscn")

        code_files, _, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"main.tscn"}

    def test_mjs_and_cjs_are_recognized_as_code(self, tmp_path):
        root = tmp_path / "node_pkg"
        _write(root / "build.mjs")
        _write(root / "loader.cjs")

        code_files, _, _, _ = cov.walk_directory(root)

        assert {p.name for p in code_files} == {"build.mjs", "loader.cjs"}

    def test_unrecognized_extension_is_counted_with_the_right_tally(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "one.xyz")
        _write(root / "two.xyz")
        _write(root / "three.xyz")

        _, _, _, unknown = cov.walk_directory(root)

        assert unknown == {".xyz": 3}

    def test_unknown_extensions_are_aggregated_not_itemized(self, tmp_path):
        """105 files of one unrecognized extension must not become 105 entries."""
        root = tmp_path / "assets_weird"
        for i in range(105):
            _write(root / f"item{i}.qux")

        _, _, _, unknown = cov.walk_directory(root)

        assert unknown == {".qux": 105}

    def test_asset_and_binary_extensions_do_not_count_as_unknown(self, tmp_path):
        root = tmp_path / "mixed"
        _write(root / "main.c")
        _write(root / "sprite.png")
        _write(root / "theme.ttf")
        _write(root / "clip.mp4")
        _write(root / "bundle.zip")
        _write(root / "lib.dll")
        _write(root / "yarn.lock")

        _, _, _, unknown = cov.walk_directory(root)

        assert unknown == {}

    def test_md_like_extensions_do_not_count_as_unknown(self, tmp_path):
        root = tmp_path / "docs_mixed"
        _write(root / "main.c")
        _write(root / "NOTES.md")
        _write(root / "notes.mdx")
        _write(root / "notes.rst")
        _write(root / "notes.txt")

        _, _, _, unknown = cov.walk_directory(root)

        assert unknown == {}

    def test_ordinary_all_recognized_subtree_has_no_unknown_extensions(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "CLAUDE.md", "# root\n")
        _write(repo / "engine" / "main.c")
        _write(repo / "engine" / "README.md")

        subject = cov.build_subject(repo / "engine")

        assert subject["unknownExtensions"] == {}


class TestRootExclusion:
    def test_named_vendored_root_is_reported_not_silently_scanned(self, tmp_path):
        """walk_subtree tests what it descends into, never the root it was handed."""
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "vendor" / "lib.c")

        subject = cov.build_subject(repo / "vendor")

        assert subject["rootExclusion"] == cov.SKIP_VENDORED
        # Honoured, because the user named it explicitly -- but reported.
        assert subject["codeFiles"]

    def test_ordinary_root_has_no_exclusion(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "engine" / "main.c")

        assert cov.build_subject(repo / "engine")["rootExclusion"] is None


class TestBuildSubject:
    def test_subject_shape(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "CLAUDE.md", "# root\n")
        _write(repo / "engine" / "main.c")

        subject = cov.build_subject(repo / "engine")

        assert set(subject) == {
            "root", "rootExclusion", "codeFiles", "ambientClaudeMdPaths",
            "skipped", "noisePruned", "unknownExtensions",
        }
        assert subject["codeFiles"] and subject["codeFiles"][0].endswith("main.c")
        assert len(subject["ambientClaudeMdPaths"]) == 1

    def test_uncovered_subtree_reports_an_empty_chain(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "engine" / "main.c")

        subject = cov.build_subject(repo / "engine")

        assert subject["codeFiles"]
        assert subject["ambientClaudeMdPaths"] == []


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
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, text=True)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _write(repo / "engine" / "main.c", "int a;\n")
    _write(repo / "tools" / "run.py", "a = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


class TestDiffRoots:
    """diff_roots had no tests in its first revision and carried three bugs.

    `git diff --name-only` prints WORKTREE-ROOT-relative paths, C-quotes
    non-ASCII names by default, and will happily read a leading-dash range as a
    flag. All three are silent: they produce an empty or short subject list
    rather than an error.
    """

    def test_names_resolve_when_invoked_from_a_subdirectory(self, git_repo):
        """The bug: names joined to -C's dir instead of the worktree root."""
        (git_repo / "engine" / "main.c").write_text("int a; int b;\n", encoding="utf-8")

        roots, notes = cov.diff_roots(git_repo / "tools", None)

        assert notes == []
        assert [p.name for p in roots] == ["engine"]

    def test_non_ascii_filename_is_not_dropped(self, git_repo):
        _write(git_repo / "engine" / "café.c", "int a;\n")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add unicode")
        (git_repo / "engine" / "café.c").write_text("int a; int b;\n", encoding="utf-8")

        roots, _ = cov.diff_roots(git_repo, None)

        assert [p.name for p in roots] == ["engine"]

    def test_option_shaped_range_does_not_write(self, git_repo, tmp_path):
        """A range is user input; it must never be read as a git flag."""
        target = tmp_path / "should-not-exist.txt"

        roots, notes = cov.diff_roots(git_repo, f"--output={target}")

        assert not target.exists()
        assert roots == []
        assert notes and "git diff failed" in notes[0]

    def test_vendored_diff_roots_are_excluded_and_counted(self, git_repo):
        _write(git_repo / "vendor" / "dep.c", "int a;\n")
        _git(git_repo, "add", "-A")
        _git(git_repo, "commit", "-qm", "add vendor")
        (git_repo / "vendor" / "dep.c").write_text("int a; int b;\n", encoding="utf-8")
        (git_repo / "engine" / "main.c").write_text("int a; int b;\n", encoding="utf-8")

        roots, notes = cov.diff_roots(git_repo, None)

        assert [p.name for p in roots] == ["engine"]
        assert notes and "vendored or generated" in notes[0]

    def test_outside_a_worktree_reports_rather_than_guessing(self, tmp_path):
        roots, notes = cov.diff_roots(tmp_path, None)

        assert roots == []
        assert notes and "git worktree" in notes[0]


class TestCli:
    def _run(self, *args, cwd: Path):
        return subprocess.run(
            [sys.executable, str(DISCOVER_PATH), *args],
            capture_output=True, text=True, cwd=str(cwd),
        )

    def test_no_arguments_refuses_rather_than_scanning_the_repo(self, tmp_path):
        """There is no whole-repo default, deliberately."""
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
        _write(repo / "CLAUDE.md", "# root\n")
        _write(repo / "engine" / "main.c")

        result = self._run("engine", "--json", cwd=repo)

        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert len(payload["subjects"]) == 1
        assert payload["subjects"][0]["codeFiles"][0].endswith("main.c")

    def test_text_output_names_the_null_chain_explicitly(self, tmp_path):
        repo = _mkrepo(tmp_path / "repo")
        _write(repo / "engine" / "main.c")

        result = self._run("engine", cwd=repo)

        assert result.returncode == 0
        assert "NONE" in result.stdout


@pytest.fixture(autouse=True)
def _clear_vcs_detection_cache():
    """Detection is cached per directory for the life of the process; a test that
    creates a repository where a previous test saw none must not inherit the old
    answer. Both module copies are cleared -- discover_coverage imports its own
    instance of vcs_ignore through the scripts dir.
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


class TestVcsIgnorePredicate:
    """Three VCS states, and the third is the one that must exclude NOTHING.

    A tree under no version control has no ignore information; inventing some
    would silently drop a real coverage subject. Every failure path degrades to
    the same empty answer.
    """

    def test_git_project_is_detected(self, git_repo):
        assert vcs.detect_vcs(git_repo) == vcs.GIT

    def test_no_vcs_is_detected_as_none_and_excludes_nothing(self, tmp_path):
        plain = tmp_path / "plain"
        _write(plain / "main.c")

        assert vcs.detect_vcs(plain) is None
        assert vcs.ignored_paths([plain / "main.c"], root=plain) == set()

    def test_p4_project_is_detected_from_a_config_marker(self, tmp_path):
        """Filesystem-only detection, deliberately: `p4 info` contacts a server,
        so a machine with P4PORT set and no reachable server would pay a timeout
        per directory and a wrong answer would look like a slow one.
        """
        proj = tmp_path / "depot_ws"
        _write(proj / "main.c")
        _write(proj / ".p4config", "P4CLIENT=ws\n")

        assert vcs.detect_vcs(proj) == vcs.P4

    def test_p4_query_failure_excludes_nothing(self, tmp_path, monkeypatch):
        """The p4 branch is UNVERIFIED, so it must fail toward including the
        subject: a missing binary, a non-zero exit, or an unparseable line
        yields no exclusion at all.
        """
        proj = tmp_path / "depot_ws"
        _write(proj / "main.c")
        _write(proj / ".p4config", "P4CLIENT=ws\n")
        seen = {}

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            raise OSError("p4 not installed")

        monkeypatch.setattr(vcs.subprocess, "run", _fake_run)

        assert vcs.ignored_paths([proj / "main.c"], root=proj, vcs=vcs.P4) == set()
        assert seen["cmd"][:1] == ["p4"] and "ignores" in seen["cmd"]

    def test_git_ignored_directory_is_reported(self, git_repo):
        _write(git_repo / ".gitignore", "tmpwork/\n")
        _write(git_repo / "tmpwork" / "scratch.py")

        ignored = vcs.ignored_paths(
            [git_repo / "tmpwork", git_repo / "engine"], root=git_repo
        )

        assert ignored == {git_repo / "tmpwork"}
        assert vcs.is_ignored(git_repo / "tmpwork")
        assert not vcs.is_ignored(git_repo / "engine")

    def test_no_index_is_required_for_a_tracked_but_ignored_path(self, git_repo):
        """The question is whether the ignore RULES cover the path. Plain
        `check-ignore` consults the index first and calls a TRACKED path
        not-ignored, which is the opposite answer.
        """
        _write(git_repo / ".gitignore", "forced/\n")
        _write(git_repo / "forced" / "kept.py")
        _git(git_repo, "add", "-f", "forced/kept.py")
        _git(git_repo, "commit", "-qm", "force-add an ignored path")

        assert vcs.is_ignored(git_repo / "forced")
        assert vcs.is_ignored(git_repo / "forced" / "kept.py")

    def test_one_subprocess_covers_the_whole_batch(self, git_repo, monkeypatch):
        """A tree walk asks about hundreds of paths; one subprocess per path is
        the difference between a discovery step and a stall.
        """
        # The directory must EXIST: a trailing-slash pattern matches directories
        # only, and git cannot classify a path that is not on disk.
        _write(git_repo / ".gitignore", "tmpwork/\n")
        _write(git_repo / "tmpwork" / "scratch.py")
        calls = []
        real_run = vcs.subprocess.run

        def _counting_run(cmd, **kwargs):
            calls.append(cmd)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(vcs.subprocess, "run", _counting_run)

        ignored = vcs.ignored_paths(
            [git_repo / f"d{i}" for i in range(20)] + [git_repo / "tmpwork"],
            root=git_repo,
            vcs=vcs.GIT,
        )

        assert ignored == {git_repo / "tmpwork"}
        # One check-ignore for 21 paths. The rev-parse that finds the worktree
        # root is fixed-cost and cached, so only this count scales with input.
        assert len([c for c in calls if "check-ignore" in c]) == 1

    def test_the_worktree_root_is_never_reported_ignored(self, git_repo):
        """A repository cannot be excluded from itself, and git's answer for the
        root under --no-index is actively wrong: the root's repo-relative path is
        empty and matches a BLANK LINE in .gitignore. Observed live on git
        2.55.0.windows.3 -- a real project's root reported ignored by
        `.gitignore:55` with an empty pattern. Guarded, not tolerated: the false
        positive would have declared every file in the project excluded.
        """
        _write(git_repo / ".gitignore", "a\n\nb\n\n")

        assert not vcs.is_ignored(git_repo)
        assert vcs.ignored_paths([git_repo], root=git_repo) == set()
        assert cov.build_subject(git_repo)["rootExclusion"] is None

    def test_empty_input_spends_no_subprocess(self, git_repo, monkeypatch):
        def _explode(*args, **kwargs):
            raise AssertionError("no subprocess for an empty batch")

        monkeypatch.setattr(vcs.subprocess, "run", _explode)

        assert vcs.ignored_paths([], root=git_repo) == set()


class TestIgnoredPathsShapeTheSubject:
    def test_ignored_files_are_excluded_and_recorded(self, git_repo):
        _write(git_repo / ".gitignore", "generated_api.py\n")
        _write(git_repo / "engine" / "generated_api.py")

        subject = cov.build_subject(git_repo / "engine")

        assert [Path(p).name for p in subject["codeFiles"]] == ["main.c"]
        assert {
            (Path(e["path"]).name, e["reason"]) for e in subject["skipped"]
        } == {("generated_api.py", cov.SKIP_IGNORED)}

    def test_ignored_child_directory_is_reported_not_silent(self, git_repo):
        _write(git_repo / ".gitignore", "engine/scratch/\n")
        _write(git_repo / "engine" / "scratch" / "throwaway.c")

        subject = cov.build_subject(git_repo / "engine")

        assert (
            {"path": str(git_repo / "engine" / "scratch"), "reason": cov.SKIP_IGNORED}
            in subject["skipped"]
        )

    def test_named_ignored_root_is_honoured_but_reported(self, git_repo):
        """Same posture as a vendored root: the user asked for it by name."""
        _write(git_repo / ".gitignore", "tmpwork/\n")
        _write(git_repo / "tmpwork" / "scratch.py")

        subject = cov.build_subject(git_repo / "tmpwork")

        assert subject["rootExclusion"] == cov.SKIP_IGNORED
        assert [Path(p).name for p in subject["codeFiles"]] == ["scratch.py"]

    def test_no_vcs_excludes_nothing_from_the_subject(self, tmp_path):
        plain = tmp_path / "plain"
        _write(plain / "main.c")

        subject = cov.build_subject(plain)

        assert [Path(p).name for p in subject["codeFiles"]] == ["main.c"]
        assert subject["rootExclusion"] is None
        assert subject["skipped"] == []


class TestWalkTreeStillRecurses:
    """`walk_tree` is the RECURSIVE primitive discover_composition.py consumes,
    and it MUST keep recursing: its job is to enumerate every code-bearing
    directory in a tree. Making the coverage SUBJECT non-recursive
    (`walk_directory`) must not reach it.
    """

    def _tree(self, tmp_path):
        root = _mkrepo(tmp_path / "repo")
        _write(root / "top.js")
        _write(root / "engine" / "physics.js")
        _write(root / "engine" / "solver" / "deep.js")
        _write(root / "ui" / "panel.js")
        _write(root / "docs" / "notes.md")  # not a leaf: no code
        return root

    def test_leaf_enumeration_reaches_every_depth(self, tmp_path):
        root = self._tree(tmp_path)

        leaves, _, _, _ = cov.walk_tree(root)

        assert {Path(p).name for p in leaves} == {"repo", "engine", "solver", "ui"}

    def test_walk_tree_and_walk_directory_disagree_on_purpose(self, tmp_path):
        """The contrast IS the fix: one unit is a tree, the other a directory."""
        root = self._tree(tmp_path)

        leaves, _, _, _ = cov.walk_tree(root)
        code_files, _, _, _ = cov.walk_directory(root)

        assert len(leaves) == 4
        assert [p.name for p in code_files] == ["top.js"]
