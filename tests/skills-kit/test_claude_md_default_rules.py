"""Default rules for CLAUDE.md / AGENTS.md: universal rows that run with or
without a claude_md: block (claude-md-links-resolve, claude-md-fences-closed,
claude-md-import-size, claude-md-size-signal), resolved-config handling, and
the evidence pack's use of the same audit."""

import importlib.util
from pathlib import Path

import pytest

from skills_kit_lib.audit import FAIL, JUDGMENT, NA, PASS, THRESHOLDS, audit
from skills_kit_lib.standards_resolve import (
    ResolvedStandards, StandardsConfigError, _validate_and_extract,
)


def _by_rule(report):
    return {r["rule"]: r for r in report["universal"]}


def _write(tmp_path, text, name="CLAUDE.md"):
    md = tmp_path / name
    md.write_text(text)
    return md


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_clean_file_passes_all_defaults(tmp_path, name):
    (tmp_path / "other.md").write_text("x\n")
    md = _write(tmp_path, "# T\n\nSee [other](other.md).\n", name)
    rows = _by_rule(audit(md))
    assert {r["verdict"] for r in rows.values()} == {PASS}
    assert set(rows) == {
        "claude-md-links-resolve", "claude-md-fences-closed",
        "claude-md-import-size", "claude-md-size-signal",
    }


def test_broken_relative_link_fails_without_any_block(tmp_path):
    md = _write(tmp_path, "# T\n\nSee [gone](missing/file.md).\n")
    report = audit(md)
    assert _by_rule(report)["claude-md-links-resolve"]["verdict"] == FAIL
    assert "missing/file.md" in _by_rule(report)["claude-md-links-resolve"]["note"]
    assert [r["verdict"] for r in report["yaml_contract"]] == [NA]


def test_link_in_code_fence_or_inline_code_ignored(tmp_path):
    md = _write(tmp_path, "# T\n\n```\n[x](nope.md)\n```\n\nInline `[y](nope2.md)`.\n")
    assert _by_rule(audit(md))["claude-md-links-resolve"]["verdict"] == PASS


@pytest.mark.parametrize("target", [
    "https://example.com/x", "mailto:a@b.c", "#section", "/abs/path.md",
    "other.md#frag", "other.md?x=1",
])
def test_url_fragment_absolute_skipped_or_resolved(tmp_path, target):
    (tmp_path / "other.md").write_text("x\n")
    md = _write(tmp_path, f"[l]({target})\n")
    assert _by_rule(audit(md))["claude-md-links-resolve"]["verdict"] == PASS


def test_fragment_on_missing_file_fails(tmp_path):
    md = _write(tmp_path, "[l](gone.md#frag)\n")
    assert _by_rule(audit(md))["claude-md-links-resolve"]["verdict"] == FAIL


def test_unclosed_fence_fails(tmp_path):
    md = _write(tmp_path, "# T\n\n```python\nx = 1\n")
    assert _by_rule(audit(md))["claude-md-fences-closed"]["verdict"] == FAIL


def test_tilde_and_longer_fences_close(tmp_path):
    md = _write(tmp_path, "~~~\nx\n~~~\n\n````\n```\ninner\n```\n````\n")
    assert _by_rule(audit(md))["claude-md-fences-closed"]["verdict"] == PASS


def test_unclosed_outer_fence_with_inner_shorter_fence_fails(tmp_path):
    md = _write(tmp_path, "````\n```\ninner\n```\n")
    assert _by_rule(audit(md))["claude-md-fences-closed"]["verdict"] == FAIL


def test_import_over_limit_fails(tmp_path):
    (tmp_path / "big.md").write_text("line\n" * (THRESHOLDS["import_max_lines"] + 1))
    (tmp_path / "small.md").write_text("line\n" * 3)
    md = _write(tmp_path, "@big.md\n@small.md\n")
    rows = _by_rule(audit(md))
    assert rows["claude-md-import-size"]["verdict"] == FAIL
    assert "big.md" in rows["claude-md-import-size"]["note"]
    assert "small.md" not in rows["claude-md-import-size"]["note"]


def test_import_at_limit_passes(tmp_path):
    (tmp_path / "ok.md").write_text("line\n" * THRESHOLDS["import_max_lines"])
    md = _write(tmp_path, "@ok.md\n")
    assert _by_rule(audit(md))["claude-md-import-size"]["verdict"] == PASS


