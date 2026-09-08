"""I7: check_technique_skill does NOT thread frontmatter into an ordered-step
exemption for user-only skills (disable-model-invocation: true). The
skills_kit_lib/CLAUDE.md insight `user_only_via_disable_model_invocation`
claimed audit.check_technique_skill reports n/a for user-only skills; that
is false -- fm is accepted but never read, and Dec-2
(tests/skills-kit/test_schemas.py) pins the opposite at the schema layer.
This test pins the same fact at the legacy-fallback audit layer: a
user-only technique skill with no YAML contract and no ordered steps FAILs
technique-ordered-steps, it does not get n/a.
"""

from pathlib import Path

from skills_kit_lib.audit import audit, check_technique_skill
from skills_kit_lib.markdown_heuristics import parse_body, parse_frontmatter


def _write(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "s"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(
        "---\nname: s\ndescription: Use when X. Do NOT use for Y.\n"
        "skill-type: technique-skill\ndisable-model-invocation: true\n---\n"
        + body,
        encoding="utf-8",
    )
    return p


def _row(rows, name_substr):
    for r in rows:
        if name_substr in r["row"]:
            return r
    raise AssertionError(f"row '{name_substr}' not found in {rows}")


class TestUserOnlyDoesNotExemptOrderedSteps:
    def test_no_steps_fails_even_when_user_only(self, tmp_path):
        p = _write(tmp_path, "# S\n\nNo ordered steps at all, just prose.\n")
        report = audit(p)
        row = _row(report["type_specific"], "ordered-step body")
        assert row["verdict"] == "fail", row

    def test_check_technique_skill_ignores_fm_directly(self, tmp_path):
        content = (
            "---\nname: s\ndisable-model-invocation: true\n---\n"
            "# S\n\nNo ordered steps.\n"
        )
        fm = parse_frontmatter(content, mode="full")
        body = parse_body(content)
        results_with_fm = check_technique_skill(body, tmp_path, fm)
        results_without_fm = check_technique_skill(body, tmp_path, None)
        # fm is accepted (arity preserved for positional callers) but must
        # not change the outcome -- it is documented as unused.
        assert [r.verdict for r in results_with_fm] == [r.verdict for r in results_without_fm]
