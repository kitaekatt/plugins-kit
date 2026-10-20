"""The shipped standards layer (skills_kit_lib/defaults/standards/) and the
mechanical evaluators that serve its criteria.

desc-160-char, desc-directive-form, desc-exclusion-clause, skill-type-tag,
skill-type-valid, body-line-count, body-token-count, body-size-signal and
refs-one-hop-deep, facts-floor, facts-gotcha, facts-example and caution-floor
are criteria of the shipped SKILL-standards.md; the legacy per-skill-type
heuristic ids (LEGACY_IDS) are criteria of the shipped SKILL-legacy-standards.md;
claude-md-import-size, claude-md-size-signal and claude-md-record-floor are
criteria of the shipped CLAUDE-md-standards.md. All of them are evaluated by
audit.MECHANICAL_EVALUATORS and absent from rule_catalog.RULES. These tests pin
that the file, not the code, decides which rows an audit emits (the
red-on-revert tests drop a criterion from a copy of the file), and that every
way the layer can be broken is loud.
"""

import dataclasses
import importlib.util
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
from skills_kit_lib.audit import FAIL, JUDGMENT, MECHANICAL_EVALUATORS, PASS, audit
from skills_kit_lib.markdown_heuristics import CANONICAL_TYPES

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = REPO_ROOT / "plugins" / "skills-kit"
SHIPPED_DIR = PLUGIN / "skills_kit_lib" / "defaults" / "standards"
SHIPPED_FILE = SHIPPED_DIR / "SKILL-standards.md"
LEGACY_SHIPPED_FILE = SHIPPED_DIR / "SKILL-legacy-standards.md"
CLAUDE_SHIPPED_FILE = SHIPPED_DIR / "CLAUDE-md-standards.md"
REFERENCES_STANDARDS = PLUGIN / "skills" / "md-domain" / "references" / "standards"
RESOLVE_CLI = PLUGIN / "scripts" / "resolve_standards.py"

DESC_IDS = ["desc-160-char", "desc-directive-form", "desc-exclusion-clause"]
SHIPPED_IDS = [*DESC_IDS, "skill-type-tag", "skill-type-valid"]
BODY_IDS = ["body-line-count", "body-token-count", "body-size-signal", "refs-one-hop-deep"]
CONTRACT_IDS = ["facts-floor", "facts-gotcha", "facts-example", "caution-floor"]
SKILL_IDS = [*SHIPPED_IDS, *BODY_IDS, *CONTRACT_IDS]
REF_LEGACY_IDS = ["ref-example-block", "ref-gotcha-block", "ref-prohibited-discipline", "ref-prohibited-checklist"]
PATTERN_LEGACY_IDS = ["pattern-recognition-block", "pattern-counter-example", "pattern-prohibited-bundle",
                      "pattern-prohibited-checklist", "pattern-prohibited-rule-counter"]
TECHNIQUE_LEGACY_IDS = ["technique-ordered-steps", "step-tracking", "technique-prohibited-pressure-test"]
WRAPPER_LEGACY_IDS = ["wrapper-ordered-step", "wrapper-procedure-cited", "wrapper-max-lines"]
DISCIPLINE_LEGACY_IDS = ["discipline-rule-counter", "discipline-red-flags", "discipline-pressure-test"]
DOMAIN_LEGACY_IDS = ["domain-identity-sentence", "domain-companion-declaration", "domain-orientation",
                     "domain-reference-index", "domain-prohibited-index-only"]
LEGACY_IDS = [*REF_LEGACY_IDS, *PATTERN_LEGACY_IDS, *TECHNIQUE_LEGACY_IDS, *WRAPPER_LEGACY_IDS,
              *DISCIPLINE_LEGACY_IDS, *DOMAIN_LEGACY_IDS]
CLAUDE_IDS = ["claude-md-import-size", "claude-md-size-signal", "claude-md-record-floor"]


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


def _shipped_block(path=SHIPPED_FILE):
    text = path.read_text(encoding="utf-8")
    block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


def _write_shipped(tmp_path, mutate, which=SHIPPED_FILE):
    """Copy the shipped directory into a tmp dir, mutating the block of one
    file (SKILL by default); the other required file is copied verbatim."""
    d = tmp_path / "shipped"
    d.mkdir()
    for src in (SHIPPED_FILE, LEGACY_SHIPPED_FILE, CLAUDE_SHIPPED_FILE):
        if src != which:
            shutil.copy(src, d / src.name)
    data = _shipped_block(which)
    mutate(data["standards_set"]["criteria"])
    (d / which.name).write_text(
        "# Shipped copy\n\n```yaml\n" + yaml.safe_dump(data, sort_keys=False) + "```\n",
        encoding="utf-8",
    )
    return d


def _skill(tmp_path, description="Use when doing X. Do NOT use for Y.", skill_type="reference-skill"):
    """A SKILL.md fixture; description=None or skill_type=None omits that field."""
    d = tmp_path / "subject"
    d.mkdir()
    p = d / "SKILL.md"
    desc_line = "" if description is None else f"description: {description}\n"
    type_line = "" if skill_type is None else f"skill-type: {skill_type}\n"
    p.write_text(
        f"---\nname: subject\n{desc_line}{type_line}---\n"
        "# Subject\n\n## Example\n\nfoo\n\n## Gotcha\n\nbar\n",
        encoding="utf-8",
    )
    return p


def _rows(report):
    return {r["rule"]: r for r in report["universal"]}


def _all_claude_rows(report):
    """Every row of a CLAUDE.md report, universal then yaml_contract."""
    return {r["rule"]: r for r in [*report["universal"], *report["yaml_contract"]]}


CLAUDE_BLOCK = (
    "```yaml\nclaude_md:\n  _schema_version: \"1\"\n"
    "  scope:\n    directory: x\n    covers: [a]\n    excludes: [b]\n"
    "  conventions:\n    - rule: r\n      keywords: [k1, k2, k3]\n      why: w\n```\n"
)


def _claude_md(tmp_path, *, block=True, extra="", name="CLAUDE.md", lines=0):
    """A CLAUDE.md with one existing @import; block=False omits claude_md:."""
    d = tmp_path / "cm"
    d.mkdir(exist_ok=True)
    (d / "imp.md").write_text("line\n" * 8, encoding="utf-8")
    p = d / name
    p.write_text(
        "# Notes\n\n@imp.md\n\n" + (CLAUDE_BLOCK if block else "") + extra + "word\n" * lines,
        encoding="utf-8",
    )
    return p


def _standards_md(criteria_yaml):
    return (
        "# Authored\n\n```yaml\nstandards_set:\n"
        "  identity: An authored set for SKILL.md files.\n"
        "  applies_to: skill_md\n"
        "  criteria:\n" + textwrap.indent(textwrap.dedent(criteria_yaml), "    ") + "```\n"
    )


# ---------------------------------------------------------------------------
# where the five criteria live
# ---------------------------------------------------------------------------


def test_the_shipped_ids_left_the_catalog_and_nothing_else_did():
    for rid in [*SKILL_IDS, *LEGACY_IDS, *CLAUDE_IDS]:
        assert rid not in rule_catalog.RULES
    buckets = [b for b, _g, _d in rule_catalog.RULES.values()]
    assert (buckets.count("optional"), buckets.count("inoffensive"),
            buckets.count("architectural")) == (8, 11, 3)
    assert "import_max_lines" not in audit_mod.THRESHOLDS


