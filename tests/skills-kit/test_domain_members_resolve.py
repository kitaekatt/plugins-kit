"""Registry integrity for the skills-kit skill roster and md-domain's dispatch table.

Successor of the old member-resolution integration test. Before the md-domain
fold (2026-07-29) skills-kit expressed its verb x artifact matrix as TOPOLOGY --
two router skills declaring members that had to resolve against the on-disk skill
pool. The fold replaced that with DATA: one skill, one dispatch table, a lane
record per dispatch entry. The member-resolution question became vacuous (there are no members to
dangle); the questions that replaced it are the ones the phase-3 design's "Router
enforcement" section names:

(a) the skills-kit skill roster on disk is exactly the four surviving skills;
(b) md-domain's dispatch table holds exactly the registered lanes, and
    `generate x references` is deliberately absent;
(c) every lane record carries the two REQUIRED fields (settled decision 3) --
    invocation_phrasings (>= 3) and change_driver;
(d) every path a lane record binds resolves on disk;
(e) no dissolved member/router directory survives.

The generic `checks.check_domain_members_resolve` unit tests are retained below:
the check is still live library code consumed by other corpora, and its tmp-tree
tests are not vacuous -- only the skills-kit-specific integration assertion was.
"""

import re
from pathlib import Path

import pytest
import yaml

from skills_kit_lib import checks

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_KIT_SKILLS = REPO_ROOT / "plugins" / "skills-kit" / "skills"
MD_DOMAIN = SKILLS_KIT_SKILLS / "md-domain"

EXPECTED_ROSTER = {
    "knowledge-encoding",
    "materialized-output",
    "md-domain",
    "update-documentation",
}

# The nine skills dissolved by the fold (six members, two routers, and the
# standalone placement skill that became a reference doc).
DISSOLVED = (
    "md-audit",
    "md-authoring",
    "skill-audit",
    "claude-md-audit",
    "project-doc-audit",
    "references-audit",
    "claude-md-authoring",
    "skill-authoring",
    "cohesion-principles",
)

# `table_key` is the left-hand cell of the human-readable dispatch table row.
# It is spelled out per lane rather than derived because the non-artifact lanes
# each phrase their subject differently in prose, and a derived key would only
# ever be checked against itself.
EXPECTED_LANES = {
    "audit_skill": {"verb": "audit", "artifact": "skill"},
    "audit_claude_md": {"verb": "audit", "artifact": "claude-md"},
    "audit_project_doc": {"verb": "audit", "artifact": "project-doc"},
    "audit_references": {"verb": "audit", "artifact": "references"},
    "author_skill": {"verb": "author", "artifact": "skill"},
    "author_claude_md": {"verb": "author", "artifact": "claude-md"},
    "author_project_doc": {"verb": "author", "artifact": "project-doc"},
    "generate_claude_md": {"verb": "generate", "artifact": "claude-md"},
    "coverage_code_subtree": {
        # The verb is `analyze`; the lane id, its procedure, its standards doc
        # and its scripts are all named for the OUTPUT (coverage) instead.
        "verb": "analyze",
        "subject": "code_subtree",
        # The lane id stays `coverage_code_subtree` (a stable identifier), but
        # the table's human-readable key names the real unit: one directory's
        # own direct code files, never a subtree.
        "table_key": "analyze (one directory)",
    },
}

# Lane-record keys whose value is a path relative to the md-domain skill dir.
PATH_KEYS = (
    "standards",
    "procedure",
    "discover_script",
    "scanner_script",
    "taxonomy_doc",
    "workflow_detect",
    "workflow_classify",
    "workflow_remediate",
    "workflow_generate",
)


def _skill_md() -> str:
    return (MD_DOMAIN / "SKILL.md").read_text(encoding="utf-8")


def _lanes_block(text: str) -> str:
    """The fenced ```yaml block whose root key is `lanes:`."""
    for block in re.findall(r"```yaml\n(.*?)\n```", text, re.S):
        if block.lstrip().startswith("lanes:"):
            return block
    raise AssertionError("md-domain/SKILL.md has no fenced `lanes:` YAML block")


def load_lane_records():
    """Parse the dispatch table's lane records out of SKILL.md.

    The block is plain YAML: a `lanes:` mapping carrying `_schema_version`
    and the `records:` sequence.
    """
    data = yaml.safe_load(_lanes_block(_skill_md()))
    lanes = data["lanes"]
    assert lanes.get("_schema_version"), "the lanes block lost its _schema_version marker"
    records = lanes["records"]
    assert isinstance(records, list) and records, "lanes block did not parse to a list"
    return records


