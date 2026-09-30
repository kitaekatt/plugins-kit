"""md-domain's `render` verb: the instructions against the code and the registry.

Every row compares what the skill tells Claude with what the `material`
command does (skills_kit_lib/material.py), or with the lane registry, so a
change on either side turns a row red. The registry rows that an added lane
record reaches (roster, verb and axis, dispatch row, phrasings, bound paths)
are the existing ones in test_domain_members_resolve.py, driven by its
EXPECTED_LANES.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from skills_kit_lib import audit, material

REPO_ROOT = Path(__file__).resolve().parents[2]
MD_DOMAIN = REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
SKILL_MD = MD_DOMAIN / "SKILL.md"
RENDER_LANE = MD_DOMAIN / "references" / "lanes" / "render-lane.md"

FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")


def _skill_text() -> str:
    return SKILL_MD.read_text(encoding="utf-8")


def _lane_text() -> str:
    return RENDER_LANE.read_text(encoding="utf-8")


def _frontmatter() -> dict:
    match = re.match(r"---\n(.*?)\n---\n", _skill_text(), re.S)
    return yaml.safe_load(match.group(1))


def _render_record() -> dict:
    for block in re.findall(r"```yaml\n(.*?)\n```", _skill_text(), re.S):
        if block.lstrip().startswith("lanes:"):
            records = yaml.safe_load(block)["lanes"]["records"]
            return next(r for r in records if r["id"] == "render_skill")
    raise AssertionError("no lanes block")


def _parser_flags() -> set:
    parser = material.build_parser()
    return {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }


def _grammar_section() -> str:
    text = _skill_text()
    return text.split("## Argument grammar", 1)[1].split("## Review mode", 1)[0]


def _render_grammar_text() -> str:
    """The render form line and the render-arguments bullet of the grammar."""
    section = _grammar_section()
    form = next(line for line in section.splitlines() if line.startswith("Render form:"))
    bullet = section.split("- **Render arguments**", 1)[1].split("\n- **", 1)[0]
    return form + "\n" + bullet


def _verdict_table() -> dict:
    """{exit code: verdict} from the render lane's outcome table."""
    table = {}
    for line in _lane_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and cells[0].isdigit():
            table[int(cells[0])] = cells[1]
    return table


def test_render_verdicts_equal_the_command_outcomes():
    record = _render_record()
    assert set(record["verdicts"]) == set(material.OUTCOMES.values())
    assert len(record["verdicts"]) == len(set(record["verdicts"]))


def test_render_lane_exit_codes_match_the_module():
    assert _verdict_table() == material.OUTCOMES


def test_every_render_flag_is_accepted_by_the_parser():
    known = _parser_flags()
    for label, text in (("SKILL.md render grammar", _render_grammar_text()),
                        ("render-lane.md", _lane_text())):
        named = set(FLAG.findall(text))
        assert named, f"{label} names no flag"
        assert named <= known, f"{label} names flags the parser lacks: {sorted(named - known)}"


def test_every_parser_flag_is_named_in_the_render_lane():
    missing = {f for f in _parser_flags() if f not in _lane_text()}
    assert not missing, f"the parser has options render-lane.md does not document: {sorted(missing)}"


def test_render_lane_classifies_usage_outside_the_verdicts():
    text = _lane_text()
    assert re.search(r"Exit 2 is outside the verdicts", text)
    assert "no verdict is reported" in text
    assert 2 not in _verdict_table()
    assert len(_render_record()["verdicts"]) == 4


@pytest.mark.parametrize("term", ["SKILL.md", "CLAUDE.md", "docs"])
def test_description_keeps_skill_md_claude_md_and_docs(term):
    assert term in _frontmatter()["description"]


OWNER_RULED_DESCRIPTION = (
    "Use when auditing/authoring/generating/analyzing SKILL.md, CLAUDE.md, docs or "
    "rendering a skill prompt. Do NOT use for knowledge-encoding/update-documentation."
)


def test_description_names_rendering_and_fits_the_limit():
    description = _frontmatter()["description"]
    assert "rendering a skill" in description
    assert description.startswith("Use when")
    assert "Do NOT use for" in description
    assert "knowledge-encoding" in description and "update-documentation" in description
    assert len(description) <= audit.THRESHOLDS["desc_max_chars"]
    assert description == OWNER_RULED_DESCRIPTION
    assert len(description) == 159


def test_argument_grammar_and_hint_name_render():
    hint = _frontmatter()["argument-hint"]
    assert "render" in hint.split("]", 1)[0].split("|")
    section = _grammar_section()
    verb_line = next(line for line in section.splitlines() if line.startswith("- **Verb**"))
    assert "`render`" in verb_line
    assert "Render form: `render skill <path>..." in section
    assert "--budget <n>" in section


def test_greeting_offers_render():
    text = _skill_text()
    greeting = text.split("### Bare-invocation greeting", 1)[1].split("```", 2)[1]
    can_do = greeting.split("WHAT I CAN DO IT TO", 1)[0]
    assert re.search(r"^  render +\S", can_do, re.M), "no render verb line in WHAT I CAN DO"
    targets = greeting.split("WHAT I CAN DO IT TO", 1)[1].split("FOR EXAMPLE", 1)[0]
    skills_line = next(line for line in targets.splitlines() if line.lstrip().startswith("skills"))
    assert "render" in skills_line
    assert "render this skill" in greeting.split("FOR EXAMPLE", 1)[1]


def test_render_phrasings_do_not_claim_checking():
    for phrasing in _render_record()["invocation_phrasings"]:
        lowered = phrasing.lower()
        for word in ("check", "audit", "valid", "strict"):
            assert word not in lowered, f"{phrasing!r} claims {word}"
    lane = " ".join(_lane_text().split())
    assert "`audit skill`" in lane
    assert "check, validate or audit a skill" in lane


def test_render_lane_has_a_report_outcome_step_naming_exit_codes():
    lane = _lane_text()
    assert "## Step 3 -- Report the outcome" in lane
    step = lane.split("## Step 3 -- Report the outcome", 1)[1].split("\n## ", 1)[0]
    assert "for every exit code" in step
    assert "shown as printed" in step
    for code in (0, 1, 3, 4):
        assert f"| {code} |" in step


def test_render_lane_states_no_default_budget():
    lane = _lane_text()
    assert "ask for one and do" in lane and "not choose one" in lane
    assert "no default budget" in lane
    budget = next(
        action for action in material.build_parser()._actions if "--budget" in action.option_strings
    )
    assert budget.default is None and budget.required is True


def test_md_domain_skill_md_passes_the_audit():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "plugins" / "skills-kit")] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    run = subprocess.run(
        [sys.executable, "-m", "skills_kit_lib.audit", "--config", "--json", str(SKILL_MD)],
        cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    fails = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("verdict") == "fail":
                fails.append(node.get("row"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(json.loads(run.stdout))
    assert fails == [], f"md-domain's own audit fails: {fails}"
    # The control: the walk reads real rows, so a pass is not an empty report.
    assert '"verdict": "pass"' in run.stdout