def test_shipped_file_declares_the_thirteen_mechanical_criteria():
    criteria = _shipped_block()["standards_set"]["criteria"]
    assert [c["id"] for c in criteria] == SKILL_IDS
    assert all(c["enforcement"] == "mechanical" for c in criteria)
    assert criteria[0]["threshold"] == {"name": "desc_max_chars", "default": 160}
    assert {c["id"]: c["severity"] for c in criteria[3:]} == {
        "skill-type-tag": "judgment", "skill-type-valid": "fail",
        "body-line-count": "info", "body-token-count": "info",
        "body-size-signal": "judgment", "refs-one-hop-deep": "fail",
        "facts-floor": "fail", "facts-gotcha": "fail", "facts-example": "fail",
        "caution-floor": "fail"}
    assert all(len(c["keywords"]) >= 3 for c in criteria)
    assert "desc_max_chars" not in audit_mod.THRESHOLDS
    # The body limits are built in (D1): no body criterion declares a threshold.
    assert all("threshold" not in c for c in criteria[1:])
    assert {"body_max_lines", "body_max_tokens"} <= set(audit_mod.THRESHOLDS)


def test_shipped_claude_md_file_declares_the_three_mechanical_criteria():
    criteria = _shipped_block(CLAUDE_SHIPPED_FILE)["standards_set"]["criteria"]
    assert _shipped_block(CLAUDE_SHIPPED_FILE)["standards_set"]["applies_to"] == "claude_md"
    assert [c["id"] for c in criteria] == CLAUDE_IDS
    assert all(c["enforcement"] == "mechanical" for c in criteria)
    assert {c["id"]: c["severity"] for c in criteria} == {
        "claude-md-import-size": "fail",
        "claude-md-size-signal": "judgment",
        "claude-md-record-floor": "fail",
    }
    # The import limit is owned by its criterion; the body limits are built in
    # and are declared by no criterion.
    assert criteria[0]["threshold"] == {"name": "import_max_lines", "default": 50}
    assert all("threshold" not in c for c in criteria[1:])
    assert all(len(c["keywords"]) >= 3 for c in criteria)


def test_registry_keys_are_exactly_the_shipped_mechanical_ids():
    """No evaluator without a shipped criterion, no criterion without one."""
    assert list(MECHANICAL_EVALUATORS) == [*SKILL_IDS, *LEGACY_IDS, *CLAUDE_IDS]
    assert {ev.applies_to for rid, ev in MECHANICAL_EVALUATORS.items() if rid in CLAUDE_IDS} == {"claude_md"}


def test_resolve_loads_the_shipped_layer_by_default():
    r = sr.resolve(None)
    files = r.standards_by_primitive["skill_md"]
    # Two shipped skill_md files, in sorted file-name order.
    assert [(sf.layer, sf.path) for sf in files] == [
        ("shipped", LEGACY_SHIPPED_FILE), ("shipped", SHIPPED_FILE)]
    cfiles = r.standards_by_primitive["claude_md"]
    assert [(sf.layer, sf.path) for sf in cfiles] == [("shipped", CLAUDE_SHIPPED_FILE)]
    assert r.threshold_defaults == {"desc_max_chars": 160, "import_max_lines": 50}
    assert sr.resolve(None, shipped_dir=None).standards_by_primitive == {}


# ---------------------------------------------------------------------------
# red on revert: the file decides the rows
# ---------------------------------------------------------------------------


def test_removing_skill_type_valid_from_the_shipped_file_drops_only_its_row(tmp_path, monkeypatch):
    """Red on revert: with the extraction undone (the skill-type block back in
    check_universal), the row survives the file edit and this test fails."""
    path = _skill(tmp_path)
    baseline = audit(path)
    assert "skill-type-valid" in _rows(baseline)

    shipped = _write_shipped(
        tmp_path, lambda cs: cs.__setitem__(slice(None), [c for c in cs if c["id"] != "skill-type-valid"]))
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    expected = [r for r in baseline["universal"] if r["rule"] != "skill-type-valid"]
    assert report["universal"] == expected


def test_removing_a_criterion_from_the_shipped_file_drops_only_its_row(tmp_path, monkeypatch):
    path = _skill(tmp_path)
    baseline = audit(path)
    assert set(DESC_IDS) <= set(_rows(baseline))

    def drop_exclusion(cs):
        cs[:] = [c for c in cs if c["id"] != "desc-exclusion-clause"]

    shipped = _write_shipped(tmp_path, drop_exclusion)
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    expected = [r for r in baseline["universal"] if r["rule"] != "desc-exclusion-clause"]
    assert report["universal"] == expected


def test_rows_sit_right_after_desc_present_in_file_order(tmp_path):
    rules = [r["rule"] for r in audit(_skill(tmp_path))["universal"]]
    i = rules.index("desc-present")
    assert rules[i + 1:i + 5] == [*DESC_IDS, "skill-type-valid"]
    assert "skill-type-tag" not in rules


def test_each_skill_type_row_appears_only_when_it_applies(tmp_path):
    """Exact label, note and verdict of the two skill-type rows."""
    tag = _rows(audit(_skill(tmp_path, skill_type=None)))
    assert "skill-type-valid" not in tag
    assert tag["skill-type-tag"] == {
        "row": "skill-type advisory tag",
        "verdict": JUDGMENT,
        "note": "no skill-type frontmatter; agent infers type from content",
        "rule": "skill-type-tag",
    }

    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    ok = _rows(audit(_skill(ok_dir)))
    assert "skill-type-tag" not in ok
    assert ok["skill-type-valid"] == {
        "row": "skill-type value valid", "verdict": PASS,
        "note": "reference-skill", "rule": "skill-type-valid"}

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bad = _rows(audit(_skill(bad_dir, skill_type="bogus-skill")))
    assert "skill-type-tag" not in bad
    row = bad["skill-type-valid"]
    assert (row["row"], row["verdict"]) == ("skill-type value valid", FAIL)
    assert row["note"] == f"got 'bogus-skill', expected one of {sorted(CANONICAL_TYPES)}"


def test_skill_type_row_is_emitted_when_the_description_is_absent(tmp_path):
    rows = audit(_skill(tmp_path, description=None))["universal"]
    rules = [r["rule"] for r in rows]
    i = rules.index("desc-present")
    assert rows[i]["verdict"] == FAIL
    assert rules[i + 1] == "skill-type-valid"
    assert not set(DESC_IDS) & set(rules)


def test_non_fail_severity_reports_judgment_not_fail(tmp_path, monkeypatch):
    def soften(cs):
        for c in cs:
            if c["id"] == "desc-directive-form":
                c["severity"] = "info"
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, soften))
    rows = _rows(audit(_skill(tmp_path, description="Provides X. Do NOT use for Y.")))
    assert rows["desc-directive-form"]["verdict"] == JUDGMENT
    assert rows["desc-exclusion-clause"]["verdict"] == PASS


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_skill_type_valid_below_fail_severity_reports_judgment(tmp_path, monkeypatch):
    def soften(cs):
        for c in cs:
            if c["id"] == "skill-type-valid":
                c["severity"] = "info"
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, soften))
    rows = _rows(audit(_skill(tmp_path, skill_type="bogus-skill")))
    assert rows["skill-type-valid"]["verdict"] == JUDGMENT