LANE_RECORDS = load_lane_records()
LANE_IDS = [r["id"] for r in LANE_RECORDS]


class TestSkillsKitRoster:
    """(a) + (e) -- the fold is complete on disk, not half-migrated."""

    def test_roster_is_exactly_the_four_surviving_skills(self):
        on_disk = {
            d.name for d in SKILLS_KIT_SKILLS.iterdir()
            if d.is_dir() and (d / "SKILL.md").exists()
        }
        assert on_disk == EXPECTED_ROSTER, (
            "skills-kit's skill roster drifted from the post-fold target "
            f"(extra: {sorted(on_disk - EXPECTED_ROSTER)}, "
            f"missing: {sorted(EXPECTED_ROSTER - on_disk)})"
        )

    @pytest.mark.parametrize("name", DISSOLVED)
    def test_no_dissolved_skill_dir_survives(self, name):
        assert not (SKILLS_KIT_SKILLS / name).exists(), (
            f"{name}/ still exists -- a router or member skill surviving next to "
            "md-domain is the half-migration the fold exists to avoid"
        )


class TestDispatchTable:
    """(b) -- exactly the registered lanes, and generate x references is deliberately absent."""

    def test_lane_roster_is_exact(self):
        assert sorted(LANE_IDS) == sorted(EXPECTED_LANES), (
            f"lane roster drifted: {sorted(LANE_IDS)}"
        )

    def test_lane_ids_are_unique(self):
        assert len(LANE_IDS) == len(set(LANE_IDS))

    @pytest.mark.parametrize("lane_id", sorted(EXPECTED_LANES))
    def test_lane_declares_its_verb_and_subject_axis(self, lane_id):
        record = next(r for r in LANE_RECORDS if r["id"] == lane_id)
        expected = EXPECTED_LANES[lane_id]
        assert record.get("verb") == expected["verb"]
        if "artifact" in expected:
            assert record.get("artifact") == expected["artifact"]
            assert "subject" not in record
        else:
            assert record.get("subject") == expected["subject"]
            assert "artifact" not in record

    def test_author_references_lane_is_absent(self):
        assert not any(
            r.get("verb") == "author" and r.get("artifact") == "references"
            for r in LANE_RECORDS
        ), "author x references must have no lane -- cross-references are emergent"

    def test_generate_is_claude_md_only(self):
        """`generate` consumes coverage, and only `analyze` produces any."""
        arts = {r.get("artifact") for r in LANE_RECORDS if r.get("verb") == "generate"}
        assert arts == {"claude-md"}, (
            "generate must take claude-md alone -- nothing analyzes a codebase and "
            f"emits skill or project-doc coverage; got {sorted(arts)}"
        )

    def test_producing_verbs_declare_their_input_provenance(self):
        """author vs generate is decided by provenance, so each must state it."""
        expected = {"author": "user_supplied", "generate": "coverage"}
        for record in LANE_RECORDS:
            verb = record.get("verb")
            if verb not in expected:
                continue
            assert record.get("input_provenance") == expected[verb], (
                f"{record['id']}: input_provenance must be {expected[verb]!r} -- "
                "the two producing verbs are told apart by it, not by wording"
            )

    def test_markdown_table_matches_the_lane_records(self):
        """The human-readable table and the machine-readable records must agree."""
        text = _skill_md()
        section = text.split("## Dispatch table", 1)[1].split("### Lane records", 1)[0]
        rows = {}
        for line in section.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 2 or cells[0].startswith("---") or "Verb x artifact" in cells[0]:
                continue
            rows[cells[0]] = cells[1]
        assert rows, "could not parse the dispatch table"
        for lane_id, expected in EXPECTED_LANES.items():
            key = (
                f"{expected['verb']} x {expected['artifact']}"
                if "artifact" in expected
                else expected["table_key"]
            )
            assert key in rows, f"dispatch table has no `{key}` row"
            assert rows[key] == f"`{lane_id}`", (
                f"dispatch table row `{key}` points at {rows[key]}, not `{lane_id}`"
            )
        assert "author x references" in rows
        assert "no lane" in rows["author x references"], (
            "the author x references row gained a lane id"
        )


