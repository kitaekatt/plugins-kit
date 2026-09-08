"""Pinning tests for I1: audit / classify / tag must parse frontmatter with
parse_frontmatter's mode="full" (or an equivalent that resolves block
scalars and quoted multi-line values), not the default mode="light".

Light mode is same-line regex extraction: a folded `description: >-` block
reads as the literal ">-" and a two-line description is truncated to its
first line, so a description satisfying both desc-directive-form ("Use
when...") and desc-exclusion-clause ("Do NOT use for...") FAILs when the
two phrases land on different physical lines under a folded/multi-line
block scalar.
"""

from pathlib import Path

from skills_kit_lib.audit import audit
from skills_kit_lib.classify import classify
from skills_kit_lib.tag import tag


FOLDED_DESC_SKILL = (
    "---\n"
    "name: s\n"
    "description: >-\n"
    "  Use when doing X.\n"
    "  Do NOT use for Y.\n"
    "skill-type: reference-skill\n"
    "---\n"
    "# S\n\n"
    "## Example\n\nfoo\n\n"
    "## Gotcha\n\nbar\n"
)

TWO_LINE_DESC_SKILL = (
    "---\n"
    "name: s\n"
    'description: "Use when doing X.\n'
    "  Do NOT use for Y.\"\n"
    "skill-type: reference-skill\n"
    "---\n"
    "# S\n"
)

QUOTED_SKILL_TYPE_SKILL = (
    "---\n"
    "name: s\n"
    "description: Use when X. Do NOT use for Y.\n"
    "skill-type: 'reference-skill'\n"
    "---\n"
    "# S\n"
)


def _write(tmp_path: Path, text: str, name: str = "SKILL.md") -> Path:
    d = tmp_path / "s"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _row(rows: list[dict], rule: str) -> dict:
    for r in rows:
        if r["rule"] == rule:
            return r
    raise AssertionError(f"rule '{rule}' not found in {rows}")


class TestAuditFoldedDescription:
    def test_directive_form_and_exclusion_clause_pass_on_folded_block(self, tmp_path):
        report = audit(_write(tmp_path, FOLDED_DESC_SKILL))
        directive = _row(report["universal"], "desc-directive-form")
        exclusion = _row(report["universal"], "desc-exclusion-clause")
        assert directive["verdict"] == "pass", directive
        assert exclusion["verdict"] == "pass", exclusion

    def test_desc_length_measures_full_folded_text(self, tmp_path):
        report = audit(_write(tmp_path, FOLDED_DESC_SKILL))
        length_row = _row(report["universal"], "desc-160-char")
        # The folded text is "Use when doing X. Do NOT use for Y." (35 chars),
        # not the literal marker ">-" (2 chars).
        assert "len=35" in length_row["note"], length_row


class TestAuditTwoLineDescription:
    def test_two_line_description_measures_full_text(self, tmp_path):
        report = audit(_write(tmp_path, TWO_LINE_DESC_SKILL))
        length_row = _row(report["universal"], "desc-160-char")
        assert "len=35" in length_row["note"], length_row


class TestClassifyReadsQuotedSkillType:
    def test_declared_type_unquoted(self, tmp_path):
        report = classify(_write(tmp_path, QUOTED_SKILL_TYPE_SKILL))
        assert report["declared_type"] == "reference-skill", report


class TestTagReadsQuotedSkillType:
    def test_current_value_unquoted_for_noop(self, tmp_path):
        p = _write(tmp_path, QUOTED_SKILL_TYPE_SKILL)
        result = tag(p, "reference-skill", force=False, check_only=False)
        assert result["ok"] and result["action"] == "no-op", result
