"""Schema validation tests for the skills-kit plugin.

Each canonical type has a "minimal floor" fixture that the schema accepts. Tests
mutate one field at a time -- removing a required key, swapping a type,
inserting a forbidden key, hedging a discipline rule -- and confirm the schema
catches each class of drift. This is the framework eating its own
self-correcting-loop dogfood: the validator must catch the failures the
framework documents.
"""

# schemas/_shared were extracted into skills_kit_lib (on sys.path via
# pyproject.toml [tool.pytest.ini_options].pythonpath = "plugins/skills-kit").
from skills_kit_lib.schemas.skill_types import (
    REFERENCE_SKILL_SCHEMA,
    PATTERN_SKILL_SCHEMA,
    TECHNIQUE_SKILL_SCHEMA,
    DISCIPLINE_SKILL_SCHEMA,
    DOMAIN_SKILL_SCHEMA,
)
from skills_kit_lib.schemas.claude_md import CLAUDE_MD_SCHEMA
from skills_kit_lib.schema_registry import (
    SCHEMAS_BY_ROOT,
    detect_mixed_type_yaml,
    resolve_schema,
)
from skills_kit_lib.schema_engine import validate

# The "at least one fact / gotcha / example" requirement moved OUT of the schema
# (facts: is now optional there, because facts may live nested in reference_skill:
# OR as a top-level portable facts: unit) and INTO an audit-time cross-source rule.
import yaml

from skills_kit_lib.audit import FAIL, check_facts_cross_rules


def _has_fail_at(fails, path_substring: str) -> bool:
    return any(path_substring in path for path, _ in fails)


def _has_fail_with_msg(fails, msg_substring: str) -> bool:
    return any(msg_substring in msg for _, msg in fails)


def _ref_body(data: dict) -> str:
    """Wrap a reference_skill dict as a SKILL.md body with a fenced yaml block."""
    return "```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n"


def _fail_rows(results):
    return [r for r in results if r.verdict == FAIL]


# ---------------------------------------------------------------------------
# Reference-skill
# ---------------------------------------------------------------------------


class TestReferenceSkill:
    def test_minimal_floor_validates(self, minimal_reference_skill):
        fails, _ = validate(minimal_reference_skill, REFERENCE_SKILL_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_missing_facts_fails(self, minimal_reference_skill, make_invalid):
        # Enforced at audit time (cross-source), not by the schema: a reference
        # skill must carry at least one fact somewhere (nested or top-level unit).
        bad = make_invalid(minimal_reference_skill, lambda d: d["reference_skill"].pop("facts"))
        results = check_facts_cross_rules(_ref_body(bad), "reference-skill")
        assert any("facts present" in r.row for r in _fail_rows(results))

    def test_empty_facts_list_fails_min_len(self, minimal_reference_skill, make_invalid):
        bad = make_invalid(minimal_reference_skill, lambda d: d["reference_skill"].update({"facts": []}))
        fails, _ = validate(bad, REFERENCE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "list length 0")

    def test_keywords_below_three_fails(self, minimal_reference_skill, make_invalid):
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"]["facts"][0].update({"keywords": ["only", "two"]}),
        )
        fails, _ = validate(bad, REFERENCE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "keywords")

    def test_facts_must_include_gotcha(self, minimal_reference_skill, make_invalid):
        # Cross-source audit rule: at least one fact must carry a gotchas list.
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"]["facts"][0].pop("gotchas"),
        )
        results = check_facts_cross_rules(_ref_body(bad), "reference-skill")
        assert any("gotchas" in r.row for r in _fail_rows(results))

    def test_facts_must_include_example(self, minimal_reference_skill, make_invalid):
        # Cross-source audit rule: at least one fact must carry an example block.
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"]["facts"][0].pop("example"),
        )
        results = check_facts_cross_rules(_ref_body(bad), "reference-skill")
        assert any("example" in r.row for r in _fail_rows(results))

    def test_forbidden_key_rules_fails(self, minimal_reference_skill, make_invalid):
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"].update({"rules": []}),
        )
        fails, _ = validate(bad, REFERENCE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "forbidden key")

    def test_extras_allowed(self, minimal_reference_skill, make_invalid):
        """Schemas-as-floors: unknown non-forbidden keys must NOT fail."""
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"].update({"my_extra": "anything"}),
        )
        fails, _ = validate(bad, REFERENCE_SKILL_SCHEMA)
        assert fails == [], f"extras should be allowed; got fails: {fails}"

    def test_missing_scope_excludes_fails(self, minimal_reference_skill, make_invalid):
        bad = make_invalid(
            minimal_reference_skill,
            lambda d: d["reference_skill"]["scope"].pop("excludes"),
        )
        fails, _ = validate(bad, REFERENCE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "scope.excludes")