class TestRequiredLaneFields:
    """(c) -- the settled-decision-3 mechanical check on every lane record."""

    @pytest.mark.parametrize("lane_id", sorted(EXPECTED_LANES))
    def test_invocation_phrasings_present_and_plural(self, lane_id):
        record = next(r for r in LANE_RECORDS if r["id"] == lane_id)
        phrasings = record.get("invocation_phrasings")
        assert isinstance(phrasings, list), (
            f"{lane_id}: invocation_phrasings is REQUIRED -- a lane nobody can "
            "phrase their way into is unreachable"
        )
        assert len(phrasings) >= 3, (
            f"{lane_id}: only {len(phrasings)} invocation_phrasings; at least 3 required"
        )
        assert all(isinstance(p, str) and p.strip() for p in phrasings)

    @pytest.mark.parametrize("lane_id", sorted(EXPECTED_LANES))
    def test_change_driver_present_and_non_empty(self, lane_id):
        record = next(r for r in LANE_RECORDS if r["id"] == lane_id)
        driver = record.get("change_driver")
        assert isinstance(driver, str) and driver.strip(), (
            f"{lane_id}: change_driver is REQUIRED -- a lane whose content "
            "accretes because nobody declared what changes it"
        )

    @pytest.mark.parametrize("lane_id", sorted(EXPECTED_LANES))
    def test_verdict_set_declared(self, lane_id):
        record = next(r for r in LANE_RECORDS if r["id"] == lane_id)
        verdicts = record.get("verdicts")
        assert isinstance(verdicts, list) and verdicts, f"{lane_id}: no verdict set"
        if lane_id == "audit_references":
            assert verdicts == ["AUTO", "DISCUSS", "SPECIAL"]
        elif record["verb"] == "audit":
            assert "NOT-AUDITED" in verdicts, (
                f"{lane_id}: an audit lane that cannot express a decline is a fake gate"
            )
            assert "DIFF-CLEAN" in verdicts


class TestBoundPathsResolve:
    """(d) -- every path a lane record binds exists on disk."""

    @pytest.mark.parametrize("lane_id", sorted(EXPECTED_LANES))
    def test_every_bound_path_resolves(self, lane_id):
        record = next(r for r in LANE_RECORDS if r["id"] == lane_id)
        bound = {k: v for k, v in record.items() if k in PATH_KEYS}
        assert bound, f"{lane_id}: binds no paths at all"
        for key, rel in bound.items():
            assert (MD_DOMAIN / rel).exists(), (
                f"{lane_id}.{key} -> {rel} does not resolve under md-domain/"
            )

    def test_audit_lanes_bind_their_machinery(self):
        for record in LANE_RECORDS:
            if record["verb"] != "audit":
                continue
            assert "standards" in record and "procedure" in record
            assert "workflow_remediate" in record, f"{record['id']}: no remediate lane"
            assert ("workflow_detect" in record) or ("workflow_classify" in record), (
                f"{record['id']}: no detect/classify lane"
            )

    def test_analyze_lane_is_report_only(self):
        record = next(r for r in LANE_RECORDS if r["verb"] == "analyze")
        assert "workflow_remediate" not in record
        assert record["verdicts"] == ["GAPS-FOUND", "COVERAGE-ASSESSED"]

    def test_producing_lanes_bind_standards_and_procedure(self):
        """author and generate SHARE one producing procedure."""
        producing = [r for r in LANE_RECORDS if r["verb"] in ("author", "generate")]
        assert producing, "no producing lanes found"
        for record in producing:
            assert record["procedure"] == "references/lanes/generation-lane.md"
            assert record["standards"].startswith("references/standards/")

    def test_regeneration_declares_the_propose_markings_gate(self):
        """Regeneration must never silently overwrite an unmarked document."""
        record = next(r for r in LANE_RECORDS if r["verb"] == "generate")
        assert record.get("regeneration") == "propose-markings-first", (
            "generate_claude_md must declare the propose-markings-first gate -- "
            "without it the first regeneration of a hand-written CLAUDE.md guts it"
        )


# ---------------------------------------------------------------------------
# Generic member-resolution unit tests for checks.check_domain_members_resolve.
# Retained from the pre-fold file: the check is live library code, and these
# tmp-tree tests exercise it independently of the skills-kit roster.
# ---------------------------------------------------------------------------


