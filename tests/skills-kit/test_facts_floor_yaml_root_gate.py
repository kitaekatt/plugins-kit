"""I8: check_facts_cross_rules gated on the FRONTMATTER skill-type only,
while its sibling check_technique_caution_cross_rule gates on the YAML
ROOT. A reference_skill: document whose (advisory, JUDGMENT-only)
frontmatter skill-type is missing or says something else silently skipped
the facts floor. Gate on the YAML root too, with frontmatter as the
fallback for legacy documents that carry no YAML root at all.
"""

from pathlib import Path

from skills_kit_lib.audit import audit


def _write(tmp_path: Path, body: str, frontmatter: str = "---\nname: s\n---\n") -> Path:
    d = tmp_path / "s"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(frontmatter + body, encoding="utf-8")
    return p


def _find_rule(report, rule):
    for r in report["yaml_contract"]:
        if r["rule"] == rule:
            return r
    return None


class TestFactsFloorGatesOnYamlRoot:
    def test_reference_skill_root_no_frontmatter_type_still_gets_facts_floor(self, tmp_path):
        body = (
            "# S\n\n"
            "```yaml\nreference_skill:\n  identity: i\n  scope:\n    covers: [a]\n"
            "    excludes: [b]\n```\n"
        )
        # No skill-type in frontmatter at all.
        report = audit(_write(tmp_path, body, frontmatter="---\nname: s\n---\n"))
        row = _find_rule(report, "facts-floor")
        assert row is not None, report["yaml_contract"]
        assert row["verdict"] == "fail", row  # no facts declared -> facts-floor FAILs

    def test_frontmatter_disagreeing_with_yaml_root_still_gets_facts_floor(self, tmp_path):
        body = (
            "# S\n\n"
            "```yaml\nreference_skill:\n  identity: i\n  scope:\n    covers: [a]\n"
            "    excludes: [b]\n```\n"
        )
        report = audit(_write(
            tmp_path, body,
            frontmatter="---\nname: s\nskill-type: technique-skill\n---\n",
        ))
        row = _find_rule(report, "facts-floor")
        assert row is not None, report["yaml_contract"]

    def test_legacy_document_no_yaml_root_falls_back_to_frontmatter(self, tmp_path):
        # No YAML contract block at all -- frontmatter is the only signal;
        # a non-reference-skill declared type must still skip the floor.
        report = audit(_write(
            tmp_path, "# S\n\nplain prose\n",
            frontmatter="---\nname: s\nskill-type: technique-skill\n---\n",
        ))
        row = _find_rule(report, "facts-floor")
        assert row is None
