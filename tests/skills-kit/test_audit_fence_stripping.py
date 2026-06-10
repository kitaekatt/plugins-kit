"""Tests for audit's legacy type-specific checks running on the fence-stripped
narrative body (arch-review S6, the strip_code_fences_before_heuristics
insight applied to the legacy-fallback path).

Numbered lists inside fenced code examples must not satisfy (or inflate) the
technique-skill ordered-step row; discipline/pattern/reference markers inside
fences must not fire either.
"""

from pathlib import Path

from skills_kit_lib.audit import audit


def _write_skill(tmp_path: Path, body: str, skill_type: str = "technique-skill") -> Path:
    d = tmp_path / "s"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text(
        f"---\nname: s\ndescription: d\nskill-type: {skill_type}\n---\n{body}",
        encoding="utf-8",
    )
    return p


def _row(report: dict, name_substr: str) -> dict:
    for r in report["type_specific"]:
        if name_substr in r["row"]:
            return r
    raise AssertionError(f"row '{name_substr}' not found in {report['type_specific']}")


class TestTechniqueStepsIgnoreFences:
    def test_steps_only_inside_code_fence_fail(self, tmp_path):
        body = (
            "# S\n\nRun the helper.\n\n"
            "```python\n# 1. first\n# 2. second\n1. looks like a step\n```\n"
        )
        report = audit(_write_skill(tmp_path, body))
        row = _row(report, "ordered-step body")
        assert row["verdict"] == "fail", row

    def test_narrative_steps_still_pass(self, tmp_path):
        body = "# S\n\n1. Do the thing.\n2. Verify the thing.\n"
        report = audit(_write_skill(tmp_path, body))
        row = _row(report, "ordered-step body")
        assert row["verdict"] == "pass", row

    def test_fenced_steps_do_not_inflate_step_count(self, tmp_path):
        # 2 narrative steps + 5 fenced ones: the >3-step conditional row must
        # be n/a (only 2 real steps), not triggered by the fenced lines.
        body = (
            "# S\n\n1. Do A.\n2. Do B.\n\n"
            "```text\n1. x\n2. x\n3. x\n4. x\n5. x\n```\n"
        )
        report = audit(_write_skill(tmp_path, body))
        row = _row(report, "explicit step-tracking")
        assert row["verdict"] == "n/a", row


class TestDisciplineMarkersIgnoreFences:
    def test_discipline_table_inside_fence_does_not_satisfy_rule_row(self, tmp_path):
        body = (
            "# S\n\nProse only.\n\n"
            "```markdown\n| excuse | reality |\n|---|---|\n| a | b |\n```\n"
        )
        report = audit(_write_skill(tmp_path, body, skill_type="discipline-skill"))
        row = _row(report, "rule + counter pair")
        assert row["verdict"] == "fail", row

    def test_narrative_discipline_table_satisfies_rule_row(self, tmp_path):
        body = (
            "# S\n\n| excuse | reality |\n|---|---|\n| too slow | costs more |\n"
        )
        report = audit(_write_skill(tmp_path, body, skill_type="discipline-skill"))
        row = _row(report, "rule + counter pair")
        assert row["verdict"] == "pass", row
