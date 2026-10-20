"""The legacy per-skill-type heuristic criteria (M6 wave 5).

The ids in LEGACY_IDS are criteria of the shipped SKILL-legacy-standards.md,
evaluated by audit.MECHANICAL_EVALUATORS in the `type_specific` section and
absent from rule_catalog.RULES. They keep three behaviours of the hard-coded
checks they replaced: they have a row only for a SKILL.md with no staged YAML
contract, only for the declared skill-type their statement names, and against
the body with fenced code removed. They also keep their real verdict on a bare
interpreter. These tests pin each of those, plus the red-on-revert property
that the shipped file, not code, decides which rows an audit emits.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from skills_kit_lib import audit as audit_mod
from skills_kit_lib import rule_catalog
from skills_kit_lib import standards_resolve as sr
from skills_kit_lib.audit import (
    FAIL,
    JUDGMENT,
    MECHANICAL_EVALUATORS,
    NA,
    PASS,
    MechanicalCriterion,
    MechanicalEvaluator,
    SkillContext,
    _criterion_rows,
    audit,
)
from skills_kit_lib.markdown_heuristics import Body, Frontmatter

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
SHIPPED_DIR = PLUGIN / "skills_kit_lib" / "defaults" / "standards"
LEGACY_FILE = SHIPPED_DIR / "SKILL-legacy-standards.md"
OTHER_SHIPPED = [SHIPPED_DIR / "SKILL-standards.md", SHIPPED_DIR / "CLAUDE-md-standards.md"]

# The legacy ids by declared skill-type, in the order their rows appear. A type
# is listed in MIGRATED_TYPES once its ids have moved into the shipped file.
ROWS_BY_TYPE = {
    "reference-skill": ["ref-example-block", "ref-gotcha-block",
                        "ref-prohibited-discipline", "ref-prohibited-checklist"],
    "pattern-skill": ["pattern-recognition-block", "pattern-counter-example",
                      "pattern-prohibited-bundle", "pattern-prohibited-checklist",
                      "pattern-prohibited-rule-counter"],
    "technique-skill": ["technique-ordered-steps", "step-tracking",
                        "technique-prohibited-pressure-test"],
    "technique-wrapper": ["wrapper-ordered-step", "wrapper-procedure-cited", "wrapper-max-lines"],
    "discipline-skill": ["discipline-rule-counter", "discipline-red-flags", "discipline-pressure-test"],
    "domain-skill": ["domain-identity-sentence", "domain-companion-declaration", "domain-orientation",
                     "domain-reference-index", "domain-prohibited-index-only"],
}
MIGRATED_TYPES = ("reference-skill", "pattern-skill", "technique-skill", "technique-wrapper",
                  "discipline-skill", "domain-skill")
LEGACY_IDS = [rid for t in MIGRATED_TYPES for rid in ROWS_BY_TYPE[t]]
TYPE_OF = {rid: t for t in MIGRATED_TYPES for rid in ROWS_BY_TYPE[t]}

# The severity each criterion declares: what a miss reports.
SEVERITY = {
    "ref-example-block": "judgment", "ref-gotcha-block": "judgment",
    "ref-prohibited-discipline": "fail", "ref-prohibited-checklist": "fail",
    "pattern-recognition-block": "judgment", "pattern-counter-example": "judgment",
    "pattern-prohibited-bundle": "fail", "pattern-prohibited-checklist": "fail",
    "pattern-prohibited-rule-counter": "fail",
    "technique-ordered-steps": "fail", "step-tracking": "fail",
    "technique-prohibited-pressure-test": "fail",
    "wrapper-ordered-step": "fail", "wrapper-procedure-cited": "fail", "wrapper-max-lines": "fail",
    "discipline-rule-counter": "fail", "discipline-red-flags": "fail", "discipline-pressure-test": "fail",
    "domain-identity-sentence": "judgment", "domain-companion-declaration": "fail",
    "domain-orientation": "fail", "domain-reference-index": "fail", "domain-prohibited-index-only": "fail",
}

H1 = "# Subject skill\n\n"
BLANK = H1 + "Prose only.\n"
STEPS_2 = "1. one\n2. two\n"
STEPS_3 = STEPS_2 + "3. three\n"
STEPS_4 = STEPS_3 + "4. four\n"
WRAPPER_CITES = "wrapper cites its procedure document"
WRAPPER_LINES = "wrapper stub within 40 lines"
STEP_TRACKING = "explicit step-tracking (conditional, IF >3 steps): checklist OR tracker invocation"

# (id, scenario, body, directories to create, expected (label, verdict, note)).
CASES = [
    ("ref-example-block", "miss", BLANK, (),
     (">=1 example block", JUDGMENT, "no 'Example' heading detected")),
    ("ref-example-block", "hit", H1 + "## Examples\n\nfoo\n", (),
     (">=1 example block", PASS, "")),
    ("ref-gotcha-block", "miss", BLANK, (),
     (">=1 gotcha block", JUDGMENT, "no 'Gotcha' heading detected")),
    ("ref-gotcha-block", "hit", H1 + "### Known gotchas\n\nbar\n", (),
     (">=1 gotcha block", PASS, "")),
    ("ref-prohibited-discipline", "miss", BLANK, (),
     ("prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)", PASS, "")),
    ("ref-prohibited-discipline", "hit-red-green-refactor",
     H1 + "Apply RED -> GREEN -> REFACTOR.\n", (),
     ("prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)", FAIL,
      "discipline markers detected")),
    ("ref-prohibited-discipline", "hit-excuse-table",
     H1 + "| excuse | reality |\n|---|---|\n| a | b |\n", (),
     ("prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)", FAIL,
      "discipline markers detected")),
    ("ref-prohibited-checklist", "miss", BLANK, (),
     ("prohibited: workflow checklist", PASS, "")),
    ("ref-prohibited-checklist", "hit", H1 + "- [ ] do a thing\n", (),
     ("prohibited: workflow checklist", FAIL, "tickbox list present")),
    ("pattern-recognition-block", "miss", BLANK, (),
     ("recognition criteria block", JUDGMENT, "no 'recognize/recognition/applies when' marker")),
    ("pattern-recognition-block", "hit-recognize", H1 + "Recognize this shape by its second step.\n", (),
     ("recognition criteria block", PASS, "")),
    ("pattern-recognition-block", "hit-applies-when", H1 + "It applies when two lists disagree.\n", (),
     ("recognition criteria block", PASS, "")),
    ("pattern-counter-example", "miss", BLANK, (),
     ("counter-example(s) block", JUDGMENT, "no 'counter-example' or 'do NOT apply' marker")),
    ("pattern-counter-example", "hit-counter-example", H1 + "A counter-example: a single list.\n", (),
     ("counter-example(s) block", PASS, "")),
    ("pattern-counter-example", "hit-do-not-apply", H1 + "Do NOT apply to single lists.\n", (),
     ("counter-example(s) block", PASS, "")),
    ("pattern-prohibited-bundle", "miss", BLANK, (),
     ("prohibited: utility bundle", PASS, "")),
    ("pattern-prohibited-bundle", "miss-references-only", BLANK, ("references",),
     ("prohibited: utility bundle", PASS, "")),
    ("pattern-prohibited-bundle", "hit-scripts", BLANK, ("scripts",),
     ("prohibited: utility bundle", FAIL, "scripts/ or bin/ present")),
    ("pattern-prohibited-bundle", "hit-bin", BLANK, ("bin",),
     ("prohibited: utility bundle", FAIL, "scripts/ or bin/ present")),
    ("pattern-prohibited-checklist", "miss", BLANK, (),
     ("prohibited: workflow checklist", PASS, "")),
    ("pattern-prohibited-checklist", "hit", H1 + "- [ ] do a thing\n", (),
     ("prohibited: workflow checklist", FAIL, "")),
    ("pattern-prohibited-rule-counter", "miss", BLANK, (),
     ("prohibited: rule + counter pairs", PASS, "")),
    ("pattern-prohibited-rule-counter", "hit-table", H1 + "| excuse | reality |\n|---|---|\n| a | b |\n", (),
     ("prohibited: rule + counter pairs", FAIL, "rationalization/excuse->reality detected")),
    ("pattern-prohibited-rule-counter", "hit-record", H1 + "excuse: too slow\nreality: costs more\n", (),
     ("prohibited: rule + counter pairs", FAIL, "rationalization/excuse->reality detected")),
    ("technique-ordered-steps", "miss", BLANK, (),
     ("ordered-step body", FAIL, "0 ordered-step entries detected")),
    ("technique-ordered-steps", "hit", H1 + STEPS_2, (),
     ("ordered-step body", PASS, "2 ordered-step entries detected")),
    ("step-tracking", "na-no-steps", BLANK, (),
     (STEP_TRACKING, NA, "only 0 steps")),
    ("step-tracking", "na-three-steps", H1 + STEPS_3, (),
     (STEP_TRACKING, NA, "only 3 steps")),
    ("step-tracking", "fail-neither", H1 + STEPS_4, (),
     (STEP_TRACKING, FAIL, "4 steps; neither tickbox checklist nor step-tracker invocation present")),
    ("step-tracking", "pass-checklist", H1 + STEPS_4 + "- [ ] a tick\n", (),
     (STEP_TRACKING, PASS, "4 steps; satisfied via tickbox checklist")),
    ("step-tracking", "pass-tracker", H1 + STEPS_4 + "Use TaskCreate first.\n", (),
     (STEP_TRACKING, PASS, "4 steps; satisfied via step-tracker invocation")),
    ("step-tracking", "pass-both-reports-checklist", H1 + STEPS_4 + "- [ ] a tick\nUse TaskCreate.\n", (),
     (STEP_TRACKING, PASS, "4 steps; satisfied via tickbox checklist")),
    ("wrapper-ordered-step", "miss", BLANK, (),
     ("ordered-step body", FAIL, "0 ordered-step entries detected")),
    ("wrapper-ordered-step", "hit", H1 + STEPS_2, (),
     ("ordered-step body", PASS, "2 ordered-step entries detected")),
    ("wrapper-procedure-cited", "miss-no-citation", BLANK, (),
     (WRAPPER_CITES, FAIL, "no references/<file>.md citation in the body")),
    ("wrapper-procedure-cited", "miss-file-absent", H1 + "1. Follow `references/nope.md`.\n", ("references",),
     (WRAPPER_CITES, FAIL, "cited but missing: ['nope.md']")),
    ("wrapper-procedure-cited", "hit-skill-dir-form",
     H1 + "1. Follow `${CLAUDE_SKILL_DIR}/references/x.md`.\n", ("references",),
     (WRAPPER_CITES, PASS, "cites 1 reference file(s)")),
    ("wrapper-procedure-cited", "hit-bare-form", H1 + "1. Follow `references/x.md`.\n", ("references",),
     (WRAPPER_CITES, PASS, "cites 1 reference file(s)")),
    ("wrapper-max-lines", "hit", H1 + STEPS_2, (),
     (WRAPPER_LINES, PASS, "4 lines")),
    ("wrapper-max-lines", "miss-inlined-body", H1 + "line\n" * 100, (),
     (WRAPPER_LINES, FAIL, "102 lines")),
    ("technique-prohibited-pressure-test", "miss", BLANK, (),
     ("prohibited: adversarial pressure testing", PASS, "")),
    ("technique-prohibited-pressure-test", "hit", H1 + "Apply RED -> GREEN -> REFACTOR.\n", (),
     ("prohibited: adversarial pressure testing", FAIL, "RED/GREEN/REFACTOR markers present")),
    ("discipline-rule-counter", "miss", BLANK, (),
     (">=1 rule + counter pair", FAIL, "no rule+counter / rationalization markers")),
    ("discipline-rule-counter", "hit-table", H1 + "| excuse | reality |\n|---|---|\n| a | b |\n", (),
     (">=1 rule + counter pair", PASS, "")),
    ("discipline-rule-counter", "hit-record", H1 + "excuse: too slow\nreality: costs more\n", (),
     (">=1 rule + counter pair", PASS, "")),
    ("discipline-red-flags", "miss", BLANK, (),
     ("red flags list", FAIL, "no 'Red flags' heading")),
    ("discipline-red-flags", "hit", H1 + "## Red flags\n\n- bad\n", (),
     ("red flags list", PASS, "")),
    ("discipline-pressure-test", "miss", BLANK, (),
     ("adversarial pressure testing applied", FAIL, "no RED/GREEN/REFACTOR markers")),
    ("discipline-pressure-test", "markers-present-needs-judgment", H1 + "Run RED -> GREEN -> REFACTOR on each rule.\n", (),
     ("adversarial pressure testing applied", JUDGMENT,
      "RED/GREEN/REFACTOR markers present; agent must verify pressure testing was applied to this skill's own rules")),
    ("domain-identity-sentence", "hit", BLANK, (),
     ("identity sentence", PASS, "")),
    ("domain-identity-sentence", "miss-no-period", H1 + "No terminator here\n", (),
     ("identity sentence", JUDGMENT, "no clear single-sentence identity after H1")),
    ("domain-identity-sentence", "miss-no-h1", "just text.\n", (),
     ("identity sentence", JUDGMENT, "no clear single-sentence identity after H1")),
    ("domain-identity-sentence", "miss-too-long", H1 + "x" * 700 + ".\n", (),
     ("identity sentence", JUDGMENT, "no clear single-sentence identity after H1")),
    ("domain-companion-declaration", "miss", BLANK, (),
     ("companion declaration", FAIL,
      "no 'Companion declaration' heading or 'no sibling' / 'companion domains' phrase")),
    ("domain-companion-declaration", "hit-heading", H1 + "## Companions\n\nfoo\n", (),
     ("companion declaration", PASS, "")),
    ("domain-companion-declaration", "hit-no-sibling", H1 + "There are no sibling domains.\n", (),
     ("companion declaration", PASS, "")),
    ("domain-companion-declaration", "hit-companion-domains", H1 + "See the companion domain for X.\n", (),
     ("companion declaration", PASS, "")),
    ("domain-orientation", "miss-no-h2", BLANK, (),
     ("orientation content (>=1 H2 beyond index)", FAIL, "0 H2 sections")),
    ("domain-orientation", "miss-one-h2", H1 + "## Only\n\nx\n", (),
     ("orientation content (>=1 H2 beyond index)", FAIL, "1 H2 sections")),
    ("domain-orientation", "hit", H1 + "## One\n\nx\n\n## Two\n\ny\n", (),
     ("orientation content (>=1 H2 beyond index)", PASS, "2 H2 sections")),
    ("domain-reference-index", "miss", BLANK, (),
     ("reference index (Conditional Loading)", FAIL, "")),
    ("domain-reference-index", "hit", H1 + "## Conditional Loading\n\n- x\n", (),
     ("reference index (Conditional Loading)", PASS, "")),
    ("domain-prohibited-index-only", "violation", H1 + "## Conditional Loading\n\n- x\n", (),
     ("prohibited: index without orientation", FAIL, "only Conditional Loading H2; no orientation content")),
    ("domain-prohibited-index-only", "ok-no-h2", BLANK, (),
     ("prohibited: index without orientation", PASS, "")),
    ("domain-prohibited-index-only", "ok-one-other-h2", H1 + "## Only\n\nx\n", (),
     ("prohibited: index without orientation", PASS, "")),
    ("domain-prohibited-index-only", "ok-index-plus-orientation", H1 + "## Conditional Loading\n\n- x\n\n## More\n\ny\n", (),
     ("prohibited: index without orientation", PASS, "")),
]

# A marker that appears ONLY inside a fenced block must not count: the id, the
# fenced body, and the expected row. Each is the "miss" outcome for a present
# marker, or the "pass" outcome for a prohibited one.
FENCED = [
    ("ref-example-block", H1 + "```markdown\n## Example\n\nfoo\n```\n", JUDGMENT),
    ("ref-gotcha-block", H1 + "```markdown\n## Gotcha\n\nbar\n```\n", JUDGMENT),
    ("ref-prohibited-discipline", H1 + "```markdown\nRED -> GREEN -> REFACTOR\n```\n", PASS),
    ("ref-prohibited-checklist", H1 + "```markdown\n- [ ] item\n```\n", PASS),
    ("pattern-recognition-block", H1 + "```markdown\nRecognize this shape.\n```\n", JUDGMENT),
    ("pattern-counter-example", H1 + "```markdown\nA counter-example.\n```\n", JUDGMENT),
    ("pattern-prohibited-checklist", H1 + "```markdown\n- [ ] item\n```\n", PASS),
    ("pattern-prohibited-rule-counter",
     H1 + "```markdown\n| excuse | reality |\n|---|---|\n| a | b |\n```\n", PASS),
    ("technique-ordered-steps", H1 + "```text\n1. a\n2. b\n```\n", FAIL),
    ("step-tracking", H1 + STEPS_2 + "```text\n3. c\n4. d\n5. e\n```\n", NA),
    ("wrapper-ordered-step", H1 + "```text\n1. a\n2. b\n```\n", FAIL),
    ("wrapper-procedure-cited", H1 + "```text\nreferences/x.md\n```\n", FAIL),
    ("technique-prohibited-pressure-test", H1 + "```markdown\nRED -> GREEN -> REFACTOR\n```\n", PASS),
    ("discipline-rule-counter",
     H1 + "```markdown\n| excuse | reality |\n|---|---|\n| a | b |\n```\n", FAIL),
    ("discipline-red-flags", H1 + "```markdown\n## Red flags\n```\n", FAIL),
    ("discipline-pressure-test", H1 + "```markdown\nRED -> GREEN -> REFACTOR\n```\n", FAIL),
    ("domain-identity-sentence", H1 + "```markdown\nIdentity here.\n```\n", JUDGMENT),
    ("domain-companion-declaration", H1 + "```markdown\n## Companions\n```\n", FAIL),
    ("domain-orientation", H1 + "```markdown\n## A\n\n## B\n```\n", FAIL),
    ("domain-reference-index", H1 + "```markdown\n## Conditional Loading\n```\n", FAIL),
    ("domain-prohibited-index-only", H1 + "## Conditional Loading\n\n```markdown\n## Extra\n```\n", FAIL),
]

CONTRACT_BLOCK = {
    "reference-skill": "```yaml\nreference_skill:\n  _schema_version: \"1\"\n```\n",
    "pattern-skill": "```yaml\npattern_skill:\n  _schema_version: \"1\"\n```\n",
    "technique-skill": "```yaml\ntechnique_skill:\n  _schema_version: \"1\"\n```\n",
    "technique-wrapper": "```yaml\ntechnique_wrapper:\n  procedure: references/x.md\n```\n",
    "discipline-skill": "```yaml\ndiscipline_skill:\n  _schema_version: \"1\"\n```\n",
    "domain-skill": "```yaml\ndomain_skill:\n  _schema_version: \"1\"\n```\n",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _hermetic_user_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))


def _user_layer(tmp_path):
    layer = tmp_path / "config" / "skills-kit"
    layer.mkdir(parents=True, exist_ok=True)
    return layer


def _skill(tmp_path, skill_type, body=BLANK, dirs=(), name="subject"):
    """A SKILL.md tagged skill_type (None omits the tag), plus empty dirs."""
    d = tmp_path / name
    d.mkdir()
    for sub in dirs:
        (d / sub).mkdir()
        (d / sub / "x.sh").write_text("x", encoding="utf-8")
        (d / sub / "x.md").write_text("x", encoding="utf-8")
    type_line = "" if skill_type is None else f"skill-type: {skill_type}\n"
    (d / "SKILL.md").write_text(
        f"---\nname: subject\ndescription: Use when X. Do NOT use for Y.\n{type_line}---\n{body}",
        encoding="utf-8",
    )
    return d / "SKILL.md"


def _legacy_rows(report):
    """The legacy rows of a report, keyed by rule id (insertion = row order)."""
    return {r["rule"]: r for r in report["type_specific"] if r["rule"] in LEGACY_IDS}


def _shipped_block(path=LEGACY_FILE):
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("```yaml\n", 1)[1].split("```", 1)[0])


def _write_shipped(tmp_path, mutate):
    """Copy the shipped directory, mutating the criteria of the legacy file."""
    d = tmp_path / "shipped"
    d.mkdir()
    for src in OTHER_SHIPPED:
        shutil.copy(src, d / src.name)
    data = _shipped_block()
    mutate(data["standards_set"]["criteria"])
    (d / LEGACY_FILE.name).write_text(
        "# Shipped copy\n\n```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n",
        encoding="utf-8",
    )
    return d


def _bare(code):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(PLUGIN)
    prelude = "import sys\nsys.modules['yaml'] = None\n"
    return subprocess.run([sys.executable, "-c", prelude + code],
                          capture_output=True, text=True, env=env, timeout=60)


def _bare_report(path):
    proc = _bare(
        "import json\nfrom skills_kit_lib.audit import audit\n"
        f"print(json.dumps(audit(__import__('pathlib').Path({str(path)!r}))))\n"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _standards_md(criteria_yaml):
    return (
        "# Authored\n\n```yaml\nstandards_set:\n"
        "  identity: An authored set for SKILL.md files.\n"
        "  applies_to: skill_md\n"
        "  criteria:\n" + textwrap.indent(textwrap.dedent(criteria_yaml), "    ") + "```\n"
    )


# ---------------------------------------------------------------------------
# where the criteria live
# ---------------------------------------------------------------------------


def test_all_twenty_three_legacy_ids_have_moved_into_the_shipped_file():
    """The 19 per-type ids, step-tracking and the three technique-wrapper ids, no more and no fewer."""
    assert len(LEGACY_IDS) == 23
    assert set(MIGRATED_TYPES) == {"reference-skill", "pattern-skill", "technique-skill", "technique-wrapper",
                                   "discipline-skill", "domain-skill"}
    assert "step-tracking" in LEGACY_IDS
    assert LEGACY_IDS == [c["id"] for c in _shipped_block()["standards_set"]["criteria"]]


def test_the_catalog_keeps_only_the_coverage_ids_as_optional():
    """Settled decision D2: the eight coverage ids stay registered in the
    catalog; their criteria are shipped data in coverage-standards.md."""
    coverage = {"absent-fact-earns-ambient-cost", "already-ambient-suppressed",
                "fact-scoped-to-this-directory", "candidate-tier-classified", "hazard-durability",
                "loud-failure-excluded", "evidence-floor", "present-content-not-re-audited"}
    assert set(rule_catalog.optional_rule_ids()) == coverage
    assert {g for b, g, _d in rule_catalog.RULES.values() if b == "optional"} == {"coverage-criteria"}


def test_the_per_type_runners_survive_only_as_deprecation_shims_audit_does_not_call():
    """The runners left audit() when their rows became criteria; the names stay
    for the deprecation window (docs/reference/deprecation-policy.md) and no
    code path inside audit.py runs them."""
    shims = {"check_reference_skill", "check_pattern_skill", "check_technique_skill",
             "check_discipline_skill", "check_domain_skill"}
    assert {n for n in dir(audit_mod) if n.startswith("check_") and n.endswith("_skill")} == shims
    with pytest.warns(DeprecationWarning):
        assert set(audit_mod.TYPE_RUNNERS) <= {n.removeprefix("check_").replace("_", "-") for n in shims}
    import ast

    tree = ast.parse(Path(audit_mod.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name not in shims | {"__getattr__"}:
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            assert not used & shims, f"{node.name} calls a deprecated runner: {sorted(used & shims)}"


def test_the_legacy_ids_left_the_catalog():
    for rid in LEGACY_IDS:
        assert rid not in rule_catalog.RULES
        assert rule_catalog.BUCKETS.get(rid) is None


def test_the_legacy_file_declares_exactly_the_migrated_criteria_in_row_order():
    data = _shipped_block()["standards_set"]
    assert data["applies_to"] == "skill_md"
    criteria = data["criteria"]
    assert [c["id"] for c in criteria] == LEGACY_IDS
    assert all(c["enforcement"] == "mechanical" for c in criteria)
    assert all("threshold" not in c for c in criteria)
    assert all(len(c["keywords"]) >= 3 for c in criteria)
    assert {c["id"]: c["severity"] for c in criteria} == SEVERITY


def test_every_legacy_evaluator_is_a_type_specific_skill_md_evaluator():
    for rid in LEGACY_IDS:
        ev = MECHANICAL_EVALUATORS[rid]
        assert (ev.applies_to, ev.section, ev.threshold) == ("skill_md", "type_specific", None)


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_bare_severity_equals_the_severity_the_shipped_file_declares(rid):
    """The bare-interpreter copy of a severity may not drift from the file."""
    assert MECHANICAL_EVALUATORS[rid].bare_severity == SEVERITY[rid]


def test_the_resolved_shipped_layer_holds_the_legacy_file():
    files = sr.resolve_shipped().standards_by_primitive["skill_md"]
    assert LEGACY_FILE in [sf.path for sf in files]
    assert sr.SHIPPED_LEGACY_STANDARDS in sr.SHIPPED_REQUIRED_STANDARDS


# ---------------------------------------------------------------------------
# exact rows, hit and miss
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rid,scenario,body,dirs,expected", CASES,
                         ids=[f"{c[0]}-{c[1]}" for c in CASES])
def test_row_label_note_and_verdict(tmp_path, rid, scenario, body, dirs, expected):
    row = _legacy_rows(audit(_skill(tmp_path, TYPE_OF[rid], body, dirs)))[rid]
    assert (row["row"], row["verdict"], row["note"]) == expected
    assert row["rule"] == rid


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_every_id_has_a_hit_and_a_miss_case(rid):
    scenarios = {c[1] for c in CASES if c[0] == rid}
    assert len(scenarios) >= 2, f"{rid} needs a hit and a miss in CASES"


@pytest.mark.parametrize("skill_type", MIGRATED_TYPES)
def test_a_type_gets_its_rows_in_order_in_the_type_specific_list(tmp_path, skill_type):
    report = audit(_skill(tmp_path, skill_type))
    assert [r["rule"] for r in report["type_specific"] if r["rule"] in LEGACY_IDS] == ROWS_BY_TYPE[skill_type]
    assert not any(r["rule"] in LEGACY_IDS for r in report["universal"] + report["yaml_contract"])


# ---------------------------------------------------------------------------
# scope: declared type, staged contract, narrative body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("other", [None, "bogus-skill", "capability-skill", "audit-skill",
                                   "pattern-skill", "technique-skill", "technique-wrapper",
                                   "discipline-skill", "domain-skill", "reference-skill"])
def test_a_row_is_emitted_only_for_its_own_declared_type(tmp_path, other):
    report = audit(_skill(tmp_path, other))
    emitted = [r["rule"] for r in report["type_specific"] if r["rule"] in LEGACY_IDS]
    assert emitted == (ROWS_BY_TYPE.get(other, []) if other in MIGRATED_TYPES else [])


@pytest.mark.parametrize("skill_type", MIGRATED_TYPES)
def test_a_staged_contract_suppresses_every_legacy_row(tmp_path, skill_type):
    path = _skill(tmp_path, skill_type, BLANK + CONTRACT_BLOCK[skill_type])
    report = audit(path)
    assert report["yaml_root"] == skill_type.replace("-", "_")
    assert _legacy_rows(report) == {}
    bare = _bare_report(path)
    assert [r for r in bare["type_specific"] if r["rule"] in LEGACY_IDS] == []


@pytest.mark.parametrize("rid,body,verdict", FENCED, ids=[f[0] for f in FENCED])
def test_a_marker_inside_a_code_fence_is_ignored(tmp_path, rid, body, verdict):
    row = _legacy_rows(audit(_skill(tmp_path, TYPE_OF[rid], body)))[rid]
    assert row["verdict"] == verdict


def test_a_fenced_block_counts_toward_the_wrapper_length(tmp_path):
    """Unlike a marker, length is what every firing injects: fenced lines are paid for too."""
    body = H1 + "1. Follow `references/x.md`.\n\n```text\n" + "l\n" * 60 + "```\n"
    row = _legacy_rows(audit(_skill(tmp_path, "technique-wrapper", body, ("references",))))["wrapper-max-lines"]
    assert row["verdict"] == FAIL


def test_the_wrapper_length_limit_is_tunable_through_thresholds(tmp_path):
    """wrapper_max_lines is a built-in threshold: raising it turns the miss into a pass."""
    (_user_layer(tmp_path) / "config.yaml").write_text("thresholds:\n  wrapper_max_lines: 200\n", encoding="utf-8")
    resolved = sr.resolve(None)
    body = H1 + "1. Follow `references/x.md`.\n" + "line\n" * 100
    report = audit(_skill(tmp_path, "technique-wrapper", body, ("references",)), resolved)
    assert _legacy_rows(report)["wrapper-max-lines"]["verdict"] == PASS


def test_the_type_specific_context_carries_the_fence_stripped_body_and_the_staged_flag():
    fm = Frontmatter(raw="", fields={"skill-type": "reference-skill"})
    body = Body(text="# T\n", lines=1, tokens_approx=1)
    ev = MECHANICAL_EVALUATORS["ref-example-block"]
    assert ev.applies(SkillContext(fm, body, Path("."), None, False))
    assert not ev.applies(SkillContext(fm, body, Path("."), None, True))
    assert not ev.applies(SkillContext(Frontmatter(raw="", fields={}), body, Path("."), None, False))


# ---------------------------------------------------------------------------
# red on revert: the file decides the rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_removing_a_criterion_from_the_shipped_file_drops_only_its_row(tmp_path, monkeypatch, rid):
    """Red on revert: with the check back in hard-coded code the row survives
    the edit of the file and this test fails."""
    path = _skill(tmp_path, TYPE_OF[rid])
    baseline = audit(path)
    assert rid in _legacy_rows(baseline)

    def drop(cs):
        cs[:] = [c for c in cs if c["id"] != rid]

    shipped = _write_shipped(tmp_path, drop)
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    assert report["type_specific"] == [r for r in baseline["type_specific"] if r["rule"] != rid]
    assert report["universal"] == baseline["universal"]
    assert report["yaml_contract"] == baseline["yaml_contract"]


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_the_severity_in_the_file_decides_what_a_miss_reports(tmp_path, monkeypatch, rid):
    """A fail criterion softened to info reports judgment on a miss; a judgment
    criterion hardened to fail reports fail. The pass outcome is untouched."""
    declared = SEVERITY[rid]
    flipped = "info" if declared == "fail" else "fail"

    def flip(cs):
        for c in cs:
            if c["id"] == rid:
                c["severity"] = flipped

    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, flip))
    miss = next(c for c in CASES if c[0] == rid and c[4][1] in (FAIL, JUDGMENT))
    row = _legacy_rows(audit(_skill(tmp_path, TYPE_OF[rid], miss[2], miss[3])))[rid]
    assert row["verdict"] == (JUDGMENT if flipped == "info" else FAIL)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_rules_off_removes_only_that_row(tmp_path, rid):
    (_user_layer(tmp_path) / "config.yaml").write_text(f"rules:\n  {rid}: off\n", encoding="utf-8")
    resolved = sr.resolve(None)
    assert resolved.disabled_rules == {rid}
    rows = _legacy_rows(audit(_skill(tmp_path, TYPE_OF[rid]), resolved))
    assert list(rows) == [r for r in ROWS_BY_TYPE[TYPE_OF[rid]] if r != rid]


def test_opting_out_of_the_shipped_layer_removes_every_legacy_row(tmp_path):
    resolved = sr.resolve(None, shipped_dir=None)
    for skill_type in MIGRATED_TYPES:
        report = audit(_skill(tmp_path, skill_type, name=f"s-{skill_type}"), resolved)
        assert _legacy_rows(report) == {}


def test_legacy_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n" + "".join(f"  {rid}: off\n" for rid in LEGACY_IDS), encoding="utf-8")
    assert sr.resolve(None, shipped_dir=None).disabled_rules == set(LEGACY_IDS)


@pytest.mark.parametrize("rid", LEGACY_IDS)
def test_authored_id_repeating_a_legacy_criterion_raises_naming_both(tmp_path, rid):
    layer = _user_layer(tmp_path)
    (layer / "SKILL-standards.md").write_text(_standards_md(f"""\
        - id: {rid}
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    msg = str(exc.value)
    assert rid in msg and str(LEGACY_FILE) in msg and str(layer / "SKILL-standards.md") in msg