def test_rules_off_removes_a_skill_type_row(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  skill-type-tag: off\n  skill-type-valid: off\n", encoding="utf-8")
    resolved = sr.resolve(None)
    assert resolved.disabled_rules == {"skill-type-tag", "skill-type-valid"}
    untagged = _rows(audit(_skill(tmp_path, skill_type=None), resolved))
    assert "skill-type-tag" not in untagged
    other = tmp_path / "other"
    other.mkdir()
    assert "skill-type-valid" not in _rows(audit(_skill(other), resolved))
    assert "desc-160-char" in untagged


def test_skill_type_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  skill-type-valid: off\n  skill-type-tag: off\n", encoding="utf-8")
    r = sr.resolve(None, shipped_dir=None)
    assert r.disabled_rules == {"skill-type-valid", "skill-type-tag"}


def test_rules_off_removes_the_row(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  desc-directive-form: off\n", encoding="utf-8")
    report = audit(_skill(tmp_path), sr.resolve(None))
    assert "desc-directive-form" not in _rows(report)
    assert "desc-160-char" in _rows(report)


def test_threshold_override_changes_label_and_verdict(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "thresholds:\n  desc_max_chars: 20\n", encoding="utf-8")
    row = _rows(audit(_skill(tmp_path), sr.resolve(None)))["desc-160-char"]
    assert row["row"] == "description <= 20 chars"
    assert row["verdict"] == FAIL


def test_shipped_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  desc-160-char: off\nthresholds:\n  desc_max_chars: 120\n", encoding="utf-8")
    r = sr.resolve(None, shipped_dir=None)
    assert r.disabled_rules == {"desc-160-char"}
    assert r.thresholds == {"desc_max_chars": 120}


# ---------------------------------------------------------------------------
# loud failures
# ---------------------------------------------------------------------------


def test_missing_shipped_layer_raises_and_audit_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", tmp_path / "absent")
    with pytest.raises(sr.StandardsConfigError):
        sr.resolve(None)
    path = _skill(tmp_path)
    with pytest.raises(sr.StandardsConfigError):
        audit(path)
    assert audit_mod.main([str(path)]) == 1
    assert "absent" in capsys.readouterr().err
    assert audit_mod.main([str(path), "--config"]) == 1


def test_shipped_dir_without_skill_standards_file_raises(tmp_path, monkeypatch):
    d = tmp_path / "shipped"
    d.mkdir()
    (d / "doc-standards.md").write_text(_standards_md("""\
        - id: some-doc-opinion
          statement: A doc names its reader.
          severity: info
          keywords: [reader, audience, doc]
        """).replace("skill_md", "plain_md"), encoding="utf-8")
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", d)
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "SKILL-standards.md" in str(exc.value)


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_claude_md_audit_needs_the_shipped_layer(tmp_path, monkeypatch, name, capsys):
    """A CLAUDE.md / AGENTS.md audit resolves the shipped claude_md criteria, so
    a missing layer stops it loudly instead of dropping three rows."""
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", tmp_path / "absent")
    d = tmp_path / "proj"
    d.mkdir()
    (d / name).write_text("# Notes\n\nSome guidance.\n", encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError):
        audit(d / name)
    assert audit_mod.main([str(d / name)]) == 1
    assert "absent" in capsys.readouterr().err


def test_shipped_dir_without_the_claude_md_file_raises(tmp_path, monkeypatch):
    d = tmp_path / "shipped"
    d.mkdir()
    shutil.copy(SHIPPED_FILE, d / SHIPPED_FILE.name)
    shutil.copy(LEGACY_SHIPPED_FILE, d / LEGACY_SHIPPED_FILE.name)
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", d)
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "CLAUDE-md-standards.md" in str(exc.value)
    skill = _skill(tmp_path)
    with pytest.raises(sr.StandardsConfigError):
        audit(skill)


@pytest.mark.parametrize("doc", ["skill-standards.md", "coverage-standards.md"])
def test_references_standards_docs_are_fatal_as_a_shipped_dir(tmp_path, doc):
    """Both P2 failure modes, separately: a doc without a standards_set block,
    and coverage-standards.md's code_directory subject."""
    d = tmp_path / "shipped"
    d.mkdir()
    shutil.copy(REFERENCES_STANDARDS / doc, d / doc)
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None, shipped_dir=d)
    msg = str(exc.value)
    if doc == "skill-standards.md":
        assert "no fenced `standards_set:` YAML block" in msg
    else:
        assert "code_directory" in msg


def test_duplicate_id_within_one_list_raises(tmp_path):
    (_user_layer(tmp_path) / "SKILL-standards.md").write_text(_standards_md("""\
        - id: one-opinion
          statement: First.
          severity: info
          keywords: [a, b, c]
        - id: one-opinion
          statement: Second.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "criteria[0]" in str(exc.value) and "criteria[1]" in str(exc.value)


def test_authored_id_repeating_a_shipped_criterion_raises_naming_both(tmp_path):
    layer = _user_layer(tmp_path)
    (layer / "SKILL-standards.md").write_text(_standards_md("""\
        - id: desc-160-char
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    msg = str(exc.value)
    assert str(SHIPPED_FILE) in msg and str(layer / "SKILL-standards.md") in msg


def test_authored_id_repeating_skill_type_tag_raises_naming_both(tmp_path):
    layer = _user_layer(tmp_path)
    (layer / "SKILL-standards.md").write_text(_standards_md("""\
        - id: skill-type-tag
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    msg = str(exc.value)
    assert str(SHIPPED_FILE) in msg and str(layer / "SKILL-standards.md") in msg


def test_duplicate_id_across_primitives_raises(tmp_path):
    layer = _user_layer(tmp_path)
    crit = """\
        - id: shared-opinion
          statement: Something.
          severity: info
          keywords: [a, b, c]
        """
    (layer / "SKILL-standards.md").write_text(_standards_md(crit), encoding="utf-8")
    (layer / "doc-standards.md").write_text(
        _standards_md(crit).replace("skill_md", "plain_md"), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError, match="shared-opinion"):
        sr.resolve(None)


def test_authored_id_equal_to_a_catalog_rule_raises(tmp_path):
    (_user_layer(tmp_path) / "SKILL-standards.md").write_text(_standards_md("""\
        - id: yaml-contract
          statement: Bypass attempt.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "architectural" in str(exc.value) and "rule_catalog" in str(exc.value)


def test_mechanical_criterion_without_an_evaluator_raises(tmp_path):
    (_user_layer(tmp_path) / "SKILL-standards.md").write_text(_standards_md("""\
        - id: no-emoji
          statement: A SKILL.md body carries no emoji.
          severity: fail
          keywords: [emoji, glyph, ascii]
          enforcement: mechanical
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    assert "no-emoji" in str(exc.value) and "no evaluator" in str(exc.value)


@pytest.mark.parametrize("mutate, needle", [
    (lambda c: c[0].pop("threshold"), "must declare"),
    (lambda c: c[0]["threshold"].update(name="desc_limit"), "evaluator reads"),
    (lambda c: c[0]["threshold"].update(default=0), "positive integer"),
    (lambda c: c[1].update(threshold={"name": "x_max", "default": 3}), "only a"),
])
def test_bad_threshold_declarations_raise(tmp_path, mutate, needle):
    d = _write_shipped(tmp_path, mutate)
    with pytest.raises(sr.StandardsConfigError, match=needle):
        sr.resolve(None, shipped_dir=d)


def test_threshold_declaration_colliding_with_a_builtin_raises(tmp_path, monkeypatch):
    evaluator = dataclasses.replace(MECHANICAL_EVALUATORS["desc-160-char"], threshold="name_max_chars")
    monkeypatch.setitem(MECHANICAL_EVALUATORS, "desc-160-char", evaluator)
    d = _write_shipped(tmp_path, lambda c: c[0]["threshold"].update(name="name_max_chars"))
    with pytest.raises(sr.StandardsConfigError, match="built-in threshold"):
        sr.resolve(None, shipped_dir=d)


def test_unknown_threshold_override_still_raises(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "thresholds:\n  bogus_max: 3\n", encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError, match="desc_max_chars"):
        sr.resolve(None)


# ---------------------------------------------------------------------------
# consumers
# ---------------------------------------------------------------------------


def _cli(tmp_path, *args):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "config")
    env["PYTHONPATH"] = str(REPO_ROOT / "plugins" / "bootstrap")
    env["_BOOTSTRAP_GUARD_SKIP_REEXEC"] = "1"
    proc = subprocess.run(
        [sys.executable, str(RESOLVE_CLI), "--project-root", str(tmp_path), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_resolver_cli_never_lists_the_shipped_file(tmp_path):
    assert _cli(tmp_path)["standards"] == {}
    assert _cli(tmp_path, "--primitive", "skill_md")["standards"] == {"skill_md": []}
    layer = _user_layer(tmp_path)
    (layer / "SKILL-standards.md").write_text(_standards_md("""\
        - id: mine
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    assert _cli(tmp_path)["standards"] == {"skill_md": [str(layer / "SKILL-standards.md")]}


def _load_script(name):
    path = PLUGIN / "skills" / "md-domain" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_m5_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_emit_audit_jobs_opts_out_of_the_shipped_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", tmp_path / "absent")
    emit = _load_script("emit_audit_jobs")
    assert emit.resolve_admitted_endpoints(tmp_path) == frozenset()


def test_evidence_pack_propagates_a_shipped_layer_error(tmp_path, monkeypatch):
    ep = _load_script("evidence_pack")
    path = _skill(tmp_path)
    assert any("description <= 160 chars" in line for line in ep._audit_verdicts(path))
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", tmp_path / "absent")
    with pytest.raises(sr.StandardsConfigError):
        ep._audit_verdicts(path)


def test_evidence_pack_still_degrades_without_pyyaml(tmp_path, monkeypatch):
    ep = _load_script("evidence_pack")
    monkeypatch.setattr(sr, "HAVE_YAML", False)
    lines = ep._audit_verdicts(_skill(tmp_path))
    assert lines == ["mechanical-contract-check unavailable: StandardsUnavailableError"]


# ---------------------------------------------------------------------------
# no pyyaml: deferred rows, still loud on a missing file
# ---------------------------------------------------------------------------


def _bare(tmp_path, code):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(PLUGIN)
    prelude = "import sys\nsys.modules['yaml'] = None\n"
    return subprocess.run([sys.executable, "-c", prelude + code],
                          capture_output=True, text=True, env=env, timeout=60)


def _bare_rows(tmp_path, path):
    proc = _bare(tmp_path, (
        "import json\nfrom skills_kit_lib.audit import audit\n"
        f"print(json.dumps(audit(__import__('pathlib').Path({str(path)!r}))['universal']))\n"
    ))
    assert proc.returncode == 0, proc.stderr
    return {r["rule"]: r for r in json.loads(proc.stdout)}


def test_without_pyyaml_the_rows_are_deferred(tmp_path):
    rows = _bare_rows(tmp_path, _skill(tmp_path))
    for rid in [*DESC_IDS, "skill-type-valid"]:
        assert rows[rid]["verdict"] == JUDGMENT
        assert "(deferred)" in rows[rid]["row"]
        assert "160" not in rows[rid]["row"]
    assert rows["skill-type-valid"]["row"] == "skill-type value valid (deferred)"
    assert "skill-type-tag" not in rows


def test_without_pyyaml_an_untagged_skill_gets_only_the_deferred_tag_row(tmp_path):
    rows = _bare_rows(tmp_path, _skill(tmp_path, skill_type=None))
    assert rows["skill-type-tag"]["verdict"] == JUDGMENT
    assert rows["skill-type-tag"]["row"] == "skill-type advisory tag (deferred)"
    assert "skill-type-valid" not in rows


def test_without_pyyaml_a_missing_shipped_file_still_exits_1(tmp_path):
    path = _skill(tmp_path)
    proc = _bare(tmp_path, (
        "from pathlib import Path\nfrom skills_kit_lib import standards_resolve as sr\n"
        f"sr.SHIPPED_STANDARDS_DIR = Path({str(tmp_path / 'absent')!r})\n"
        "from skills_kit_lib import audit\n"
        f"raise SystemExit(audit.main([{str(path)!r}]))\n"
    ))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "shipped standards file missing" in proc.stderr


# ---------------------------------------------------------------------------
# the CLAUDE.md criteria (M6 wave 2)
# ---------------------------------------------------------------------------


def test_claude_md_rows_keep_their_labels_notes_and_positions(tmp_path):
    """The three rows, exactly: labels, notes, verdicts, and which list holds
    them. The golden corpus pins the same rows end to end."""
    path = _claude_md(tmp_path, lines=0)
    report = audit(path)
    universal = [(r["rule"], r["row"], r["verdict"]) for r in report["universal"]]
    assert universal == [
        ("claude-md-links-resolve", "relative markdown links resolve", PASS),
        ("claude-md-fences-closed", "code fences closed", PASS),
        ("claude-md-import-size", "@imports <= 50 lines", PASS),
        ("claude-md-size-signal", "body size signal", PASS),
    ]
    assert _rows(report)["claude-md-size-signal"]["note"].startswith("lines=")
    floor = report["yaml_contract"][-1]
    assert floor == {
        "row": "yaml: >=1 record across insights/conventions (union floor)",
        "verdict": PASS,
        "note": "0 insight(s), 1 convention(s)",
        "rule": "claude-md-record-floor",
    }


@pytest.mark.parametrize("rid", CLAUDE_IDS)
def test_removing_a_claude_md_criterion_drops_only_its_row(tmp_path, monkeypatch, rid):
    """Red on revert: with the criterion hard-coded in audit_claude_md again,
    editing the shipped file changes nothing and this test fails."""
    path = _claude_md(tmp_path)
    baseline = audit(path)
    assert rid in _all_claude_rows(baseline)

    def drop(cs):
        cs[:] = [c for c in cs if c["id"] != rid]

    shipped = _write_shipped(tmp_path, drop, which=CLAUDE_SHIPPED_FILE)
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    expected = {key: [r for r in baseline[key] if r["rule"] != rid]
                for key in ("universal", "yaml_contract")}
    assert {key: report[key] for key in expected} == expected
    assert [r["rule"] for r in baseline["universal"] + baseline["yaml_contract"]].count(rid) == 1


def test_record_floor_has_no_row_without_a_claude_md_block(tmp_path):
    with_block = _all_claude_rows(audit(_claude_md(tmp_path, block=True)))
    assert "claude-md-record-floor" in with_block
    other = tmp_path / "other"
    other.mkdir()
    without = _all_claude_rows(audit(_claude_md(other, block=False)))
    assert "claude-md-record-floor" not in without
    # The other two criteria have a row either way.
    assert {"claude-md-import-size", "claude-md-size-signal"} <= set(without)


def test_empty_claude_md_block_fails_the_record_floor(tmp_path):
    empty = ("```yaml\nclaude_md:\n  _schema_version: \"1\"\n"
             "  scope:\n    directory: x\n    covers: [a]\n    excludes: [b]\n```\n")
    d = tmp_path / "e"
    d.mkdir()
    (d / "CLAUDE.md").write_text("# T\n\n" + empty, encoding="utf-8")
    row = _all_claude_rows(audit(d / "CLAUDE.md"))["claude-md-record-floor"]
    assert row["verdict"] == FAIL
    assert "neither insights nor conventions" in row["note"]


def test_import_threshold_is_owned_by_its_criterion_and_tunable_by_config(tmp_path):
    path = _claude_md(tmp_path)  # imp.md has 8 lines
    assert _rows(audit(path))["claude-md-import-size"]["verdict"] == PASS
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "thresholds:\n  import_max_lines: 5\n", encoding="utf-8")
    row = _rows(audit(path, sr.resolve(None)))["claude-md-import-size"]
    assert row["row"] == "@imports <= 5 lines"
    assert row["verdict"] == FAIL
    assert "imp.md has 8 lines" in row["note"]


def test_a_criterion_may_not_declare_a_builtin_threshold_the_evaluator_reads(tmp_path):
    """claude-md-size-signal READS body_max_lines; declaring it is still wrong."""
    def declare(cs):
        for c in cs:
            if c["id"] == "claude-md-size-signal":
                c["threshold"] = {"name": "body_max_lines", "default": 400}

    d = _write_shipped(tmp_path, declare, which=CLAUDE_SHIPPED_FILE)
    with pytest.raises(sr.StandardsConfigError, match="built-in threshold"):
        sr.resolve(None, shipped_dir=d)
    # The shipped file declares no threshold for it, and it still evaluates.
    assert "body_max_lines" in MECHANICAL_EVALUATORS["claude-md-size-signal"].reads_builtin_thresholds
    assert "body_max_lines" not in sr.resolve(None).threshold_defaults


def test_tuning_body_max_lines_moves_both_the_skill_and_claude_md_signal(tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill = _skill(skill_dir)
    cm = _claude_md(tmp_path, lines=3)
    quiet_skill = _rows(audit(skill))["body-size-signal"]
    quiet_cm = _rows(audit(cm))["claude-md-size-signal"]
    assert (quiet_skill["verdict"], quiet_cm["verdict"]) == ("n/a", PASS)

    (_user_layer(tmp_path) / "config.yaml").write_text(
        "thresholds:\n  body_max_lines: 2\n", encoding="utf-8")
    resolved = sr.resolve(None)
    assert _rows(audit(skill, resolved))["body-size-signal"]["verdict"] == JUDGMENT
    row = _rows(audit(cm, resolved))["claude-md-size-signal"]
    assert row["verdict"] == JUDGMENT
    assert "size-signal judgment" in row["note"]


def test_an_evaluator_reads_only_the_thresholds_its_record_names():
    size = MECHANICAL_EVALUATORS["claude-md-size-signal"]
    every = {**audit_mod.THRESHOLDS, "import_max_lines": 50, "desc_max_chars": 160}
    assert size.threshold_view(every) == {
        "body_max_lines": every["body_max_lines"], "body_max_tokens": every["body_max_tokens"]}
    assert MECHANICAL_EVALUATORS["claude-md-import-size"].threshold_view(every) == {"import_max_lines": 50}
    assert MECHANICAL_EVALUATORS["desc-directive-form"].threshold_view(every) == {}
    with pytest.raises(ValueError, match="not built-in thresholds"):
        dataclasses.replace(size, reads_builtin_thresholds=("import_max_lines",))


def test_rules_off_removes_each_claude_md_row(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  claude-md-import-size: off\n  claude-md-size-signal: off\n"
        "  claude-md-record-floor: off\n", encoding="utf-8")
    rows = _all_claude_rows(audit(_claude_md(tmp_path), sr.resolve(None)))
    assert not set(CLAUDE_IDS) & set(rows)
    assert "claude-md-links-resolve" in rows


def test_claude_md_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  claude-md-record-floor: off\nthresholds:\n  import_max_lines: 9\n",
        encoding="utf-8")
    r = sr.resolve(None, shipped_dir=None)
    assert r.disabled_rules == {"claude-md-record-floor"}
    assert r.thresholds == {"import_max_lines": 9}


def test_authored_id_repeating_a_claude_md_criterion_raises_naming_both(tmp_path):
    layer = _user_layer(tmp_path)
    (layer / "CLAUDE-md-standards.md").write_text(_standards_md("""\
        - id: claude-md-size-signal
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """).replace("skill_md", "claude_md"), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    msg = str(exc.value)
    assert str(CLAUDE_SHIPPED_FILE) in msg and str(layer / "CLAUDE-md-standards.md") in msg


def test_missing_claude_md_shipped_file_exits_1_for_claude_md(tmp_path, monkeypatch, capsys):
    d = tmp_path / "shipped"
    d.mkdir()
    shutil.copy(SHIPPED_FILE, d / SHIPPED_FILE.name)
    shutil.copy(LEGACY_SHIPPED_FILE, d / LEGACY_SHIPPED_FILE.name)
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", d)
    assert audit_mod.main([str(_claude_md(tmp_path))]) == 1
    assert "CLAUDE-md-standards.md" in capsys.readouterr().err


# no pyyaml: the CLAUDE.md rows are deferred like the SKILL rows


def _bare_claude_rows(tmp_path, path):
    proc = _bare(tmp_path, (
        "import json\nfrom skills_kit_lib.audit import audit\n"
        f"r = audit(__import__('pathlib').Path({str(path)!r}))\n"
        "print(json.dumps([*r['universal'], *r['yaml_contract']]))\n"
    ))
    assert proc.returncode == 0, proc.stderr
    return {r["rule"]: r for r in json.loads(proc.stdout)}


def test_without_pyyaml_the_claude_md_criteria_are_deferred_rows(tmp_path):
    rows = _bare_claude_rows(tmp_path, _claude_md(tmp_path))
    assert rows["claude-md-import-size"]["verdict"] == JUDGMENT
    assert rows["claude-md-import-size"]["row"] == "@imports <= import_max_lines lines (deferred)"
    assert rows["claude-md-size-signal"]["verdict"] == JUDGMENT
    assert rows["claude-md-size-signal"]["row"] == "body size signal (deferred)"
    assert "50" not in rows["claude-md-import-size"]["row"]
    # The block cannot be parsed without pyyaml, so the floor has no row to defer.
    assert "claude-md-record-floor" not in rows
    assert rows["claude-md-links-resolve"]["verdict"] == PASS


def test_without_pyyaml_a_missing_claude_md_shipped_file_exits_1(tmp_path):
    path = _claude_md(tmp_path)
    d = tmp_path / "shipped"
    d.mkdir()
    shutil.copy(SHIPPED_FILE, d / SHIPPED_FILE.name)
    shutil.copy(LEGACY_SHIPPED_FILE, d / LEGACY_SHIPPED_FILE.name)
    proc = _bare(tmp_path, (
        "from pathlib import Path\nfrom skills_kit_lib import standards_resolve as sr\n"
        f"sr.SHIPPED_STANDARDS_DIR = Path({str(d)!r})\n"
        "from skills_kit_lib import audit\n"
        f"raise SystemExit(audit.main([{str(path)!r}]))\n"
    ))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "CLAUDE-md-standards.md" in proc.stderr


# ---------------------------------------------------------------------------
# the body and references criteria (M6 wave 3)
# ---------------------------------------------------------------------------


def _body_skill(tmp_path, *, words=0, references=None):
    """A SKILL.md with `words` extra body words. references=None creates no
    references/ directory; a list creates one holding those relative files."""
    path = _skill(tmp_path)
    if words:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + " ".join(f"w{i}" for i in range(words)) + "\n")
    if references is not None:
        refs = path.parent / "references"
        refs.mkdir()
        for rel in references:
            target = refs / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# ref\n", encoding="utf-8")
    return path


def _tune(tmp_path, text):
    (_user_layer(tmp_path) / "config.yaml").write_text(text, encoding="utf-8")
    return sr.resolve(None)


def test_body_rows_keep_their_labels_notes_and_positions(tmp_path):
    """The four rows, exactly, between skill-type-valid and refs-cited-exist."""
    report = audit(_body_skill(tmp_path))
    rows = report["universal"]
    rules = [r["rule"] for r in rows]
    i = rules.index("skill-type-valid")
    assert rules[i + 1:i + 6] == [*BODY_IDS, "refs-cited-exist"]
    body = {r["rule"]: r for r in rows}
    lines = int(body["body-line-count"]["note"])
    tokens = int(body["body-token-count"]["note"])
    assert body["body-line-count"] == {
        "row": "SKILL.md line count", "verdict": PASS, "note": str(lines), "rule": "body-line-count"}
    assert body["body-token-count"] == {
        "row": "SKILL.md token count (approx)", "verdict": PASS, "note": str(tokens),
        "rule": "body-token-count"}
    assert body["body-size-signal"] == {
        "row": "progressive disclosure (conditional)", "verdict": "n/a",
        "note": f"lines={lines}, tokens~{tokens}", "rule": "body-size-signal"}
    assert body["refs-one-hop-deep"] == {
        "row": "references one-hop-deep (ADP)", "verdict": "n/a",
        "note": "no references/ directory", "rule": "refs-one-hop-deep"}


@pytest.mark.parametrize("rid", BODY_IDS)
def test_removing_a_body_criterion_from_the_shipped_file_drops_only_its_row(tmp_path, monkeypatch, rid):
    """Red on revert: with the rows hard-coded in check_universal again,
    editing the shipped file changes nothing and this test fails."""
    path = _body_skill(tmp_path)
    baseline = audit(path)
    assert [r["rule"] for r in baseline["universal"]].count(rid) == 1

    shipped = _write_shipped(tmp_path, lambda cs: cs.__setitem__(slice(None), [c for c in cs if c["id"] != rid]))
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    assert report["universal"] == [r for r in baseline["universal"] if r["rule"] != rid]


def test_refs_one_hop_deep_is_na_without_a_references_directory(tmp_path):
    row = _rows(audit(_body_skill(tmp_path)))["refs-one-hop-deep"]
    assert (row["verdict"], row["note"]) == ("n/a", "no references/ directory")


def test_refs_one_hop_deep_passes_flat_and_fails_an_undeclared_nested_file(tmp_path):
    flat = _rows(audit(_body_skill(tmp_path, references=["a.md"])))["refs-one-hop-deep"]
    assert (flat["verdict"], flat["note"]) == (PASS, "")

    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    row = _rows(audit(_body_skill(nested_dir, references=["sub/b.md"])))["refs-one-hop-deep"]
    assert row["verdict"] == FAIL
    assert row["row"] == "references one-hop-deep (ADP)"
    nested = str(Path("references") / "sub" / "b.md")
    assert row["note"] == (
        "nested and not declared in SKILL.md index.references[] (add the file's path "
        f"or its directory as an entry): [{nested!r}]")


def test_refs_one_hop_deep_below_fail_severity_reports_judgment(tmp_path, monkeypatch):
    def soften(cs):
        for c in cs:
            if c["id"] == "refs-one-hop-deep":
                c["severity"] = "info"
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, soften))
    other = tmp_path / "s"
    other.mkdir()
    row = _rows(audit(_body_skill(other, references=["sub/b.md"])))["refs-one-hop-deep"]
    assert row["verdict"] == JUDGMENT


def test_body_size_signal_is_na_pass_or_judgment(tmp_path):
    """n/a within the limits; pass when over them with a references/ directory;
    judgment-required (never fail) when over them with none."""
    small = _rows(audit(_body_skill(tmp_path)))["body-size-signal"]
    assert small["verdict"] == "n/a"

    big_refs = tmp_path / "big-refs"
    big_refs.mkdir()
    row = _rows(audit(_body_skill(big_refs, words=2600, references=[])))["body-size-signal"]
    assert row["verdict"] == PASS
    assert row["note"].startswith("body large (lines=") and row["note"].endswith("); references/ exists")

    big = tmp_path / "big"
    big.mkdir()
    row = _rows(audit(_body_skill(big, words=2600)))["body-size-signal"]
    assert row["verdict"] == JUDGMENT
    assert row["row"] == "progressive disclosure (conditional)"
    assert "and no references/; run the CRP test" in row["note"]
    assert row["note"].endswith("size is a signal, not a verdict (Dec-11)")


def test_tuning_the_body_limits_changes_body_size_signal(tmp_path):
    quiet_dir = tmp_path / "quiet"
    quiet_dir.mkdir()
    path = _body_skill(quiet_dir)
    assert _rows(audit(path))["body-size-signal"]["verdict"] == "n/a"
    resolved = _tune(tmp_path, "thresholds:\n  body_max_lines: 3\n")
    assert _rows(audit(path, resolved))["body-size-signal"]["verdict"] == JUDGMENT

    resolved = _tune(tmp_path, "thresholds:\n  body_max_tokens: 5\n")
    assert _rows(audit(path, resolved))["body-size-signal"]["verdict"] == JUDGMENT


def test_body_size_signal_severity_decides_fail_or_judgment(tmp_path, monkeypatch):
    def harden(cs):
        for c in cs:
            if c["id"] == "body-size-signal":
                c["severity"] = "fail"
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, harden))
    other = tmp_path / "s"
    other.mkdir()
    assert _rows(audit(_body_skill(other, words=2600)))["body-size-signal"]["verdict"] == FAIL


def test_count_rows_carry_the_counts_and_always_pass(tmp_path):
    for words in (0, 2600):
        d = tmp_path / f"w{words}"
        d.mkdir()
        rows = _rows(audit(_body_skill(d, words=words)))
        assert rows["body-line-count"]["verdict"] == PASS
        assert rows["body-token-count"]["verdict"] == PASS
        assert rows["body-line-count"]["note"].isdigit()
        assert rows["body-token-count"]["note"].isdigit()
    assert int(rows["body-token-count"]["note"]) > audit_mod.THRESHOLDS["body_max_tokens"]


def test_body_size_signal_reads_the_builtin_limits_and_owns_none():
    ev = MECHANICAL_EVALUATORS["body-size-signal"]
    assert ev.threshold is None
    assert ev.reads_builtin_thresholds == ("body_max_lines", "body_max_tokens")
    assert all(MECHANICAL_EVALUATORS[rid].threshold is None
               and not MECHANICAL_EVALUATORS[rid].reads_builtin_thresholds
               for rid in ("body-line-count", "body-token-count", "refs-one-hop-deep"))


def test_rules_off_removes_each_body_row(tmp_path):
    resolved = _tune(tmp_path, "rules:\n" + "".join(f"  {rid}: off\n" for rid in BODY_IDS))
    rows = _rows(audit(_body_skill(tmp_path), resolved))
    assert not set(BODY_IDS) & set(rows)
    assert {"skill-type-valid", "refs-cited-exist"} <= set(rows)


def test_body_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n  body-size-signal: off\n  refs-one-hop-deep: off\n"
        "thresholds:\n  body_max_lines: 9\n", encoding="utf-8")
    r = sr.resolve(None, shipped_dir=None)
    assert r.disabled_rules == {"body-size-signal", "refs-one-hop-deep"}
    assert r.thresholds == {"body_max_lines": 9}


def test_authored_id_repeating_a_body_criterion_raises_naming_both(tmp_path):
    layer = _user_layer(tmp_path)
    (layer / "SKILL-standards.md").write_text(_standards_md("""\
        - id: refs-one-hop-deep
          statement: Mine.
          severity: info
          keywords: [a, b, c]
        """), encoding="utf-8")
    with pytest.raises(sr.StandardsConfigError) as exc:
        sr.resolve(None)
    msg = str(exc.value)
    assert str(SHIPPED_FILE) in msg and str(layer / "SKILL-standards.md") in msg


def test_without_pyyaml_the_body_rows_are_deferred(tmp_path):
    rows = _bare_rows(tmp_path, _body_skill(tmp_path))
    labels = {
        "body-line-count": "SKILL.md line count (deferred)",
        "body-token-count": "SKILL.md token count (approx) (deferred)",
        "body-size-signal": "progressive disclosure (conditional) (deferred)",
        "refs-one-hop-deep": "references one-hop-deep (ADP) (deferred)",
    }
    for rid, label in labels.items():
        assert rows[rid]["row"] == label
        assert rows[rid]["verdict"] == JUDGMENT
        assert "pyyaml unavailable" in rows[rid]["note"]
    assert "refs-cited-exist" in rows


# ---------------------------------------------------------------------------
# the YAML-contract floor criteria (M6 wave 4)
# ---------------------------------------------------------------------------

REF_HEAD = "reference_skill:\n  identity: i\n  scope:\n    covers: [a]\n    excludes: [b]\n"
TECH_HEAD = ("technique_skill:\n  identity: i\n  scope:\n    covers: [a]\n    excludes: [b]\n"
             "  steps:\n    - id: s1\n      action: do\n")
FACT = "    - id: f1\n      summary: s\n      keywords: [a, b, c]\n      detail: d\n"
FACT2 = FACT.replace("f1", "f2")
GOTCHAS = "      gotchas:\n        - g\n"
EXAMPLE = "      example:\n        input: i\n        output: o\n"
ANTI = "  anti_patterns:\n    - id: a1\n      what: w\n"
TECHNIQUE_GOTCHA = "  techniques:\n    - id: t1\n      gotchas:\n        - g\n"

FACTS_LABEL = "yaml: facts present (cross-source)"
GOTCHA_LABEL = "yaml: >=1 fact carries gotchas (cross-source)"
EXAMPLE_LABEL = "yaml: >=1 fact carries example (cross-source)"
CAUTION_LABEL = "yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)"
FLOOR_NOTE = ("reference-skill requires at least one fact (nested in reference_skill: "
              "or as a top-level facts: unit, or both)")
GOTCHA_NOTE = "no fact carries a gotchas list across any source"
EXAMPLE_NOTE = "no fact carries an example block across any source"
CAUTION_NOTE = "no technique carries a gotchas list and no anti_patterns record exists"


def _fence(text):
    return "```yaml\n" + text + "```\n"


def _contract_skill(tmp_path, body, skill_type="reference-skill", name="contract"):
    """A SKILL.md carrying `body` after the H1; skill_type=None omits the tag."""
    d = tmp_path / name
    d.mkdir()
    p = d / "SKILL.md"
    type_line = "" if skill_type is None else f"skill-type: {skill_type}\n"
    p.write_text(
        "---\nname: contract\ndescription: Use when doing X. Do NOT use for Y.\n"
        f"{type_line}---\n# Contract\n\n{body}",
        encoding="utf-8",
    )
    return p


def _floor_rows(report):
    """The four floor rows of a report as (rule, label, verdict, note), in order."""
    return [(r["rule"], r["row"], r["verdict"], r["note"])
            for r in report["yaml_contract"] if r["rule"] in CONTRACT_IDS]


def _facts_pass(n):
    return ("facts-floor", FACTS_LABEL, PASS, f"{n} facts across all sources")


FLOOR_FAIL = ("facts-floor", FACTS_LABEL, FAIL, FLOOR_NOTE)
GOTCHA_PASS = ("facts-gotcha", GOTCHA_LABEL, PASS, "")
GOTCHA_FAIL = ("facts-gotcha", GOTCHA_LABEL, FAIL, GOTCHA_NOTE)
EXAMPLE_PASS = ("facts-example", EXAMPLE_LABEL, PASS, "")
EXAMPLE_FAIL = ("facts-example", EXAMPLE_LABEL, FAIL, EXAMPLE_NOTE)
CAUTION_PASS = ("caution-floor", CAUTION_LABEL, PASS, "")
CAUTION_FAIL = ("caution-floor", CAUTION_LABEL, FAIL, CAUTION_NOTE)

#: name -> (skill-type tag, body, the exact floor rows an audit emits). Each
#: expectation was recorded from the hard-coded checks before they moved.
FLOOR_CASES = {
    "reference_no_facts": (
        "reference-skill", _fence(REF_HEAD), [FLOOR_FAIL]),
    "reference_facts_without_gotchas_or_examples": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n" + FACT),
        [_facts_pass(1), GOTCHA_FAIL, EXAMPLE_FAIL]),
    "reference_gotcha_only": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n" + FACT + GOTCHAS),
        [_facts_pass(1), GOTCHA_PASS, EXAMPLE_FAIL]),
    "reference_example_only": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n" + FACT + EXAMPLE),
        [_facts_pass(1), GOTCHA_FAIL, EXAMPLE_PASS]),
    "reference_gotcha_and_example_on_different_facts": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n" + FACT + GOTCHAS + FACT2 + EXAMPLE),
        [_facts_pass(2), GOTCHA_PASS, EXAMPLE_PASS]),
    "reference_nested_and_top_level_facts_are_one_union": (
        "reference-skill",
        _fence(REF_HEAD + "  facts:\n" + FACT) + _fence("facts:\n" + FACT2 + GOTCHAS),
        [_facts_pass(2), GOTCHA_PASS, EXAMPLE_FAIL]),
    "top_level_facts_unit_under_the_tag_gate": (
        "reference-skill", _fence("facts:\n" + FACT),
        [_facts_pass(1), GOTCHA_FAIL, EXAMPLE_FAIL]),
    "facts_that_are_not_mappings_do_not_count": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n    - just-a-string\n"), [FLOOR_FAIL]),
    "unparsable_reference_block_still_gets_the_floor": (
        "reference-skill", _fence("reference_skill:\n  identity: [unclosed\n"), [FLOOR_FAIL]),
    "reference_root_without_a_tag": (None, _fence(REF_HEAD), [FLOOR_FAIL]),
    "reference_root_under_a_disagreeing_tag": ("technique-skill", _fence(REF_HEAD), [FLOOR_FAIL]),
    "tag_only_no_contract_block": ("reference-skill", "prose only\n", [FLOOR_FAIL]),
    "technique_gotcha_satisfies_the_floor": (
        "technique-skill", _fence(TECH_HEAD + TECHNIQUE_GOTCHA), [CAUTION_PASS]),
    "technique_anti_pattern_satisfies_the_floor": (
        "technique-skill", _fence(TECH_HEAD + ANTI), [CAUTION_PASS]),
    "technique_with_neither_fails": (
        "technique-skill", _fence(TECH_HEAD), [CAUTION_FAIL]),
    "technique_empty_anti_patterns_fails": (
        "technique-skill", _fence(TECH_HEAD + "  anti_patterns: []\n"), [CAUTION_FAIL]),
    "technique_untyped_gated_on_the_root": (None, _fence(TECH_HEAD), [CAUTION_FAIL]),
    "first_technique_unit_decides": (
        "technique-skill", _fence(TECH_HEAD) + _fence(TECH_HEAD + ANTI), [CAUTION_FAIL]),
    "mapping_technique_unit_after_a_non_mapping_one": (
        "technique-skill", _fence("technique_skill: nope\n") + _fence(TECH_HEAD + ANTI),
        [CAUTION_PASS]),
    "reference_and_technique_blocks_get_both_in_file_order": (
        "reference-skill", _fence(REF_HEAD + "  facts:\n" + FACT) + _fence(TECH_HEAD),
        [_facts_pass(1), GOTCHA_FAIL, EXAMPLE_FAIL, CAUTION_FAIL]),
}

