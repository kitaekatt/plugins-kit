"""Tests for check_project_doc_audit.py -- the project-doc audit acceptance contract.

The point of the script is that the valid criterion and taxonomy ids are PARSED
from the standards document rather than restated in code, so the tests that
matter are the ones that would have caught the drift in the task-local
prototype it replaces: ids the document carries but a frozen copy omitted
(K_unclassified, N_user_standard_violation), and buckets beyond FIX/IMPROVE.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
          / "scripts" / "check_project_doc_audit.py")
STANDARDS = (REPO_ROOT / "plugins" / "skills-kit" / "skills" / "md-domain"
             / "references" / "standards" / "project-doc-standards.md")

_spec = importlib.util.spec_from_file_location("check_project_doc_audit", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


@pytest.fixture(scope="module")
def contract():
    return mod.load_contract(STANDARDS)


def _report(**overrides):
    finding = {
        "criterion": "adp_one_hop_deep",
        "taxonomy": "F_chained_reference",
        "bucket": "IMPROVE",
        "line": 3,
        "remediation": "Inline the target.",
    }
    finding.update(overrides.pop("finding", {}))
    report = {"subject": "doc.md", "verdict": "FAIL", "findings": [finding]}
    report.update(overrides)
    return report


def _check(report, contract, subject_lines=10):
    criteria, taxonomy = contract
    mod.check_report(report, "doc.md", subject_lines, criteria, taxonomy)


# --- the contract is read from the document, not frozen in code ---------------

def test_ids_come_from_the_standards_document(contract):
    criteria, taxonomy = contract
    assert "adp_one_hop_deep" in criteria
    assert "ancestor_convention_conformance" in criteria
    assert taxonomy["F_chained_reference"] == "IMPROVE"
    assert taxonomy["N_broken_link_identified_target"] == "FIX"


@pytest.mark.parametrize("taxonomy_id,bucket", [
    ("K_unclassified", "SPECIAL"),
    ("N_user_standard_violation", "SERIOUS"),
])
def test_ids_the_frozen_prototype_omitted_are_accepted(contract, taxonomy_id, bucket):
    """The regression that motivated this script: a valid finding was rejected."""
    _check(_report(finding={"taxonomy": taxonomy_id, "bucket": bucket}), contract)


def test_every_documented_taxonomy_id_is_usable(contract):
    """No documented id is unreachable -- the drift check, generalized."""
    _criteria, taxonomy = contract
    for taxonomy_id, bucket in taxonomy.items():
        _check(_report(finding={"taxonomy": taxonomy_id, "bucket": bucket}), contract)


def test_missing_id_section_is_refused(tmp_path):
    doc = tmp_path / "s.md"
    doc.write_text("# Standards\n\nNo id tables here.\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.load_contract(doc)


def test_absent_standards_document_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        mod.load_contract(tmp_path / "nope.md")


# --- grounding rejections -----------------------------------------------------

def test_accepts_a_grounded_finding(contract):
    _check(_report(), contract)


def test_accepts_pass_with_no_findings(contract):
    _check({"subject": "doc.md", "verdict": "PASS", "findings": []}, contract)


def test_rejects_unknown_criterion(contract):
    with pytest.raises(SystemExit):
        _check(_report(finding={"criterion": "invented_criterion"}), contract)


def test_rejects_unknown_taxonomy(contract):
    with pytest.raises(SystemExit):
        _check(_report(finding={"taxonomy": "Z_invented"}), contract)


def test_rejects_bucket_that_contradicts_the_document(contract):
    """F_chained_reference is IMPROVE; claiming FIX is a mis-citation."""
    with pytest.raises(SystemExit):
        _check(_report(finding={"bucket": "FIX"}), contract)


def test_rejects_line_past_end_of_file(contract):
    with pytest.raises(SystemExit):
        _check(_report(finding={"line": 900}), contract, subject_lines=10)


def test_rejects_line_zero(contract):
    with pytest.raises(SystemExit):
        _check(_report(finding={"line": 0}), contract)


def test_rejects_boolean_line(contract):
    """bool is an int subclass -- True must not pass as line 1."""
    with pytest.raises(SystemExit):
        _check(_report(finding={"line": True}), contract)


def test_rejects_empty_remediation(contract):
    with pytest.raises(SystemExit):
        _check(_report(finding={"remediation": "   "}), contract)


def test_rejects_wrong_subject(contract):
    with pytest.raises(SystemExit):
        _check(_report(subject="other.md"), contract)


def test_rejects_pass_with_findings(contract):
    with pytest.raises(SystemExit):
        _check(_report(verdict="PASS"), contract)


def test_rejects_fail_with_no_findings(contract):
    with pytest.raises(SystemExit):
        _check({"subject": "doc.md", "verdict": "FAIL", "findings": []}, contract)


def test_rejects_extra_finding_key(contract):
    bad = _report()
    bad["findings"][0]["confidence"] = 0.9
    with pytest.raises(SystemExit):
        _check(bad, contract)


# --- completion-text handling -------------------------------------------------

def test_extracts_json_from_a_fenced_block():
    payload = {"subject": "doc.md", "verdict": "PASS", "findings": []}
    text = f"Here you go:\n\n```json\n{json.dumps(payload)}\n```\n"
    assert mod.extract_json(text) == payload


def test_rejects_non_json_completion():
    with pytest.raises(SystemExit):
        mod.extract_json("I could not complete the audit.")


# --- main(): argv, stdin, and the report-writing side effect --------------------

def _run_main(monkeypatch, tmp_path, argv, stdin_text, report_dir):
    """Drive main() end to end; return (exit_code_or_SystemExit, report_dir)."""
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setenv("JOB_KIT_REPORT_DIR", str(report_dir))
    monkeypatch.setenv("JOB_KIT_JOB_ID", "j1")
    monkeypatch.setenv("JOB_KIT_RUN_ID", "r1")
    monkeypatch.setenv("JOB_KIT_ATTEMPT_NO", "2")
    return mod.main(argv)


@pytest.fixture
def subject(tmp_path):
    """A 10-line subject file inside a repo root."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "d.md").write_text("\n".join(f"line {i}" for i in range(1, 11)),
                                        encoding="utf-8")
    return root


