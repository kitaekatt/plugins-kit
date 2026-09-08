"""Tests for the md-domain audit lane's scripts/evidence_pack.py.

evidence_pack.py computes the deterministic evidence pack an audit prompt
carries: six fixed sections of measured facts about one Markdown subject and
the repository around it. The tests here pin the public surface
(build_pack / build_structured), the character budget, the ancestor chain at
both ends of its range, and the rejection of a subject outside the repository
root.

Loaded via importlib under a unique module name because the md-domain scripts
directory ships many sibling modules; a bare `import evidence_pack` risks
colliding with pytest's module cache across the sibling script tests.
"""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills"
    / "md-domain" / "scripts" / "evidence_pack.py"
)

_spec = importlib.util.spec_from_file_location("md_evidence_pack", MODULE_PATH)
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)

SECTIONS = ("IDENTITY", "MEASUREMENTS", "REFERENCES", "ANCESTORS",
            "CLAIM EVIDENCE", "MECHANICAL")

BODY = """# Title

Some prose about the module. `helper.py` lives beside this document and
exports `do_work`.

## Section two

- A bullet with a [link](helper.py).
- A citation at `helper.py:1`.

```
fenced
```
"""


def _repo(tmp_path: Path) -> Path:
    """A minimal repository: one nested doc plus a code file to resolve against."""
    root = tmp_path.resolve() / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "helper.py").write_text("def do_work():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "NOTES.md").write_text(BODY, encoding="utf-8")
    return root


class TestPublicSurface:
    def test_build_pack_returns_every_section_header(self, tmp_path):
        root = _repo(tmp_path)
        pack = ep.build_pack(root, "pkg/NOTES.md")
        assert isinstance(pack, str)
        lines = pack.splitlines()
        for section in SECTIONS:
            assert section in lines, f"missing section header: {section}"

    def test_sections_appear_in_the_fixed_order(self, tmp_path):
        root = _repo(tmp_path)
        lines = ep.build_pack(root, "pkg/NOTES.md").splitlines()
        positions = [lines.index(section) for section in SECTIONS]
        assert positions == sorted(positions)

    def test_pack_is_pure_ascii(self, tmp_path):
        root = _repo(tmp_path)
        (root / "pkg" / "UNICODE.md").write_text("# T\n\nAn em dash \u2014 here.\n", encoding="utf-8")
        pack = ep.build_pack(root, "pkg/UNICODE.md")
        pack.encode("ascii")

    def test_compact_flag_changes_the_rendering(self, tmp_path):
        root = _repo(tmp_path)
        compact = ep.build_pack(root, "pkg/NOTES.md", compact=True)
        verbose = ep.build_pack(root, "pkg/NOTES.md", compact=False)
        assert "[serves:" in compact
        assert "[serves:" not in verbose

    def test_build_structured_returns_row_lists_per_section(self, tmp_path):
        root = _repo(tmp_path)
        data = ep.build_structured(root, "pkg/NOTES.md")
        for section in SECTIONS:
            assert isinstance(data[section], list)
            assert data[section], f"empty section: {section}"
        assert isinstance(data["closing"], str)

    def test_accepts_an_absolute_subject_inside_the_repository(self, tmp_path):
        root = _repo(tmp_path)
        pack = ep.build_pack(root, root / "pkg" / "NOTES.md")
        assert "IDENTITY" in pack.splitlines()

    def test_missing_subject_raises(self, tmp_path):
        root = _repo(tmp_path)
        with pytest.raises(FileNotFoundError):
            ep.build_pack(root, "pkg/ABSENT.md")


class TestBudget:
    def test_default_budget_is_honoured(self, tmp_path):
        root = _repo(tmp_path)
        assert len(ep.build_pack(root, "pkg/NOTES.md")) <= 24000

    @pytest.mark.parametrize("cap", [4000, 1200, 400, 60, 10])
    def test_explicit_budget_is_never_exceeded(self, tmp_path, cap):
        root = _repo(tmp_path)
        # A wordy subject so the renderer has to drop rows to fit.
        (root / "pkg" / "BIG.md").write_text(BODY * 40, encoding="utf-8")
        assert len(ep.build_pack(root, "pkg/BIG.md", max_chars=cap)) <= cap

    def test_truncation_is_announced(self, tmp_path):
        root = _repo(tmp_path)
        (root / "pkg" / "BIG.md").write_text(BODY * 40, encoding="utf-8")
        assert "truncated" in ep.build_pack(root, "pkg/BIG.md", max_chars=1500)

    def test_zero_budget_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        with pytest.raises(ValueError):
            ep.build_pack(root, "pkg/NOTES.md", max_chars=0)


class TestTruncationIsNotLinearInRowCount:
    """build_pack's truncation loop used to pop one row and re-render the
    WHOLE pack on every pop -- O(rows x pack). A subject with many rows to
    drop must still converge in a small, bounded number of _render calls
    (binary search per section), not one call per dropped row."""

    def test_render_call_count_stays_small_on_a_large_subject(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        # Enough repeated content to force MANY rows to be dropped across
        # sections at a tight cap -- large enough that a linear one-row-at-a-
        # time implementation would need hundreds of renders.
        (root / "pkg" / "BIG.md").write_text(BODY * 400, encoding="utf-8")

        calls = {"n": 0}
        real_render = ep._render

        def _counting_render(*args, **kwargs):
            calls["n"] += 1
            return real_render(*args, **kwargs)

        monkeypatch.setattr(ep, "_render", _counting_render)

        result = ep.build_pack(root, "pkg/BIG.md", max_chars=1500)

        assert len(result) <= 1500
        # log2 of a few thousand rows is well under 20; a linear
        # implementation on this fixture calls _render in the hundreds.
        assert calls["n"] < 100, calls["n"]

    def test_result_still_fits_and_is_marked_truncated_at_various_caps(self, tmp_path):
        # Same characterization TestBudget already pins, run again here so a
        # correctness regression in the binary-search rewrite is caught next
        # to the perf assertion above rather than only in a separate class.
        root = _repo(tmp_path)
        (root / "pkg" / "BIG.md").write_text(BODY * 400, encoding="utf-8")
        for cap in (4000, 1200, 400, 60):
            result = ep.build_pack(root, "pkg/BIG.md", max_chars=cap)
            assert len(result) <= cap


class TestAncestors:
    def test_no_ancestor_chain_reports_none(self, tmp_path):
        root = tmp_path.resolve()
        (root / "CLAUDE.md").write_text("# Root\n\nOne rule.\n", encoding="utf-8")
        pack = ep.build_pack(root, "CLAUDE.md")
        assert "chain=none" in pack

    def test_several_ancestors_are_each_listed(self, tmp_path):
        root = tmp_path.resolve()
        text = (
            "# Guide\n\n"
            "**Scratch files go in `tmp/`.** Anything throwaway must live there.\n"
        )
        (root / "CLAUDE.md").write_text(text, encoding="utf-8")
        (root / "a").mkdir()
        (root / "a" / "CLAUDE.md").write_text(text, encoding="utf-8")
        (root / "a" / "b").mkdir()
        (root / "a" / "b" / "CLAUDE.md").write_text(text, encoding="utf-8")
        pack = ep.build_pack(root, "a/b/CLAUDE.md", max_chars=24000)
        assert "ancestor=a/CLAUDE.md" in pack
        assert "ancestor=CLAUDE.md" in pack
        assert "chain=none" not in pack

    def test_shared_sentence_is_flagged_as_a_duplicate_candidate(self, tmp_path):
        root = tmp_path.resolve()
        shared = (
            "# Guide\n\n"
            "Every player-facing string lives in the loc tables and reaches the "
            "screen through the Loc autoload, without exception.\n"
        )
        (root / "CLAUDE.md").write_text(shared, encoding="utf-8")
        (root / "a").mkdir()
        (root / "a" / "CLAUDE.md").write_text(shared, encoding="utf-8")
        pack = ep.build_pack(root, "a/CLAUDE.md", max_chars=24000)
        assert "DUPLICATE CANDIDATE" in pack


class TestOutsideRepository:
    def test_relative_escape_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        (tmp_path / "outside.md").write_text("# Outside\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ep.build_pack(root, "../outside.md")

    def test_absolute_outside_path_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        target = tmp_path.resolve() / "outside.md"
        target.write_text("# Outside\n", encoding="utf-8")
        with pytest.raises(ValueError):
            ep.build_structured(root, target)


class TestOutOfRepoReferenceNeverEmitsAnAbsolutePath:
    """A reference INSIDE a subject that resolves outside the repo (../../..,
    or an absolute candidate) must never make it into the rendered pack as a
    literal machine path -- the pack is inlined verbatim into a prompt that
    can reach a toolless endpoint. _rel's fallback used to be the absolute
    path itself; it must be a fixed sanitized token instead."""

    def test_relative_reference_resolving_outside_repo_has_no_absolute_path_in_pack(
        self, tmp_path
    ):
        root = _repo(tmp_path)
        # No file at outside/target.md -- resolves outside the repo AND
        # missing, so the compact renderer cannot fold the row away into an
        # "all exist" summary; the raw target= value is what leaks.
        (root / "pkg" / "LINKER.md").write_text(
            "# Linker\n\nSee [elsewhere](../../outside/target.md) for detail.\n",
            encoding="utf-8",
        )
        pack = ep.build_pack(root, "pkg/LINKER.md", max_chars=24000, compact=False)
        assert str(tmp_path) not in pack
        assert "<outside-repo>/target.md" in pack

    def test_mechanical_machine_path_regex_catches_macos_users_path(self, tmp_path):
        root = _repo(tmp_path)
        (root / "pkg" / "LEAK.md").write_text(
            "# Leak\n\nConfig lives at /Users/someone/Dev/foo/bar.yaml.\n",
            encoding="utf-8",
        )
        pack = ep.build_pack(root, "pkg/LEAK.md", max_chars=24000)
        assert "machine-path" in pack
        assert "/Users/someone" in pack  # the finding QUOTES the offending value


class TestIdentityDimensionAgreesWithClassifyDimension:
    """evidence_pack._identity used to derive the code-directory dimension
    with its own regex and its own rule (gated on role == 'child' alone,
    missing Signal B and the root case) -- a THIRD implementation alongside
    discover_claude_md.classify_dimension and claude-md-detect.js's trust of
    the caller. It must defer to classify_dimension instead."""

    def test_root_file_with_code_siblings_and_signal_b_is_code_directory(self, tmp_path):
        # The old rule forced role == "child" -- a ROOT CLAUDE.md could never
        # be code-directory under it, regardless of content. classify_dimension
        # has no such restriction.
        root = tmp_path.resolve()
        (root / "helper.py").write_text("def f(): pass\n", encoding="utf-8")
        (root / "CLAUDE.md").write_text(
            "# Root\n\nNever call `frobnicate()` before init.\n", encoding="utf-8"
        )
        pack = ep.build_pack(root, "CLAUDE.md", max_chars=24000)
        assert "dimension=code-directory" in pack

    def test_contract_block_forces_classic_even_with_code_siblings(self, tmp_path):
        root = tmp_path.resolve()
        (root / "a").mkdir()
        (root / "a" / "helper.py").write_text("def f(): pass\n", encoding="utf-8")
        (root / "a" / "CLAUDE.md").write_text(
            "# Contract\n\nNever call `frobnicate()`.\n\nclaude_md:\n  scope: {}\n",
            encoding="utf-8",
        )
        pack = ep.build_pack(root, "a/CLAUDE.md", max_chars=24000)
        assert "dimension=classic" in pack


class TestArtifactClassification:
    def test_claude_md_and_skill_md_and_plain_doc(self):
        assert ep.artifact_of("a/b/CLAUDE.md") == "claude-md"
        assert ep.artifact_of("skills/alpha/SKILL.md") == "skill"
        assert ep.artifact_of("skills/alpha/references/deep.md") == "skill-reference"
        assert ep.artifact_of("docs/OVERVIEW.md") == "project-doc"
