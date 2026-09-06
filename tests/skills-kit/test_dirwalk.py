"""Tests for skills_kit_lib.dirwalk -- the shared depth-limited walk extracted
from the two audit discover.py scripts (arch-review S12).
"""

from pathlib import Path

from skills_kit_lib.dirwalk import SKIP_DIR_NAMES, iter_dirs


def _mk(tmp_path: Path, *parts: str) -> Path:
    d = tmp_path.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _visited(tmp_path: Path, max_depth: int = 8) -> set:
    return {p for p, _ in iter_dirs(tmp_path, max_depth)}


class TestSkipRules:
    def test_skips_noise_dirs(self, tmp_path):
        for noise in ("node_modules", ".git", "__pycache__", "tmp", "Saved"):
            _mk(tmp_path, noise, "inner")
        _mk(tmp_path, "src")
        visited = _visited(tmp_path)
        assert tmp_path / "src" in visited
        for noise in ("node_modules", ".git", "__pycache__", "tmp", "Saved"):
            assert tmp_path / noise not in visited

    def test_skips_dot_dirs_except_dot_claude(self, tmp_path):
        _mk(tmp_path, ".hidden", "x")
        _mk(tmp_path, ".claude", "skills", "alpha")
        visited = _visited(tmp_path)
        assert tmp_path / ".hidden" not in visited
        assert tmp_path / ".claude" / "skills" / "alpha" in visited

    def test_skip_names_are_bare_components(self):
        # Guard against re-introducing path-like entries (".claude/plugins"
        # was dead: os.walk dir names never contain a separator).
        for name in SKIP_DIR_NAMES:
            assert "/" not in name and "\\" not in name


class TestIncludeDirsOverride:
    """include_dirs lets a caller opt a normally-pruned name back in -- for a
    consumer whose own first-party directory happens to share a noise name
    (e.g. `Build/`, `tmp/`)."""

    def test_named_dir_is_visited_when_included(self, tmp_path):
        _mk(tmp_path, "tmp", "inner")
        visited_default = _visited(tmp_path)
        assert tmp_path / "tmp" not in visited_default

        visited_included = {
            p for p, _ in iter_dirs(tmp_path, 8, include_dirs=["tmp"])
        }
        assert tmp_path / "tmp" in visited_included
        assert tmp_path / "tmp" / "inner" in visited_included

    def test_other_noise_names_still_pruned_when_one_is_included(self, tmp_path):
        _mk(tmp_path, "tmp", "inner")
        _mk(tmp_path, ".git", "inner")
        visited = {p for p, _ in iter_dirs(tmp_path, 8, include_dirs=["tmp"])}
        assert tmp_path / "tmp" in visited
        assert tmp_path / ".git" not in visited


class TestSkippedOut:
    """A caller can ask for the pruned directories themselves, so a discover
    script can report what it silently walked past instead of dropping the
    information on the floor."""

    def test_pruned_directories_are_recorded(self, tmp_path):
        _mk(tmp_path, "tmp", "inner")
        _mk(tmp_path, "src")
        skipped: list = []
        list(iter_dirs(tmp_path, 8, skipped_out=skipped))
        assert tmp_path / "tmp" in skipped
        assert tmp_path / "src" not in skipped

    def test_included_dir_is_not_recorded_as_skipped(self, tmp_path):
        _mk(tmp_path, "tmp", "inner")
        skipped: list = []
        list(iter_dirs(tmp_path, 8, include_dirs=["tmp"], skipped_out=skipped))
        assert tmp_path / "tmp" not in skipped


class TestDepthLimit:
    def test_prunes_below_max_depth(self, tmp_path):
        _mk(tmp_path, "a", "b", "c")
        visited = _visited(tmp_path, max_depth=2)
        assert tmp_path / "a" / "b" in visited
        assert tmp_path / "a" / "b" / "c" not in visited

    def test_cwd_is_yielded_with_its_files(self, tmp_path):
        (tmp_path / "SKILL.md").write_text("x", encoding="utf-8")
        results = dict(iter_dirs(tmp_path, 1))
        assert tmp_path in results
        assert "SKILL.md" in results[tmp_path]
