"""Tests for markdown_heuristics consolidation fixes (arch-review S7, S11, S13).

- S7: has_excuse_reality_table matches a table/record SHAPE, not the bare
  substring "rationalization" (which fired on any skill *discussing*
  rationalization counters).
- S11: CANONICAL_TYPES is derived from the registry's SKILL_TYPE_ROOTS.
- S13: parse_frontmatter is the one frontmatter parser, with light (regex)
  and full (pyyaml) modes.
"""

from skills_kit_lib.markdown_heuristics import (
    CANONICAL_TYPES,
    has_excuse_reality_table,
    parse_frontmatter,
)
from skills_kit_lib.schema_registry import SKILL_TYPE_ROOTS


class TestExcuseRealityShape:
    def test_prose_mention_of_rationalization_does_not_fire(self):
        body = (
            "# Skill\n\nThis skill discusses rationalization counters and how "
            "agents rationalize skipping steps. Pure prose, no table.\n"
        )
        assert has_excuse_reality_table(body) is False

    def test_three_column_table_fires(self):
        body = "| excuse | foo | reality |\n|---|---|---|\n| a | b | c |\n"
        assert has_excuse_reality_table(body) is True

    def test_two_column_table_fires(self):
        body = "| Excuse | Reality |\n|---|---|\n| too slow | costs more later |\n"
        assert has_excuse_reality_table(body) is True

    def test_record_pair_fires(self):
        body = "- excuse: it's faster to skip X\n- reality: skipping X costs more\n"
        assert has_excuse_reality_table(body) is True

    def test_bold_record_pair_fires(self):
        body = "**Excuse:** just this once\n**Reality:** never just once\n"
        assert has_excuse_reality_table(body) is True

    def test_excuse_without_reality_does_not_fire(self):
        body = "- excuse: it's faster\n- consequence: slower later\n"
        assert has_excuse_reality_table(body) is False

    def test_plain_body_does_not_fire(self):
        assert has_excuse_reality_table("# T\n\nNothing relevant here.\n") is False


class TestCanonicalTypesDerived:
    def test_derived_from_skill_type_roots(self):
        assert CANONICAL_TYPES == {r.replace("_", "-") for r in SKILL_TYPE_ROOTS}

    def test_known_members(self):
        # Sanity: the registry currently ships these seven.
        for t in ("reference-skill", "pattern-skill", "technique-skill",
                  "discipline-skill", "domain-skill", "capability-skill",
                  "audit-skill"):
            assert t in CANONICAL_TYPES


DOC = """---
name: my-skill
skill-type: technique-skill
disable-model-invocation: true
---
# Body
"""


class TestParseFrontmatterModes:
    def test_light_mode_returns_string_fields(self):
        fm = parse_frontmatter(DOC)
        assert fm is not None
        assert fm.fields["name"] == "my-skill"
        assert fm.fields["disable-model-invocation"] == "true"  # string in light mode

    def test_full_mode_returns_typed_fields(self):
        fm = parse_frontmatter(DOC, mode="full")
        assert fm is not None
        assert fm.fields["name"] == "my-skill"
        assert fm.fields["disable-model-invocation"] is True  # real bool

    def test_full_mode_invalid_yaml_degrades_to_empty(self):
        fm = parse_frontmatter("---\n: : not valid : yaml :\n---\n# T\n", mode="full")
        assert fm is not None
        assert fm.fields == {}

    def test_no_frontmatter_returns_none_in_both_modes(self):
        assert parse_frontmatter("# T\nno frontmatter\n") is None
        assert parse_frontmatter("# T\nno frontmatter\n", mode="full") is None
