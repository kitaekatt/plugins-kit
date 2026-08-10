"""Tests for the coverage verb's scripts/discover_coverage.py.

discover_coverage.py resolves the coverage subject -- (code subtree, its ambient
CLAUDE.md chain) -- and applies the STRUCTURAL exclusions. It is the mechanical
half of the verb and decides nothing about what the code means, so it is fully
testable ahead of the analysis criteria.

Two behaviours are pinned here because they are the ones the design turns on and
the ones a plausible-looking implementation gets wrong:

  * The ambient chain INCLUDES a CLAUDE.md at the subtree root. The document
    lanes' resolver deliberately starts at the target's PARENT (the target being
    the CLAUDE.md itself); reusing that convention here would drop the single
    most ambient file for a directory subject.
  * A subtree with NO ambient CLAUDE.md returns an EMPTY chain rather than
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
DISCOVER_PATH = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills"
    / "md-domain" / "scripts" / "discover_coverage.py"
)

_spec = importlib.util.spec_from_file_location("cov_discover", DISCOVER_PATH)
cov = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cov)


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


class TestWalkSubtree:
    def test_collects_code_files_and_ignores_docs(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "util.py")
        _write(root / "README.md")

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c", "util.py"}
        assert skipped == []

    def test_vendored_directories_are_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "node_modules" / "dep.js")
        _write(root / "vendor" / "lib.c")

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_VENDORED}
        assert _skipped_names({"skipped": skipped}) == {"node_modules", "vendor"}

    def test_generated_directories_are_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "generated" / "schema_pb2.py")

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_GENERATED}

    def test_nested_repository_is_skipped_and_reported(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _mkrepo(root / "submodule")
        _write(root / "submodule" / "inner.c")

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert _reasons({"skipped": skipped}) == {cov.SKIP_NESTED_REPO}

    def test_noise_directories_are_pruned_without_itemizing(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "__pycache__" / "main.cpython-312.pyc")
        _write(root / ".venv" / "lib.py")

        code_files, skipped, noise, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert skipped == []
        assert noise == 2

    def test_walk_reads_no_file_contents(self, tmp_path, monkeypatch):
        """Structural exclusions are applied before anything is read."""
        root = tmp_path / "src"
        _write(root / "main.c", "int main(void){return 0;}\n")

        def _explode(*args, **kwargs):
            raise AssertionError("walk_subtree must not read file contents")

        monkeypatch.setattr(Path, "read_text", _explode)
        monkeypatch.setattr(Path, "read_bytes", _explode)

        code_files, _, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs privileges on Windows"
)
class TestSymlinks:
    def test_symlink_resolving_outside_the_subtree_is_skipped(self, tmp_path):
        outside = tmp_path / "outside"
        _write(outside / "foreign.c")
        root = tmp_path / "src"
        _write(root / "main.c")
        (root / "linked").symlink_to(outside, target_is_directory=True)

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.c"}
        assert cov.SKIP_SYMLINK_OUT in _reasons({"skipped": skipped})

    def test_circular_symlink_is_skipped_not_fatal(self, tmp_path):
        """Path.resolve() raises RuntimeError, not OSError, on a symlink loop.

        pathlib's check_eloop deliberately converts the ELOOP OSError into
        RuntimeError("Symlink loop from ..."), so catching OSError alone lets a
        circular symlink crash the whole walk instead of being recorded and
        skipped -- in the very branch that exists to keep the walk robust.
        """
        root = tmp_path / "src"
        _write(root / "main.c")
        (root / "loop_a").symlink_to(root / "loop_b", target_is_directory=True)
        (root / "loop_b").symlink_to(root / "loop_a", target_is_directory=True)

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert "main.c" in {p.name for p in code_files}
        assert cov.SKIP_SYMLINK_OUT in _reasons({"skipped": skipped})

    def test_symlink_staying_inside_the_subtree_is_followed(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "real" / "kept.c")
        (root / "alias").symlink_to(root / "real", target_is_directory=True)

        code_files, skipped, _, _ = cov.walk_subtree(root)

        assert "kept.c" in {p.name for p in code_files}
        assert cov.SKIP_SYMLINK_OUT not in _reasons({"skipped": skipped})

    def test_alias_of_an_already_walked_directory_yields_no_duplicates(self, tmp_path):
        """Two names for one real directory must not emit its files twice."""
        root = tmp_path / "src"
        _write(root / "real" / "kept.c")
        (root / "alias").symlink_to(root / "real", target_is_directory=True)

        code_files, _, _, _ = cov.walk_subtree(root)

        resolved = [p.resolve() for p in code_files]
        assert len(resolved) == len(set(resolved))
        assert [p.name for p in code_files].count("kept.c") == 1


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

        code_files, _, _, unknown = cov.walk_subtree(root)

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

        code_files, _, _, unknown = cov.walk_subtree(root)

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

        code_files, _, _, unknown = cov.walk_subtree(root)

        assert code_files == []
        assert unknown == {"": 2}

    def test_godot_scene_extension_is_recognized_as_code(self, tmp_path):
        root = tmp_path / "godot_project"
        _write(root / "main.tscn")

        code_files, _, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"main.tscn"}

    def test_mjs_and_cjs_are_recognized_as_code(self, tmp_path):
        root = tmp_path / "node_pkg"
        _write(root / "build.mjs")
        _write(root / "loader.cjs")

        code_files, _, _, _ = cov.walk_subtree(root)

        assert {p.name for p in code_files} == {"build.mjs", "loader.cjs"}

    def test_unrecognized_extension_is_counted_with_the_right_tally(self, tmp_path):
        root = tmp_path / "src"
        _write(root / "main.c")
        _write(root / "one.xyz")
        _write(root / "two.xyz")
        _write(root / "three.xyz")

        _, _, _, unknown = cov.walk_subtree(root)

        assert unknown == {".xyz": 3}

    def test_unknown_extensions_are_aggregated_not_itemized(self, tmp_path):
        """105 files of one unrecognized extension must not become 105 entries."""
        root = tmp_path / "assets_weird"
        for i in range(105):
            _write(root / f"item{i}.qux")

        _, _, _, unknown = cov.walk_subtree(root)

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

        _, _, _, unknown = cov.walk_subtree(root)

        assert unknown == {}

    def test_md_like_extensions_do_not_count_as_unknown(self, tmp_path):
        root = tmp_path / "docs_mixed"
        _write(root / "main.c")
        _write(root / "NOTES.md")
        _write(root / "notes.mdx")
        _write(root / "notes.rst")
        _write(root / "notes.txt")

        _, _, _, unknown = cov.walk_subtree(root)

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