def test_main_accepts_and_writes_the_report(monkeypatch, tmp_path, subject):
    out = tmp_path / "reports"
    payload = {"subject": "docs/d.md", "verdict": "PASS", "findings": []}
    rc = _run_main(monkeypatch, tmp_path, ["docs/d.md", str(subject)],
                   json.dumps(payload), out)
    assert rc == 0
    written = out / "j1.r1.2.json"
    assert written.is_file()
    assert json.loads(written.read_text()) == payload


def test_main_rejects_and_writes_no_report(monkeypatch, tmp_path, subject):
    """Load-bearing: a rejected attempt must leave no artifact behind."""
    out = tmp_path / "reports"
    payload = {"subject": "docs/d.md", "verdict": "FAIL", "findings": [{
        "criterion": "invented", "taxonomy": "F_chained_reference",
        "bucket": "IMPROVE", "line": 3, "remediation": "x"}]}
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["docs/d.md", str(subject)],
                  json.dumps(payload), out)
    assert not out.exists()


def test_main_honours_the_standards_flag(monkeypatch, tmp_path, subject):
    """--standards is removed from argv correctly and actually governs the id set."""
    doc = tmp_path / "custom.md"
    doc.write_text(
        "### Criteria ids\n\n| id | sev |\n|---|---|\n| `only_criterion` | FAIL |\n\n"
        "### Taxonomy ids\n\n| id | bucket |\n|---|---|\n| `Z_only` | FIX |\n",
        encoding="utf-8")
    out = tmp_path / "reports"
    payload = {"subject": "docs/d.md", "verdict": "FAIL", "findings": [{
        "criterion": "only_criterion", "taxonomy": "Z_only",
        "bucket": "FIX", "line": 1, "remediation": "x"}]}
    rc = _run_main(monkeypatch, tmp_path,
                   ["docs/d.md", str(subject), "--standards", str(doc)],
                   json.dumps(payload), out)
    assert rc == 0


def test_main_standards_flag_first_is_removed_correctly(monkeypatch, tmp_path, subject):
    """The del-slice must not eat a positional when the flag leads."""
    out = tmp_path / "reports"
    payload = {"subject": "docs/d.md", "verdict": "PASS", "findings": []}
    rc = _run_main(monkeypatch, tmp_path,
                   ["--standards", str(STANDARDS), "docs/d.md", str(subject)],
                   json.dumps(payload), out)
    assert rc == 0


def test_main_standards_flag_without_a_value_is_refused(monkeypatch, tmp_path, subject):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["docs/d.md", str(subject), "--standards"],
                  "{}", tmp_path / "reports")


def test_main_wrong_arity_is_refused(monkeypatch, tmp_path, subject):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["docs/d.md"], "{}", tmp_path / "reports")


def test_main_missing_subject_is_refused(monkeypatch, tmp_path, subject):
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["docs/absent.md", str(subject)],
                  "{}", tmp_path / "reports")


def test_main_empty_completion_is_refused(monkeypatch, tmp_path, subject):
    """An attempt that produced nothing must not pass."""
    with pytest.raises(SystemExit):
        _run_main(monkeypatch, tmp_path, ["docs/d.md", str(subject)],
                  "   \n", tmp_path / "reports")


# --- extract_json beyond the happy path ---------------------------------------

def test_extract_json_outermost_braces_fallback():
    payload = {"subject": "d.md", "verdict": "PASS", "findings": []}
    text = f"Preamble prose. {json.dumps(payload)} Trailing prose."
    assert mod.extract_json(text) == payload


def test_extract_json_takes_the_first_fenced_block():
    first = {"subject": "a.md", "verdict": "PASS", "findings": []}
    second = {"subject": "b.md", "verdict": "PASS", "findings": []}
    text = f"```json\n{json.dumps(first)}\n```\nand\n```json\n{json.dumps(second)}\n```"
    assert mod.extract_json(text) == first


def test_extract_json_rejects_malformed_json():
    with pytest.raises(SystemExit):
        mod.extract_json("{not: valid json,}")