def _write_skill(root, plugin, skill, body_yaml=None, name=None):
    """Create plugins/<plugin>/skills/<skill>/SKILL.md with optional body YAML."""
    d = root / "plugins" / plugin / "skills" / skill
    d.mkdir(parents=True, exist_ok=True)
    fm_name = name if name is not None else skill
    parts = [
        "---",
        f"name: {fm_name}",
        "description: Use when testing. Do NOT use otherwise.",
        "---",
        "",
        "# Test skill",
        "",
    ]
    if body_yaml is not None:
        parts += ["```yaml", body_yaml.rstrip("\n"), "```", ""]
    (d / "SKILL.md").write_text("\n".join(parts), encoding="utf-8")
    return d


def _domain_body(members):
    lines = ["domain_skill:", "  identity: A test domain.",
             "  scope:", "    covers: [x]", "    excludes: [y]",
             "  index:", "    members:"]
    for nm, ref in members:
        lines += [f"      - name: {nm}", "        type: audit-skill",
                  f"        ref: {ref}", "        keywords: [a, b, c]"]
    return "\n".join(lines)


def _capability_body(members):
    lines = ["capability_skill:", "  identity: A test capability."]
    lines += ["  members:"]
    for nm, ref in members:
        lines += [f"    - name: {nm}", "      type: capability-skill",
                  f"      ref: {ref}", "      keywords: [a, b, c]"]
    return "\n".join(lines)


def _result_for(results, domain):
    matches = [r for r in results if r.domain == domain]
    assert matches, f"no result for domain '{domain}'"
    return matches[0]


class TestDomainMemberResolution:
    def test_resolving_member_passes(self, tmp_path):
        _write_skill(tmp_path, "p", "some-audit")
        _write_skill(tmp_path, "p", "some-domain",
                     body_yaml=_domain_body([("some-audit", "/some-audit")]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "some-domain")
        assert r.status == "pass", r.message
        assert r.unresolved == []

    def test_dangling_member_unresolved(self, tmp_path):
        _write_skill(tmp_path, "p", "some-domain",
                     body_yaml=_domain_body([("ghost", "/ghost")]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "some-domain")
        assert r.status == "unresolved"
        assert any(name == "ghost" for name, _ref in r.unresolved)

    def test_mixed_members_report_only_the_dangling_one(self, tmp_path):
        _write_skill(tmp_path, "p", "some-audit")
        _write_skill(tmp_path, "p", "some-domain",
                     body_yaml=_domain_body([
                         ("some-audit", "/some-audit"),
                         ("ghost", "/ghost"),
                     ]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "some-domain")
        assert r.status == "unresolved"
        unresolved_names = {name for name, _ in r.unresolved}
        assert unresolved_names == {"ghost"}

    def test_bare_ref_resolves(self, tmp_path):
        """A member ref without a leading slash still resolves."""
        _write_skill(tmp_path, "ue", "ue-python-api")
        _write_skill(tmp_path, "ue", "unreal-domain",
                     body_yaml=_capability_body([("ue-python-api", "ue-python-api")]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "unreal-domain")
        assert r.status == "pass", r.message

    def test_plugin_qualified_ref_resolves(self, tmp_path):
        """A `plugin:skill` qualified ref normalizes to the bare name and resolves."""
        _write_skill(tmp_path, "p", "some-member")
        _write_skill(tmp_path, "p", "some-domain",
                     body_yaml=_domain_body([("some-member", "skills-kit:some-member")]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "some-domain")
        assert r.status == "pass", r.message

    def test_frontmatter_name_pool_resolves(self, tmp_path):
        """Resolution uses the frontmatter name, not only the directory name."""
        # Skill lives in dir 'dir-name' but declares name 'real-name'.
        _write_skill(tmp_path, "p", "dir-name", name="real-name")
        _write_skill(tmp_path, "p", "some-domain",
                     body_yaml=_domain_body([("real-name", "/real-name")]))
        results = checks.check_domain_members_resolve(tmp_path)
        r = _result_for(results, "some-domain")
        assert r.status == "pass", r.message

    def test_non_member_skill_is_silent(self, tmp_path):
        """A skill that declares no members produces no result row."""
        _write_skill(tmp_path, "p", "plain-skill")
        results = checks.check_domain_members_resolve(tmp_path)
        assert all(r.domain != "plain-skill" for r in results)

    def test_real_corpus_has_no_unresolved_members(self):
        """Integration: any domain/capability member in the live repo resolves.

        md-domain declares no members (the fold turned membership into lane
        records), so this covers the other plugins' domain skills.
        """
        results = checks.check_domain_members_resolve()
        unresolved = [r for r in results if r.status == "unresolved"]
        assert not unresolved, checks.render_member_results(results)