#: documents that must produce no floor row at all: name -> (tag, body)
NO_ROW_CASES = {
    "untyped_no_contract": (None, "prose only\n"),
    "technique_tag_no_contract": ("technique-skill", "prose only\n"),
    "pattern_contract": ("pattern-skill", _fence(
        "pattern_skill:\n  identity: i\n  scope:\n    covers: [a]\n    excludes: [b]\n")),
    "facts_unit_in_a_domain_skill": ("domain-skill", _fence("facts:\n" + FACT)),
    "technique_root_that_is_not_a_mapping": ("technique-skill", _fence("technique_skill: nope\n")),
    "unparsable_technique_block": ("technique-skill", _fence("technique_skill:\n  identity: [unclosed\n")),
    "unrecognised_root_under_a_non_reference_tag": ("pattern-skill", _fence("bogus_root:\n  a: 1\n")),
}


@pytest.mark.parametrize("case", FLOOR_CASES)
def test_floor_rows_keep_their_labels_notes_verdicts_and_order(tmp_path, case):
    skill_type, body, expected = FLOOR_CASES[case]
    assert _floor_rows(audit(_contract_skill(tmp_path, body, skill_type))) == expected


@pytest.mark.parametrize("case", NO_ROW_CASES)
def test_no_floor_row_where_none_applies(tmp_path, case):
    skill_type, body = NO_ROW_CASES[case]
    report = audit(_contract_skill(tmp_path, body, skill_type))
    assert _floor_rows(report) == []
    assert not set(CONTRACT_IDS) & {r["rule"] for r in report["universal"]}


