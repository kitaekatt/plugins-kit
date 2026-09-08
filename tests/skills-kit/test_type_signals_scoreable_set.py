"""I6: type_signals never scores "audit-skill" even though CANONICAL_TYPES
includes it, so a legacy audit-skill silently falls to another type or
indeterminate. Pin the set of types the function CAN score (returns a
nonzero signal for) and confirm audit-skill's absence is a stated
exclusion, not an oversight -- the returned scores dict still carries the
key (always 0), but it is documented as never incremented.
"""

from skills_kit_lib.markdown_heuristics import CANONICAL_TYPES, type_signals


# A body carrying at least one marker for every OTHER canonical type.
KITCHEN_SINK_BODY = (
    "# S\n\n"
    "## Red flags\n\nWatch out.\n\n"
    "RED -> GREEN -> REFACTOR\n\n"
    "This will recognize the pattern; a counter-example follows.\n\n"
    "1. Step one.\n2. Step two.\n3. Step three.\n4. Step four.\n\n"
    "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n\n"
    "## Conditional Loading\n\n- ref\n\n"
    "no sibling domains\n\n"
    "This skill wraps the widget tool.\n\n"
    "## Capability\n\nfoo\n"
)


class TestScoreableTypeSet:
    def test_scores_dict_always_carries_every_canonical_type(self):
        scores = type_signals(KITCHEN_SINK_BODY)
        assert set(scores) == set(CANONICAL_TYPES)

    def test_audit_skill_is_never_scored(self):
        """audit-skill's identity lives entirely in the structured
        criteria/taxonomy/procedures/remediations YAML contract; it carries
        no reliable narrative-only marker, so it is deliberately excluded
        from heuristic scoring rather than guessed at (I6)."""
        scores = type_signals(KITCHEN_SINK_BODY)
        assert scores["audit-skill"] == 0

    def test_scoreable_types_are_every_type_except_audit_skill(self):
        scores = type_signals(KITCHEN_SINK_BODY)
        scoreable = {t for t, s in scores.items() if s > 0}
        assert scoreable == set(CANONICAL_TYPES) - {"audit-skill"}
