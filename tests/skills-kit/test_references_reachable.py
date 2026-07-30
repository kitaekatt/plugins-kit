"""Tests for the references_reachable_from_skill_md rule.

The rule closes the missing-load-graph-edge defect class (home-domain audit,
2026-07-15): a member of the skill composition exists on disk but SKILL.md
carries no edge to it, so an agent with the skill loaded cannot discover it.
Three mechanical detections in audit.check_references_reachable_from_skill_md:
orphaned references/ files (FAIL for .md, JUDGMENT for other files),
two-hop-only references (JUDGMENT), and content-bearing member directories
with no SKILL.md edge (JUDGMENT); plus dangling structured index/members
paths (FAIL).
"""

import yaml

from skills_kit_lib.audit import (
    FAIL,
    JUDGMENT,
    PASS,
    audit,
    check_references_reachable_from_skill_md,
)


def _yaml_block(data: dict) -> str:
    return "```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n"


def _fails(results):
    return [r for r in results if r.verdict == FAIL]


def _judgments(results):
    return [r for r in results if r.verdict == JUDGMENT]


def _skill(tmp_path, body: str, refs: dict[str, str] | None = None):
    """Create <root>/.claude/skills/subject/SKILL.md plus references/ files."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    skill_dir = tmp_path / ".claude" / "skills" / "subject"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: subject\n---\n\n# subject\n\n" + body, encoding="utf-8",
    )
    for name, content in (refs or {}).items():
        p = skill_dir / "references" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return skill_dir


def _run(skill_dir):
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    return check_references_reachable_from_skill_md(body, skill_dir)


class TestOrphanedReferences:
    def test_orphaned_md_reference_fails(self, tmp_path):
        """The pixel-wifi case: a references/*.md in no index, cited nowhere."""
        skill_dir = _skill(
            tmp_path,
            "See references/linked.md for detail.\n",
            refs={"linked.md": "# linked\n", "orphan.md": "# orphan\n"},
        )
        results = _run(skill_dir)
        fails = _fails(results)
        assert len(fails) == 1
        assert "references/orphan.md" in fails[0].row
        assert "orphaned reference" in fails[0].note

    def test_two_hop_only_md_reference_is_judgment(self, tmp_path):
        """Cited only from a sibling reference: reachable, but the SKILL.md
        index cannot route to it."""
        skill_dir = _skill(
            tmp_path,
            "See references/linked.md for detail.\n",
            refs={
                "linked.md": "# linked\n\nSee also two-hop.md for the log.\n",
                "two-hop.md": "# two-hop\n",
            },
        )
        results = _run(skill_dir)
        assert not _fails(results)
        judgments = _judgments(results)
        assert len(judgments) == 1
        assert "references/two-hop.md" in judgments[0].row
        assert "references/linked.md" in judgments[0].note

    def test_orphaned_non_md_file_is_judgment_not_fail(self, tmp_path):
        """A data file under references/ may be script-consumed; surface it
        without gating compliance."""
        skill_dir = _skill(
            tmp_path,
            "See references/linked.md.\n",
            refs={"linked.md": "# linked\n", "inventory.yaml": "a: 1\n"},
        )
        results = _run(skill_dir)
        assert not _fails(results)
        judgments = _judgments(results)
        assert any("references/inventory.yaml" in r.row for r in judgments)

    def test_wiki_link_counts_as_direct_edge(self, tmp_path):
        skill_dir = _skill(
            tmp_path,
            "Load [[linked]] when debugging.\n",
            refs={"linked.md": "# linked\n"},
        )
        results = _run(skill_dir)
        assert not _fails(results)
        assert not _judgments(results)

    def test_basename_must_not_match_inside_longer_name(self, tmp_path):
        """Citing blind-state.md must not count as an edge to state.md."""
        skill_dir = _skill(
            tmp_path,
            "See references/blind-state.md.\n",
            refs={"blind-state.md": "# blinds\n", "state.md": "# state\n"},
        )
        results = _run(skill_dir)
        fails = _fails(results)
        assert len(fails) == 1
        assert "references/state.md" in fails[0].row


class TestMemberDirectories:
    def test_unlinked_tests_dir_is_judgment(self, tmp_path):
        """The home-domain tests/ case: a real suite, zero SKILL.md mention."""
        skill_dir = _skill(tmp_path, "Orientation only.\n")
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "test_x.py").write_text("def test(): pass\n",
                                                       encoding="utf-8")
        results = _run(skill_dir)
        judgments = _judgments(results)
        assert len(judgments) == 1
        assert judgments[0].row == "load-graph: tests/"
        assert "no edge from SKILL.md" in judgments[0].note

    def test_two_hop_dir_mention_still_flags_with_context(self, tmp_path):
        """A reference doc naming tests/ does not substitute for a SKILL.md
        edge, but the note says the two-hop mention exists."""
        skill_dir = _skill(
            tmp_path,
            "See references/linked.md.\n",
            refs={"linked.md": "# linked\n\nRun tests/ to verify.\n"},
        )
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "test_x.py").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        judgments = [r for r in _judgments(results) if r.row == "load-graph: tests/"]
        assert len(judgments) == 1
        assert "two hops" in judgments[0].note

    def test_path_mention_links_dir(self, tmp_path):
        skill_dir = _skill(tmp_path, "Edit tests/conftest.py, nothing else.\n")
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "conftest.py").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        assert not _judgments(results)
        assert not _fails(results)

    def test_structured_ref_value_links_dir(self, tmp_path):
        """index.members ref: tests (bare, no slash) counts as the edge."""
        body = _yaml_block({"domain_skill": {"index": {"members": [
            {"name": "suite", "type": "tests", "ref": "tests",
             "keywords": ["a", "b", "c"]},
        ]}}})
        skill_dir = _skill(tmp_path, body)
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "test_x.py").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        assert not _judgments(results)

    def test_junk_and_empty_dirs_are_excluded(self, tmp_path):
        skill_dir = _skill(tmp_path, "Orientation only.\n")
        (skill_dir / "__pycache__").mkdir()
        (skill_dir / "__pycache__" / "m.cpython-312.pyc").write_text(
            "", encoding="utf-8")
        (skill_dir / ".venv").mkdir()
        (skill_dir / ".venv" / "pyvenv.cfg").write_text("", encoding="utf-8")
        (skill_dir / "empty").mkdir()
        (skill_dir / "pyc-only").mkdir()
        (skill_dir / "pyc-only" / "m.pyc").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        assert results == []

    def test_no_members_no_references_emits_nothing(self, tmp_path):
        skill_dir = _skill(tmp_path, "Orientation only.\n")
        assert _run(skill_dir) == []


class TestDanglingIndexEdges:
    def test_dangling_members_ref_fails(self, tmp_path):
        body = _yaml_block({"domain_skill": {"index": {"members": [
            {"name": "suite", "type": "tests", "ref": "tests/",
             "keywords": ["a", "b", "c"]},
        ]}}})
        skill_dir = _skill(tmp_path, body)
        results = _run(skill_dir)
        fails = _fails(results)
        assert len(fails) == 1
        assert "index.members[0].ref" in fails[0].row
        assert "does not exist" in fails[0].note

    def test_skill_name_and_command_member_refs_are_not_paths(self, tmp_path):
        """Union-domain skills index sibling SKILLS (ref: skill-audit) and
        slash commands (ref: /md-domain) -- never dangling-edge FAILs."""
        body = _yaml_block({"domain_skill": {"index": {"members": [
            {"name": "skill-audit", "type": "audit-skill",
             "ref": "skill-audit", "keywords": ["a", "b", "c"]},
            {"name": "fixer", "type": "command",
             "ref": "/fix-up-redirectors", "keywords": ["a", "b", "c"]},
        ]}}})
        skill_dir = _skill(tmp_path, body)
        assert not _fails(_run(skill_dir))

    def test_dangling_non_md_index_path_fails(self, tmp_path):
        body = _yaml_block({"domain_skill": {"index": {"references": [
            {"id": "inv", "path": "references/inventory.yaml",
             "keywords": ["a", "b", "c"], "summary": "Inventory."},
        ]}}})
        skill_dir = _skill(tmp_path, body)
        results = _run(skill_dir)
        assert any("index.references[0].path" in r.row for r in _fails(results))

    def test_md_index_path_left_to_body_citation_check(self, tmp_path):
        """references/x.md shapes are already covered by the universal
        'references cited in body all exist' check -- the structured pass
        skips them, and the full audit still reports exactly one FAIL."""
        body = _yaml_block({"domain_skill": {"index": {"references": [
            {"id": "gone", "path": "references/gone.md",
             "keywords": ["a", "b", "c"], "summary": "Missing."},
        ]}}})
        skill_dir = _skill(tmp_path, body)
        results = _run(skill_dir)
        assert not any("index.references" in r.row for r in results)
        report = audit(skill_dir / "SKILL.md")
        rows = report["universal"]
        dangling = [r for r in rows if r["verdict"] == FAIL
                    and "gone.md" in (r["note"] or "")]
        assert len(dangling) == 1

    def test_resolving_index_edges_pass(self, tmp_path):
        body = _yaml_block({"domain_skill": {"index": {
            "references": [
                {"id": "linked", "path": "references/linked.md",
                 "keywords": ["a", "b", "c"], "summary": "Linked."},
            ],
            "members": [
                {"name": "suite", "type": "tests", "ref": "tests/",
                 "keywords": ["a", "b", "c"]},
            ],
        }}})
        skill_dir = _skill(tmp_path, body, refs={"linked.md": "# linked\n"})
        (skill_dir / "tests").mkdir()
        (skill_dir / "tests" / "test_x.py").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        assert not _fails(results)
        assert not _judgments(results)
        assert any(r.verdict == PASS for r in results)


class TestCleanSkill:
    def test_clean_skill_single_pass_row(self, tmp_path):
        skill_dir = _skill(
            tmp_path,
            "See references/linked.md. Run scripts/tool.py.\n",
            refs={"linked.md": "# linked\n"},
        )
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "tool.py").write_text("", encoding="utf-8")
        results = _run(skill_dir)
        assert len(results) == 1
        assert results[0].verdict == PASS
        assert results[0].row == "load-graph: members reachable from SKILL.md"


class TestAuditIntegration:
    def test_full_audit_reports_orphan_in_universal_group(self, tmp_path):
        skill_dir = _skill(
            tmp_path,
            "Orientation only.\n",
            refs={"orphan.md": "# orphan\n"},
        )
        report = audit(skill_dir / "SKILL.md")
        rows = report["universal"]
        assert any(
            r["verdict"] == FAIL and "load-graph: references/orphan.md" == r["row"]
            for r in rows
        )