def test_a_document_with_no_frontmatter_still_gets_the_floor(tmp_path):
    d = tmp_path / "bare"
    d.mkdir()
    (d / "SKILL.md").write_text("# T\n\n" + _fence(REF_HEAD), encoding="utf-8")
    assert _floor_rows(audit(d / "SKILL.md")) == [FLOOR_FAIL]


def test_gotcha_and_example_are_absent_until_a_fact_exists(tmp_path):
    empty = _floor_rows(audit(_contract_skill(tmp_path, _fence(REF_HEAD))))
    assert [row[0] for row in empty] == ["facts-floor"]
    other = tmp_path / "other"
    other.mkdir()
    one = _floor_rows(audit(_contract_skill(other, _fence(REF_HEAD + "  facts:\n" + FACT))))
    assert [row[0] for row in one] == ["facts-floor", "facts-gotcha", "facts-example"]


def _all_four_body():
    """A document that gets every floor row: a reference block with one fact
    plus a technique block with no caution."""
    return _fence(REF_HEAD + "  facts:\n" + FACT) + _fence(TECH_HEAD)


@pytest.mark.parametrize("rid", CONTRACT_IDS)
def test_removing_a_floor_criterion_from_the_shipped_file_drops_only_its_row(tmp_path, monkeypatch, rid):
    """Red on revert: with the floors hard-coded in audit() again, editing the
    shipped file changes nothing and this test fails."""
    path = _contract_skill(tmp_path, _all_four_body())
    baseline = audit(path)
    assert [row[0] for row in _floor_rows(baseline)] == CONTRACT_IDS

    shipped = _write_shipped(
        tmp_path, lambda cs: cs.__setitem__(slice(None), [c for c in cs if c["id"] != rid]))
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", shipped)
    report = audit(path)

    expected = {key: [r for r in baseline[key] if r["rule"] != rid]
                for key in ("universal", "yaml_contract", "type_specific")}
    assert {key: report[key] for key in expected} == expected
    assert report["mixed_type"] == baseline["mixed_type"]