# ---------------------------------------------------------------------------
# Pattern-skill
# ---------------------------------------------------------------------------


class TestPatternSkill:
    def test_minimal_floor_validates(self, minimal_pattern_skill):
        fails, _ = validate(minimal_pattern_skill, PATTERN_SKILL_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_missing_apply_when_fails(self, minimal_pattern_skill, make_invalid):
        bad = make_invalid(
            minimal_pattern_skill,
            lambda d: d["pattern_skill"]["patterns"][0].pop("apply_when"),
        )
        fails, _ = validate(bad, PATTERN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "apply_when")

    def test_missing_do_not_apply_when_fails(self, minimal_pattern_skill, make_invalid):
        bad = make_invalid(
            minimal_pattern_skill,
            lambda d: d["pattern_skill"]["patterns"][0].pop("do_not_apply_when"),
        )
        fails, _ = validate(bad, PATTERN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "do_not_apply_when")

    def test_missing_examples_fails(self, minimal_pattern_skill, make_invalid):
        bad = make_invalid(
            minimal_pattern_skill,
            lambda d: d["pattern_skill"]["patterns"][0].pop("examples"),
        )
        fails, _ = validate(bad, PATTERN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "examples")


# ---------------------------------------------------------------------------
# Technique-skill
# ---------------------------------------------------------------------------


class TestTechniqueSkill:
    def test_minimal_floor_validates(self, minimal_technique_skill):
        fails, _ = validate(minimal_technique_skill, TECHNIQUE_SKILL_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_missing_steps_fails(self, minimal_technique_skill, make_invalid):
        """Dec-2: ordered steps universal on technique-skills."""
        bad = make_invalid(
            minimal_technique_skill,
            lambda d: d["technique_skill"]["techniques"][0].pop("steps"),
        )
        fails, _ = validate(bad, TECHNIQUE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "steps")

    def test_empty_steps_fails(self, minimal_technique_skill, make_invalid):
        bad = make_invalid(
            minimal_technique_skill,
            lambda d: d["technique_skill"]["techniques"][0].update({"steps": []}),
        )
        fails, _ = validate(bad, TECHNIQUE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "list length 0")

    def test_user_only_still_requires_steps(self, minimal_technique_skill, make_invalid):
        """Dec-2: trigger_model is metadata; user-only does NOT exempt from steps."""
        bad = make_invalid(
            minimal_technique_skill,
            lambda d: (
                d["technique_skill"].update({"trigger_model": "user-only"}),
                d["technique_skill"]["techniques"][0].pop("steps"),
            ),
        )
        fails, _ = validate(bad, TECHNIQUE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "steps")

    def test_resolve_technique_dispatches_user_only(self):
        data = {"technique_skill": {"trigger_model": "user-only"}}
        root, schema = resolve_schema(data)
        assert root == "technique_skill"
        # trigger_model is metadata; user-only resolves to the one unified schema.
        assert schema["root"] == "technique_skill"

    def test_missing_gotchas_fails(self, minimal_technique_skill, make_invalid):
        bad = make_invalid(
            minimal_technique_skill,
            lambda d: d["technique_skill"]["techniques"][0].pop("gotchas"),
        )
        fails, _ = validate(bad, TECHNIQUE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "gotchas")

    def test_forbidden_key_rules_fails(self, minimal_technique_skill, make_invalid):
        bad = make_invalid(
            minimal_technique_skill,
            lambda d: d["technique_skill"].update({"rules": []}),
        )
        fails, _ = validate(bad, TECHNIQUE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "forbidden key")


# ---------------------------------------------------------------------------
# Discipline-skill
# ---------------------------------------------------------------------------


class TestDisciplineSkill:
    def test_minimal_floor_validates(self, minimal_discipline_skill):
        fails, _ = validate(minimal_discipline_skill, DISCIPLINE_SKILL_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_hedging_statement_fails(self, minimal_discipline_skill, make_invalid):
        """Discipline rules must not hedge."""
        bad = make_invalid(
            minimal_discipline_skill,
            lambda d: d["discipline_skill"]["rules"][0].update(
                {"statement": "You should usually do X before Y."}
            ),
        )
        fails, _ = validate(bad, DISCIPLINE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "must not hedge")

    def test_consider_phrasing_fails(self, minimal_discipline_skill, make_invalid):
        bad = make_invalid(
            minimal_discipline_skill,
            lambda d: d["discipline_skill"]["rules"][0].update(
                {"statement": "Consider doing X."}
            ),
        )
        fails, _ = validate(bad, DISCIPLINE_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "must not hedge")

    def test_counter_missing_observed_in_fails(self, minimal_discipline_skill, make_invalid):
        bad = make_invalid(
            minimal_discipline_skill,
            lambda d: d["discipline_skill"]["rules"][0]["counters"][0].pop("observed_in"),
        )
        fails, _ = validate(bad, DISCIPLINE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "observed_in")

    def test_pressure_test_required(self, minimal_discipline_skill, make_invalid):
        bad = make_invalid(
            minimal_discipline_skill,
            lambda d: d["discipline_skill"].pop("pressure_test"),
        )
        fails, _ = validate(bad, DISCIPLINE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "pressure_test")

    def test_red_flags_required(self, minimal_discipline_skill, make_invalid):
        bad = make_invalid(
            minimal_discipline_skill,
            lambda d: d["discipline_skill"]["rules"][0].pop("red_flags"),
        )
        fails, _ = validate(bad, DISCIPLINE_SKILL_SCHEMA)
        assert _has_fail_at(fails, "red_flags")


# ---------------------------------------------------------------------------
# Domain-skill
# ---------------------------------------------------------------------------


class TestDomainSkill:
    def test_minimal_floor_validates(self, minimal_domain_skill):
        fails, _ = validate(minimal_domain_skill, DOMAIN_SKILL_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_missing_companions_fails(self, minimal_domain_skill, make_invalid):
        bad = make_invalid(
            minimal_domain_skill,
            lambda d: d["domain_skill"].pop("companions"),
        )
        fails, _ = validate(bad, DOMAIN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "companions")

    def test_missing_index_fails(self, minimal_domain_skill, make_invalid):
        bad = make_invalid(
            minimal_domain_skill,
            lambda d: d["domain_skill"].pop("index"),
        )
        fails, _ = validate(bad, DOMAIN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "index")

    def test_index_reference_keywords_below_three_fails(self, minimal_domain_skill, make_invalid):
        bad = make_invalid(
            minimal_domain_skill,
            lambda d: d["domain_skill"]["index"]["references"][0].update(
                {"keywords": ["only", "two"]}
            ),
        )
        fails, _ = validate(bad, DOMAIN_SKILL_SCHEMA)
        assert _has_fail_at(fails, "keywords")

    def test_forbidden_key_rules_fails(self, minimal_domain_skill, make_invalid):
        bad = make_invalid(
            minimal_domain_skill,
            lambda d: d["domain_skill"].update({"rules": []}),
        )
        fails, _ = validate(bad, DOMAIN_SKILL_SCHEMA)
        assert _has_fail_with_msg(fails, "forbidden key")


# ---------------------------------------------------------------------------
# claude_md
# ---------------------------------------------------------------------------


class TestClaudeMd:
    def test_minimal_floor_validates(self, minimal_claude_md):
        fails, _ = validate(minimal_claude_md, CLAUDE_MD_SCHEMA)
        assert fails == [], f"unexpected fails: {fails}"

    def test_insights_key_is_optional_at_schema_level(self, minimal_claude_md, make_invalid):
        # Dec-19: the non-empty floor is the insights/conventions UNION, enforced
        # as a document-level check (see test_claude_md_floor.py). Dropping the
        # insights KEY is no longer a schema failure -- a conventions-only
        # CLAUDE.md is a valid shape.
        bad = make_invalid(
            minimal_claude_md,
            lambda d: d["claude_md"].pop("insights"),
        )
        fails, _ = validate(bad, CLAUDE_MD_SCHEMA)
        assert not _has_fail_at(fails, "insights")

    def test_insight_missing_origin_fails(self, minimal_claude_md, make_invalid):
        bad = make_invalid(
            minimal_claude_md,
            lambda d: d["claude_md"]["insights"][0].pop("origin"),
        )
        fails, _ = validate(bad, CLAUDE_MD_SCHEMA)
        assert _has_fail_at(fails, "origin")

    def test_insight_missing_added_fails(self, minimal_claude_md, make_invalid):
        bad = make_invalid(
            minimal_claude_md,
            lambda d: d["claude_md"]["insights"][0].pop("added"),
        )
        fails, _ = validate(bad, CLAUDE_MD_SCHEMA)
        assert _has_fail_at(fails, "added")


# ---------------------------------------------------------------------------
# Mixed-type detection
# ---------------------------------------------------------------------------


class TestMixedType:
    def test_single_root_no_mixed_signal(self, minimal_reference_skill):
        roots = detect_mixed_type_yaml(minimal_reference_skill)
        assert roots == ["reference_skill"]

    def test_two_roots_signals_mixed_type(self, minimal_reference_skill, minimal_technique_skill):
        merged = {**minimal_reference_skill, **minimal_technique_skill}
        roots = detect_mixed_type_yaml(merged)
        assert set(roots) == {"reference_skill", "technique_skill"}
        assert len(roots) > 1

    def test_resolve_schema_picks_first(self, minimal_pattern_skill):
        root, schema = resolve_schema(minimal_pattern_skill)
        assert root == "pattern_skill"
        assert schema["root"] == "pattern_skill"

    def test_resolve_schema_returns_none_for_unknown_root(self):
        data = {"some_random_root": {}}
        root, schema = resolve_schema(data)
        assert root is None
        assert schema is None


# ---------------------------------------------------------------------------
# Schema registry sanity
# ---------------------------------------------------------------------------


class TestSchemaRegistry:
    def test_all_canonical_roots_registered(self):
        # Every canonical root resolves through SCHEMAS_BY_ROOT; technique_skill
        # included (the per-variant resolver and its aliases were deleted -- the
        # unified schema is the only technique schema). The known set:
        for root in ("reference_skill", "pattern_skill", "technique_skill",
                     "discipline_skill", "domain_skill", "claude_md"):
            assert root in SCHEMAS_BY_ROOT, f"{root} missing from SCHEMAS_BY_ROOT"

    def test_each_schema_declares_root(self):
        for root, schema in SCHEMAS_BY_ROOT.items():
            assert schema.get("root") == root

    def test_each_skill_schema_declares_forbidden_keys(self):
        skill_roots = ["reference_skill", "pattern_skill", "discipline_skill", "domain_skill"]
        for root in skill_roots:
            assert "forbidden_keys" in SCHEMAS_BY_ROOT[root]
            assert isinstance(SCHEMAS_BY_ROOT[root]["forbidden_keys"], list)
            assert len(SCHEMAS_BY_ROOT[root]["forbidden_keys"]) > 0