# ---------------------------------------------------------------------------
# loud failure: the legacy file is required
# ---------------------------------------------------------------------------


def _shipped_without_legacy(tmp_path):
    d = tmp_path / "shipped"
    d.mkdir()
    for src in OTHER_SHIPPED:
        shutil.copy(src, d / src.name)
    return d


def test_a_shipped_dir_without_the_legacy_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _shipped_without_legacy(tmp_path))
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "SKILL-legacy-standards.md" in str(exc.value)
    with pytest.raises(sr.StandardsConfigError):
        audit(_skill(tmp_path, "reference-skill"))


def test_a_missing_legacy_file_exits_1_for_a_skill_audit(tmp_path, monkeypatch, capsys):
    path = _skill(tmp_path, "reference-skill")
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _shipped_without_legacy(tmp_path))
    assert audit_mod.main([str(path)]) == 1
    assert "SKILL-legacy-standards.md" in capsys.readouterr().err


def test_without_pyyaml_a_missing_legacy_file_still_exits_1(tmp_path):
    path = _skill(tmp_path, "reference-skill")
    d = _shipped_without_legacy(tmp_path)
    proc = _bare(
        "from pathlib import Path\nfrom skills_kit_lib import standards_resolve as sr\n"
        f"sr.SHIPPED_STANDARDS_DIR = Path({str(d)!r})\n"
        "from skills_kit_lib import audit\n"
        f"raise SystemExit(audit.main([{str(path)!r}]))\n"
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SKILL-legacy-standards.md" in proc.stderr


# ---------------------------------------------------------------------------
# no pyyaml: the rows keep their real verdicts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_type", MIGRATED_TYPES)
@pytest.mark.parametrize("scenario", ["blank", "marked"])
def test_without_pyyaml_the_rows_equal_the_ones_with_it(tmp_path, skill_type, scenario):
    """Byte-for-byte: the same rows, labels, notes and verdicts on a bare
    interpreter as under the plugin venv, not deferred rows."""
    marked = H1 + "## Example\n\nx\n\n- [ ] tick\n\nRED -> GREEN -> REFACTOR\n"
    path = _skill(tmp_path, skill_type, BLANK if scenario == "blank" else marked)
    expected = audit(path)["type_specific"]
    assert any(r["rule"] in LEGACY_IDS for r in expected)
    assert _bare_report(path)["type_specific"] == expected
    assert not any("(deferred)" in r["row"] for r in expected)


# ---------------------------------------------------------------------------
# the outcome vocabulary the legacy rows rely on
# ---------------------------------------------------------------------------


def _fake(outcome, bare_severity=None):
    return MechanicalEvaluator(
        "skill_md", None, "fake (deferred)", lambda ctx: True,
        lambda ctx, th: ("fake row", outcome, "note"),
        section="type_specific", bare_severity=bare_severity)


def _ctx():
    return SkillContext(Frontmatter(raw="", fields={}), Body(text="", lines=0, tokens_approx=0), Path("."))


@pytest.mark.parametrize("severity", ["fail", "judgment", "info"])
def test_a_judgment_outcome_is_judgment_whatever_the_severity(monkeypatch, severity):
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "fake", _fake(JUDGMENT))
    rows = _criterion_rows(_ctx(), [MechanicalCriterion("fake", severity)], {}, section="type_specific")
    assert [(r.verdict) for _s, r in rows] == [JUDGMENT]


def test_a_string_outcome_other_than_judgment_is_an_error(monkeypatch):
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "fake", _fake("pass"))
    with pytest.raises(ValueError, match="JUDGMENT"):
        _criterion_rows(_ctx(), [MechanicalCriterion("fake", "fail")], {}, section="type_specific")


def test_a_criterion_without_a_readable_severity_is_deferred_unless_it_has_a_bare_severity(monkeypatch):
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "fake", _fake(False))
    deferred = _criterion_rows(_ctx(), [MechanicalCriterion("fake", None)], {}, section="type_specific")
    assert [(r.row, r.verdict) for _s, r in deferred] == [("fake (deferred)", JUDGMENT)]
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "fake", _fake(False, bare_severity="fail"))
    bare = _criterion_rows(_ctx(), [MechanicalCriterion("fake", None)], {}, section="type_specific")
    assert [(r.row, r.verdict) for _s, r in bare] == [("fake row", FAIL)]


def test_the_na_outcome_still_means_na(monkeypatch):
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "fake", _fake(None))
    rows = _criterion_rows(_ctx(), [MechanicalCriterion("fake", "fail")], {}, section="type_specific")
    assert [r.verdict for _s, r in rows] == [NA]