def test_floor_rows_sit_between_the_portable_unit_rows_and_the_asset_rows(tmp_path):
    asset = _fence("asset_dependencies:\n  - path: SKILL.md\n    kind: file\n    purpose: p\n")
    body = _fence(REF_HEAD + "  facts:\n" + FACT) + _fence("facts:\n" + FACT2) + _fence(TECH_HEAD) + asset
    rows = audit(_contract_skill(tmp_path, body))["yaml_contract"]
    rules = [r["rule"] for r in rows]
    first = {rid: rules.index(rid) for rid in [*CONTRACT_IDS, "asset-paths-resolve"]}
    assert list(first) == sorted(first, key=first.get)
    portable = [i for i, r in enumerate(rows) if r["row"].startswith("yaml: portable unit")]
    assert portable and max(portable) < first["facts-floor"]


def test_floor_rows_are_not_in_the_universal_list(tmp_path):
    report = audit(_contract_skill(tmp_path, _all_four_body()))
    assert not set(CONTRACT_IDS) & {r["rule"] for r in report["universal"]}
    assert [row[0] for row in _floor_rows(report)] == CONTRACT_IDS


def test_floor_criteria_below_fail_severity_report_judgment(tmp_path, monkeypatch):
    def soften(cs):
        for c in cs:
            if c["id"] in CONTRACT_IDS:
                c["severity"] = "judgment"
    monkeypatch.setattr(sr, "SHIPPED_STANDARDS_DIR", _write_shipped(tmp_path, soften))
    other = tmp_path / "s"
    other.mkdir()
    rows = _floor_rows(audit(_contract_skill(other, _all_four_body())))
    assert [(rid, verdict) for rid, _label, verdict, _note in rows] == [
        ("facts-floor", PASS), ("facts-gotcha", JUDGMENT), ("facts-example", JUDGMENT),
        ("caution-floor", JUDGMENT)]


