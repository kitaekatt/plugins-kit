"""Tests for project-doc-audit/scripts/discover.py.

discover.py enumerates standalone PROJECT DOCUMENTS (plain_md outside any skill
references/ folder and outside the CLAUDE.md hierarchy) and computes the
mechanical signals the audit lanes consume: kind classification, size, and the
inbound-citation count that powers orphan detection.

Loaded via importlib under a unique module name because claude-md-audit ships a
sibling `discover.py`; a bare `import discover` would be ambiguous across the two.
"""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DISCOVER_PATH = (
    REPO_ROOT / "plugins" / "skills-kit" / "skills"
    / "project-doc-audit" / "scripts" / "discover.py"
)

_spec = importlib.util.spec_from_file_location("pd_discover", DISCOVER_PATH)
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)


def _write(path: Path, text: str = "# doc\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestIsDocFile:
    def test_markdown_txt_markdeep_are_docs(self):
        assert pd.is_doc_file("overview.md")
        assert pd.is_doc_file("notes.txt")
        assert pd.is_doc_file("QuestSystem.md.html")
        assert pd.is_doc_file("api.rst")

    def test_code_and_config_are_not_docs(self):
        assert not pd.is_doc_file("script.py")
        assert not pd.is_doc_file("config.yaml")
        assert not pd.is_doc_file("data.json")


class TestClassifyKind:
    def test_project_doc_at_project_path(self, tmp_path):
        p = tmp_path / "Docs" / "Overview.md"
        _write(p)
        assert pd.classify_kind(p) == "project_doc"

    def test_skill_reference_inside_references_folder(self, tmp_path):
        p = tmp_path / "plugins" / "x" / "skills" / "y" / "references" / "deep.md"
        _write(p)
        assert pd.classify_kind(p) == "skill_reference"

    def test_skill_md_and_claude_md_are_other_artifacts(self, tmp_path):
        assert pd.classify_kind(tmp_path / "skills" / "y" / "SKILL.md") == "other_claude_artifact"
        assert pd.classify_kind(tmp_path / "CLAUDE.md") == "other_claude_artifact"
        assert pd.classify_kind(tmp_path / "sub" / "CLAUDE.local.md") == "other_claude_artifact"


class TestCollectCandidates:
    def test_finds_project_docs_and_skips_vendored(self, tmp_path):
        _write(tmp_path / "Docs" / "a.md")
        _write(tmp_path / ".claude" / "docs" / "b.md")
        _write(tmp_path / "node_modules" / "pkg" / "README.md")  # vendored -> skipped
        _write(tmp_path / "src" / "main.py")  # not a doc
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert "a.md" in found
        assert "b.md" in found
        assert "README.md" not in found  # node_modules excluded
        assert "main.py" not in found

    def test_skips_generated_and_build_dirs(self, tmp_path):
        _write(tmp_path / "Intermediate" / "gen.md")
        _write(tmp_path / "Docs" / "real.md")
        found = {p.name for p in pd.collect_candidates(tmp_path)}
        assert found == {"real.md"}


class TestOrphanDetection:
    def test_uncited_doc_is_orphan(self, tmp_path):
        orphan = tmp_path / "Docs" / "lonely.md"
        _write(orphan)
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(orphan, inbound, tmp_path)
        assert rec["inbound_citations"] == 0
        assert rec["kind"] == "project_doc"

    def test_cited_doc_is_not_orphan(self, tmp_path):
        cited = tmp_path / "Docs" / "useful.md"
        _write(cited)
        # A CLAUDE.md that points at the doc by basename.
        _write(tmp_path / "CLAUDE.md", "See Docs/useful.md when working on X.\n")
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(cited, inbound, tmp_path)
        assert rec["inbound_citations"] >= 1
        assert any("CLAUDE.md" in c for c in rec["cited_by"])

    def test_self_mention_does_not_count_as_citation(self, tmp_path):
        # A doc that mentions its own basename must not cite itself.
        doc = tmp_path / "Docs" / "selfref.md"
        _write(doc, "This file selfref.md documents itself.\n")
        cands = pd.collect_candidates(tmp_path)
        inbound = pd.build_inbound_index(tmp_path, cands)
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["inbound_citations"] == 0


class TestMeasure:
    def test_counts_effective_lines_and_tokens(self, tmp_path):
        doc = tmp_path / "Docs" / "sized.md"
        _write(doc, "line one\nline two\n\n\n")  # trailing blanks dropped
        lines, approx_tokens = pd._measure(doc)
        assert lines == 2
        assert approx_tokens > 0


class TestProjectRootCiterScope:
    """The orphan scan must cover the whole project even when auditing a
    subdirectory -- otherwise a .claude/docs doc cited from the root CLAUDE.md
    would false-positive as an orphan. find_project_root scopes the citer scan."""

    def test_find_project_root_finds_git_ancestor(self, tmp_path):
        (tmp_path / ".git").mkdir()
        deep = tmp_path / ".claude" / "docs"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) == tmp_path

    def test_find_project_root_finds_perforce_marker(self, tmp_path):
        # Non-git project: Perforce workspace marker must also resolve.
        (tmp_path / ".p4config.txt").write_text("P4USER=x\n", encoding="utf-8")
        deep = tmp_path / "SpiritCrossing" / "Source"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) == tmp_path

    def test_find_project_root_none_without_marker(self, tmp_path):
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        assert pd.find_project_root(deep) is None

    def test_orphan_scan_from_project_root_sees_distant_citer(self, tmp_path):
        # Doc in .claude/docs, cited only from a CLAUDE.md at the project root.
        (tmp_path / ".git").mkdir()
        doc = tmp_path / ".claude" / "docs" / "guide.md"
        _write(doc)
        _write(tmp_path / "CLAUDE.md", "For details see .claude/docs/guide.md\n")
        # Candidate is just the doc; citer scan rooted at the project root finds
        # the distant citer, so the doc is NOT flagged as an orphan.
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["inbound_citations"] >= 1


