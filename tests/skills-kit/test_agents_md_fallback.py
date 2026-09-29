"""AGENTS.md fallback across the md-domain lanes.

Precedence per directory: CLAUDE.md wins; AGENTS.md is read only when the
directory has no CLAUDE.md; a shadowed AGENTS.md is ignored. Cases covered:
CLAUDE-only, AGENTS-only, both, neither.

The vendored resolver must stay byte-identical to bootstrap's canonical copy
(or, when that copy is absent, to the scratchpad source it was cut from).
"""

import filecmp
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
SCRIPTS = PLUGIN / "skills" / "md-domain" / "scripts"
VENDORED = PLUGIN / "skills_kit_lib" / "instruction_files.py"
CANON = REPO_ROOT / "plugins" / "bootstrap" / "bootstrap_lib" / "instruction_files.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Loading discover_claude_md first puts the plugin root on sys.path.
dc = _load("agents_fb_discover_claude_md", SCRIPTS / "discover_claude_md.py")
from skills_kit_lib import instruction_files as inst  # noqa: E402
from skills_kit_lib import audit as audit_mod  # noqa: E402

cov = _load("agents_fb_discover_coverage", SCRIPTS / "discover_coverage.py")
comp = _load("agents_fb_discover_composition", SCRIPTS / "discover_composition.py")
ep = _load("agents_fb_evidence_pack", SCRIPTS / "evidence_pack.py")
pdoc = _load("agents_fb_discover_project_doc", SCRIPTS / "discover_project_doc.py")

BODY = "# Guidance\n\nSome rule.\n"


def _w(path: Path, text: str = BODY) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_vendored_resolver_matches_canon():
    if CANON.is_file():
        assert filecmp.cmp(CANON, VENDORED, shallow=False), (
            "skills_kit_lib/instruction_files.py drifted from "
            "bootstrap_lib/instruction_files.py"
        )
    else:
        pytest.skip("bootstrap canonical resolver not present in this tree")


@pytest.mark.parametrize(
    "files,expected",
    [
        (["CLAUDE.md"], "CLAUDE.md"),
        (["AGENTS.md"], "AGENTS.md"),
        (["CLAUDE.md", "AGENTS.md"], "CLAUDE.md"),
        ([], None),
    ],
)
def test_resolver_precedence(tmp_path, files, expected):
    for name in files:
        _w(tmp_path / name)
    got = inst.resolve_instruction_file(tmp_path)
    assert (got.name if got else None) == expected