def test_floor_evaluators_are_yaml_contract_rows_that_own_no_threshold():
    for rid in CONTRACT_IDS:
        ev = MECHANICAL_EVALUATORS[rid]
        assert (ev.applies_to, ev.section, ev.threshold, ev.reads_builtin_thresholds) == (
            "skill_md", "yaml_contract", None, ())
    assert all(MECHANICAL_EVALUATORS[rid].section == "universal"
               for rid in [*SHIPPED_IDS, *BODY_IDS])
    criteria = _shipped_block()["standards_set"]["criteria"]
    assert all("threshold" not in c for c in criteria if c["id"] in CONTRACT_IDS)
    assert sr.resolve(None).threshold_defaults == {"desc_max_chars": 160, "import_max_lines": 50}


def test_rules_off_removes_each_floor_row(tmp_path):
    resolved = _tune(tmp_path, "rules:\n" + "".join(f"  {rid}: off\n" for rid in CONTRACT_IDS))
    report = audit(_contract_skill(tmp_path, _all_four_body()), resolved)
    assert _floor_rows(report) == []
    assert any(r["rule"] == "yaml-contract" for r in report["yaml_contract"])


def test_rules_off_removes_one_floor_row_and_keeps_the_others(tmp_path):
    resolved = _tune(tmp_path, "rules:\n  facts-gotcha: off\n")
    rows = _floor_rows(audit(_contract_skill(tmp_path, _all_four_body()), resolved))
    assert [row[0] for row in rows] == ["facts-floor", "facts-example", "caution-floor"]