class TestGeneratedArtifactSignals:
    """generated_artifact role detection (stress-test gap 6): a sidecar
    generation record or an in-file marker identifies a committed generated
    output, which the audit then checks for provenance ONLY."""

    def test_params_sidecar_detected(self, tmp_path):
        doc = tmp_path / "renderings" / "top50-approaches.md"
        _write(doc, "# Top 50\n\ncontent\n")
        _write(doc.with_name("top50-approaches.params.json"), '{"query": "top50"}\n')
        rec = pd.generation_record(doc)
        assert rec == "sidecar:top50-approaches.params.json"

    def test_markdeep_compound_suffix_sidecar_detected(self, tmp_path):
        doc = tmp_path / "Docs" / "report.md.html"
        _write(doc, "<meta>\n")
        _write(doc.with_name("report.params.json"), "{}\n")
        assert pd.generation_record(doc) == "sidecar:report.params.json"

    def test_in_file_marker_detected(self, tmp_path):
        doc = tmp_path / "docs" / "analysis.md"
        _write(doc, "# Analysis\n\nThis document is generated analysis, exempt.\n")
        rec = pd.generation_record(doc)
        assert rec is not None and rec.startswith("marker:")

    def test_marker_beyond_head_is_ignored(self, tmp_path):
        doc = tmp_path / "docs" / "handwritten.md"
        _write(doc, "# Doc\n" + "\n" * 30 + "mentions generated by tooling later\n")
        assert pd.generation_record(doc) is None

    def test_plain_doc_has_no_signal(self, tmp_path):
        doc = tmp_path / "docs" / "notes.md"
        _write(doc, "# Notes\n\nHand-written notes.\n")
        assert pd.generation_record(doc) is None

    def test_describe_carries_generated_fields(self, tmp_path):
        (tmp_path / ".git").mkdir()
        doc = tmp_path / "renderings" / "out.md"
        _write(doc, "# Out\n")
        _write(doc.with_name("out.params.json"), "{}\n")
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["generated"] is True
        assert rec["generation_record"] == "sidecar:out.params.json"


class TestReadmeRoleHint:
    """readme_md role detection (stress-test gap 2): READMEs are judged as the
    human-facing derived brief, not as generic project docs."""

    def test_readme_md_flagged(self, tmp_path):
        doc = tmp_path / "README.md"
        _write(doc)
        assert pd.role_hint(doc) == "readme"

    def test_readme_case_insensitive(self, tmp_path):
        assert pd.role_hint(tmp_path / "readme.md") == "readme"
        assert pd.role_hint(tmp_path / "Readme.txt") == "readme"

    def test_other_docs_have_no_hint(self, tmp_path):
        assert pd.role_hint(tmp_path / "guide.md") is None
        assert pd.role_hint(tmp_path / "readme-first-draft-notes.md") is None

    def test_describe_carries_role_hint(self, tmp_path):
        (tmp_path / ".git").mkdir()
        doc = tmp_path / "README.md"
        _write(doc, "# Project\n")
        inbound = pd.build_inbound_index(tmp_path, [doc])
        rec = pd.describe(doc, inbound, tmp_path)
        assert rec["role_hint"] == "readme"