def test_missing_import_reported_under_links_resolve(tmp_path):
    md = _write(tmp_path, "@missing-import.md\n")
    rows = _by_rule(audit(md))
    assert rows["claude-md-links-resolve"]["verdict"] == FAIL
    assert "missing-import.md" in rows["claude-md-links-resolve"]["note"]
    assert rows["claude-md-import-size"]["verdict"] == PASS


def test_size_over_threshold_is_judgment_not_fail(tmp_path):
    md = _write(tmp_path, "word\n" * (THRESHOLDS["body_max_lines"] + 1))
    assert _by_rule(audit(md))["claude-md-size-signal"]["verdict"] == JUDGMENT


def test_resolved_threshold_applies_to_claude_md(tmp_path):
    md = _write(tmp_path, "word\n" * 10)
    resolved = ResolvedStandards(disabled_rules=set(), thresholds={"body_max_lines": 5})
    assert _by_rule(audit(md, resolved))["claude-md-size-signal"]["verdict"] == JUDGMENT
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "big.md").write_text("l\n" * 6)
    md2 = _write(sub, "@big.md\n", "AGENTS.md")
    resolved = ResolvedStandards(disabled_rules=set(), thresholds={"import_max_lines": 5})
    assert _by_rule(audit(md2, resolved))["claude-md-import-size"]["verdict"] == FAIL


def test_disabling_optional_rule_removes_its_row(tmp_path):
    md = _write(tmp_path, "word\n")
    resolved = ResolvedStandards(
        disabled_rules={"claude-md-import-size", "claude-md-size-signal"}, thresholds={})
    rules = set(_by_rule(audit(md, resolved)))
    assert "claude-md-import-size" not in rules
    assert "claude-md-size-signal" not in rules
    assert "claude-md-links-resolve" in rules


@pytest.mark.parametrize("rid", ["claude-md-links-resolve", "claude-md-fences-closed"])
def test_inoffensive_rules_cannot_be_disabled(rid):
    with pytest.raises(StandardsConfigError, match="inoffensive"):
        _validate_and_extract({"rules": {rid: "off"}})


@pytest.mark.parametrize("rid", ["claude-md-import-size", "claude-md-size-signal"])
def test_optional_default_rules_can_be_disabled(rid):
    disabled, _ = _validate_and_extract({"rules": {rid: "off"}})
    assert disabled == {rid}


def test_import_max_lines_is_a_valid_threshold_key():
    _, th = _validate_and_extract({"thresholds": {"import_max_lines": 7}})
    assert th == {"import_max_lines": 7}


# -- evidence pack -----------------------------------------------------------

_EP_PATH = (Path(__file__).resolve().parents[2] / "plugins" / "skills-kit"
            / "skills" / "md-domain" / "scripts" / "evidence_pack.py")
_spec = importlib.util.spec_from_file_location("md_evidence_pack_defaults", _EP_PATH)
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)


def test_evidence_verdicts_keep_judgment_rows(tmp_path):
    md = _write(tmp_path, "word\n" * (THRESHOLDS["body_max_lines"] + 1))
    lines = ep._audit_verdicts(md)
    assert any(l.startswith("[judgment-required]") for l in lines)


def test_evidence_verdicts_put_fail_before_cap(tmp_path):
    # 25 PASS/NA-ish rows would precede the FAIL in source order; FAIL must lead.
    md = _write(tmp_path, "[gone](missing.md)\n")
    lines = ep._audit_verdicts(md)
    assert lines[0].startswith("[fail]")


def test_evidence_verdicts_survive_cap_with_many_rows(tmp_path, monkeypatch):
    rows = [f"  [pass] row {i}" for i in range(30)] + ["  [fail] late failure"]
    monkeypatch.setattr("skills_kit_lib.audit.render_text", lambda report: "\n".join(rows))
    md = _write(tmp_path, "x\n")
    lines = ep._audit_verdicts(md)
    assert len(lines) == 20
    assert lines[0] == "[fail] late failure"


def test_evidence_verdicts_apply_resolved_config(tmp_path):
    (tmp_path / ".git").mkdir()
    cfg = tmp_path / ".claude" / "skills-kit"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("rules:\n  claude-md-size-signal: off\n")
    md = _write(tmp_path, "word\n" * (THRESHOLDS["body_max_lines"] + 1))
    assert not any("size signal" in l for l in ep._audit_verdicts(md))
    (cfg / "config.yaml").write_text("rules: {}\n")
    assert any("size signal" in l for l in ep._audit_verdicts(md))