def test_opting_out_of_the_shipped_layer_removes_every_floor_row(tmp_path):
    resolved = sr.resolve(None, shipped_dir=None)
    report = audit(_contract_skill(tmp_path, _all_four_body()), resolved)
    assert _floor_rows(report) == []


def test_floor_knobs_stay_valid_when_the_shipped_layer_is_opted_out(tmp_path):
    (_user_layer(tmp_path) / "config.yaml").write_text(
        "rules:\n" + "".join(f"  {rid}: off\n" for rid in CONTRACT_IDS), encoding="utf-8")
    r = sr.resolve(None, shipped_dir=None)
    assert r.disabled_rules == set(CONTRACT_IDS)


@pytest.mark.parametrize("rid", CONTRACT_IDS)
def test_authored_id_repeating_a_floor_criterion_raises_naming_both(tmp_path, rid):
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
    assert rid in msg and str(SHIPPED_FILE) in msg and str(layer / "SKILL-standards.md") in msg


def _bare_report(tmp_path, path):
    proc = _bare(tmp_path, (
        "import json\nfrom skills_kit_lib.audit import audit\n"
        f"print(json.dumps(audit(__import__('pathlib').Path({str(path)!r}))))\n"
    ))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("case", [
    "reference_facts_without_gotchas_or_examples", "reference_no_facts",
    "technique_with_neither_fails", "tag_only_no_contract_block"])
def test_without_pyyaml_the_floors_have_no_row_at_all(tmp_path, case):
    """Without pyyaml the block cannot be parsed, so the floors never had a row
    and are not deferred either: the contract-staged state reports only the
    staged-contract row."""
    skill_type, body, _expected = FLOOR_CASES[case]
    report = _bare_report(tmp_path, _contract_skill(tmp_path, body, skill_type))
    every = [r["rule"] for key in ("universal", "yaml_contract", "type_specific") for r in report[key]]
    assert not set(CONTRACT_IDS) & set(every)
    if case != "tag_only_no_contract_block":
        assert [r["rule"] for r in report["yaml_contract"]] == ["yaml-contract"]
        assert report["yaml_contract"][0]["verdict"] == JUDGMENT