class TestDiscoverClaudeMd:
    def _tree(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _w(tmp_path / "AGENTS.md")            # root: AGENTS only
        _w(tmp_path / "a" / "CLAUDE.md")      # CLAUDE only
        _w(tmp_path / "b" / "AGENTS.md")      # AGENTS only
        _w(tmp_path / "c" / "CLAUDE.md")      # both
        _w(tmp_path / "c" / "AGENTS.md")
        (tmp_path / "d").mkdir()              # neither
        return tmp_path

    def test_descendants_apply_precedence(self, tmp_path):
        root = self._tree(tmp_path)
        found = {p.relative_to(root).as_posix() for p, _ in dc.collect_descendants(root)}
        assert found == {"a/CLAUDE.md", "b/AGENTS.md", "c/CLAUDE.md"}

    def test_shadowed_agents_md_never_discovered(self, tmp_path):
        root = self._tree(tmp_path)
        found = {p.relative_to(root).as_posix() for p, _ in dc.discover(root)}
        assert "c/AGENTS.md" not in found
        assert "AGENTS.md" in found  # active root file

    def test_active_agents_md_at_cwd_is_root_role(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _w(tmp_path / "AGENTS.md")
        assert dc.discover(tmp_path) == [(tmp_path / "AGENTS.md", "root")]

    def test_agents_md_ancestor_demotes_cwd_file_to_child(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _w(tmp_path / "AGENTS.md")
        _w(tmp_path / "sub" / "CLAUDE.md")
        results = dc.discover(tmp_path / "sub")
        assert (tmp_path / "AGENTS.md", "ancestor") in results
        assert (tmp_path / "sub" / "CLAUDE.md", "child") in results

    def test_shadowed_ancestor_agents_md_skipped(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _w(tmp_path / "CLAUDE.md")
        _w(tmp_path / "AGENTS.md")
        _w(tmp_path / "sub" / "CLAUDE.md")
        paths = [p for p, _ in dc.collect_ancestors(tmp_path / "sub")]
        assert paths == [tmp_path / "CLAUDE.md"]

    def test_neither_yields_nothing(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert dc.discover(tmp_path) == []

    def test_agents_md_is_not_a_code_sibling(self, tmp_path):
        assert "AGENTS.md" in dc._CLAUDE_NAMES


class TestCoverageAndComposition:
    def test_ambient_chain_prefers_claude_then_agents(self, tmp_path):
        (tmp_path / ".git").mkdir()
        _w(tmp_path / "AGENTS.md")
        _w(tmp_path / "pkg" / "CLAUDE.md")
        _w(tmp_path / "pkg" / "AGENTS.md")
        _w(tmp_path / "pkg" / "x.py", "x = 1\n")
        chain = cov.ambient_chain(tmp_path / "pkg")
        assert chain == [tmp_path / "AGENTS.md", tmp_path / "pkg" / "CLAUDE.md"]

    def test_walk_tree_claude_mds(self, tmp_path):
        _w(tmp_path / "a" / "AGENTS.md")
        _w(tmp_path / "a" / "x.py", "x = 1\n")
        _w(tmp_path / "b" / "CLAUDE.md")
        _w(tmp_path / "b" / "AGENTS.md")
        _w(tmp_path / "b" / "y.py", "y = 1\n")
        _leaves, mds, _skipped, _n = cov.walk_tree(tmp_path)
        rel = sorted(p.relative_to(tmp_path).as_posix() for p in mds)
        assert rel == ["a/AGENTS.md", "b/CLAUDE.md"]

    def test_composition_document_paths(self, tmp_path):
        _w(tmp_path / "a" / "AGENTS.md")
        _w(tmp_path / "a" / "x.py", "x = 1\n")
        _w(tmp_path / "b" / "CLAUDE.md")
        _w(tmp_path / "b" / "AGENTS.md")
        _w(tmp_path / "b" / "y.py", "y = 1\n")
        _w(tmp_path / "c" / "z.py", "z = 1\n")  # neither
        docs = comp.build_subject(tmp_path)["documentPaths"]
        root = tmp_path.resolve()
        assert docs[str(root / "a")] == str(root / "a" / "AGENTS.md")
        assert docs[str(root / "b")] == str(root / "b" / "CLAUDE.md")
        assert docs[str(root / "c")] == str(root / "c" / "CLAUDE.md")


class TestEvidencePackAndProjectDoc:
    def test_artifact_of(self, tmp_path):
        _w(tmp_path / "a" / "AGENTS.md")
        _w(tmp_path / "b" / "AGENTS.md")
        _w(tmp_path / "b" / "CLAUDE.md")
        assert ep.artifact_of("a/AGENTS.md", tmp_path) == "claude-md"
        assert ep.artifact_of("b/AGENTS.md", tmp_path) == ep.SHADOWED_ARTIFACT
        assert ep.artifact_of("b/CLAUDE.md", tmp_path) == "claude-md"

    def test_ancestors_use_resolver(self, tmp_path):
        _w(tmp_path / "AGENTS.md")
        _w(tmp_path / "s" / "CLAUDE.md")
        _w(tmp_path / "s" / "AGENTS.md")
        _w(tmp_path / "s" / "t" / "CLAUDE.md")
        got = ep._ancestors(tmp_path, tmp_path / "s" / "t" / "CLAUDE.md")
        assert got == [tmp_path / "s" / "CLAUDE.md", tmp_path / "AGENTS.md"]

    def test_agents_md_is_not_a_project_doc(self):
        assert "AGENTS.md" in pdoc._NOT_PROJECT_DOC_NAMES


class TestAuditDispatch:
    CONTRACT = (
        "# T\n\n```yaml\nclaude_md:\n  _schema_version: \"1\"\n  scope:\n"
        "    directory: x\n    covers: [a]\n    excludes: [b]\n  conventions:\n"
        "    - rule: r\n      keywords: [a, b, c]\n      why: w\n```\n"
    )

    def test_active_agents_md_audited_as_claude_md(self, tmp_path):
        p = _w(tmp_path / "AGENTS.md", self.CONTRACT)
        assert audit_mod.audit(p).get("kind") == "claude_md"

    def test_shadowed_agents_md_not_audited(self, tmp_path):
        _w(tmp_path / "CLAUDE.md", self.CONTRACT)
        p = _w(tmp_path / "AGENTS.md", self.CONTRACT)
        result = audit_mod.audit(p)
        assert "not audited" in result.get("error", "")


    def test_shadowed_agents_md_is_not_an_evidence_subject(self, tmp_path):
        _w(tmp_path / "CLAUDE.md")
        _w(tmp_path / "AGENTS.md")
        with pytest.raises(ValueError, match="not an audit subject"):
            ep.build_structured(tmp_path, "AGENTS.md")


class TestGenerateCallerContract:
    def test_lane_prose_tells_caller_to_pass_document_paths(self):
        lane = (PLUGIN / "skills/md-domain/references/lanes/generation-lane.md").read_text(encoding="utf-8")
        assert "`documentPaths`" in lane and "input.documentPaths" in lane
        assert "discover_composition.py" in lane

    def test_workflow_reads_document_paths_input(self):
        js = (PLUGIN / "skills/md-domain/workflow/claude-md-generate.js").read_text(encoding="utf-8")
        assert "input.documentPaths" in js

    def test_discovery_emits_document_paths(self):
        assert "documentPaths" in (SCRIPTS / "discover_composition.py").read_text(encoding="utf-8")
