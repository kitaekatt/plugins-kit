"""Tests for the claude_md insights/conventions union floor (Dec-19).

The schema no longer requires insights: as a key -- a conventions-only
CLAUDE.md validates. The non-empty floor is the insights/conventions UNION,
enforced as a document-level check (audit.check_claude_md_record_floor); an
empty block (neither insights nor conventions) still fails.
"""

import copy

import yaml

from skills_kit_lib.audit import FAIL, PASS, audit, check_claude_md_record_floor
from skills_kit_lib.schema_engine import validate
from skills_kit_lib.schemas.claude_md import CLAUDE_MD_SCHEMA


CONVENTIONS_ONLY = {
    "claude_md": {
        "_schema_version": "1",
        "scope": {
            "directory": "some/dir",
            "covers": ["x"],
        },
        "conventions": [
            {
                "rule": "Do X before Y.",
                "keywords": ["x first", "ordering", "convention"],
                "why": "Y depends on X.",
            },
        ],
    }
}


class TestSchema:
    def test_conventions_only_block_validates(self):
        fails, _ = validate(CONVENTIONS_ONLY, CLAUDE_MD_SCHEMA)
        assert fails == []

    def test_insights_when_present_still_need_one_record(self):
        data = copy.deepcopy(CONVENTIONS_ONLY)
        data["claude_md"]["insights"] = []
        fails, _ = validate(data, CLAUDE_MD_SCHEMA)
        assert fails, "an empty insights list is still a shape error (min_len 1)"

    def test_insights_shape_still_enforced_when_present(self, minimal_claude_md):
        fails, _ = validate(minimal_claude_md, CLAUDE_MD_SCHEMA)
        assert fails == []
        bad = copy.deepcopy(minimal_claude_md)
        del bad["claude_md"]["insights"][0]["origin"]
        fails, _ = validate(bad, CLAUDE_MD_SCHEMA)
        assert fails


class TestUnionFloor:
    def test_conventions_only_passes_floor(self):
        result = check_claude_md_record_floor(CONVENTIONS_ONLY)
        assert result is not None and result.verdict == PASS

    def test_insights_only_passes_floor(self, minimal_claude_md):
        result = check_claude_md_record_floor(minimal_claude_md)
        assert result is not None and result.verdict == PASS

    def test_empty_block_fails_floor(self):
        data = copy.deepcopy(CONVENTIONS_ONLY)
        del data["claude_md"]["conventions"]
        result = check_claude_md_record_floor(data)
        assert result is not None and result.verdict == FAIL


class TestAuditIntegration:
    def _audit(self, tmp_path, data: dict) -> dict:
        p = tmp_path / "CLAUDE.md"
        p.write_text(
            "# dir insights\n\n```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n",
            encoding="utf-8",
        )
        return audit(p)

    def _fail_rows(self, report):
        return [r for r in report["yaml_contract"] if r["verdict"] == FAIL]

    def test_conventions_only_claude_md_audits_clean(self, tmp_path):
        report = self._audit(tmp_path, CONVENTIONS_ONLY)
        assert self._fail_rows(report) == []

    def test_empty_claude_md_block_fails_audit(self, tmp_path):
        data = copy.deepcopy(CONVENTIONS_ONLY)
        del data["claude_md"]["conventions"]
        report = self._audit(tmp_path, data)
        fails = self._fail_rows(report)
        assert any("union floor" in r["row"] for r in fails)
