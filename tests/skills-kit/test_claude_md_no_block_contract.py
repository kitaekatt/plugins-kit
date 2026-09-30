"""A missing claude_md: block is never a FAIL on the yaml-contract row.

The block is optional: without it the file is validated against the default
rules. The contract row is NA; a block with another contract root and no
claude_md block adds one JUDGMENT row; a present claude_md block is validated;
a block that fails to parse FAILs. The verdict never depends on sibling files
or the directory dimension classifier.
"""

import pytest

from skills_kit_lib import document_walker
from skills_kit_lib.audit import FAIL, JUDGMENT, NA, audit

BODY = "# Some directory\n\nGuidance that carries no structured block.\n"
OTHER_ROOT = (
    "# x\n\n```yaml\nreference_skill:\n  facts: []\n```\n"
)
BAD_BLOCK = "# x\n\n```yaml\nclaude_md:\n  scope: {}\n```\n"
MALFORMED = "# x\n\n```yaml\nclaude_md:\n  scope: [unclosed\n```\n"
VALID = (
    "# x\n\n```yaml\nclaude_md:\n  _schema_version: \"1\"\n"
    "  scope:\n    directory: x\n    covers: [a]\n    excludes: [b]\n"
    "  conventions:\n    - rule: r\n      keywords: [k1, k2, k3]\n      why: w\n```\n"
)


def _rows(path):
    return audit(path)["yaml_contract"]


def _contract_verdicts(path):
    return [r["verdict"] for r in _rows(path) if r["rule"] == "yaml-contract"]


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_no_block_is_na_not_fail(tmp_path, name):
    md = tmp_path / name
    md.write_text(BODY)
    rows = _rows(md)
    assert [r["verdict"] for r in rows] == [NA]
    assert "validated against default rules" in rows[0]["note"]


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_other_root_only_is_na_plus_judgment(tmp_path, name):
    md = tmp_path / name
    md.write_text(OTHER_ROOT)
    verdicts = _contract_verdicts(md)
    assert verdicts == [NA, JUDGMENT]
    assert "reference_skill" in _rows(md)[1]["note"]
    assert "section 6.2" in _rows(md)[1]["note"]


def test_other_root_before_valid_claude_md_block_validates_claude_md(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(OTHER_ROOT + "\n" + VALID)
    rows = _rows(md)
    assert NA not in [r["verdict"] for r in rows]
    assert JUDGMENT not in [r["verdict"] for r in rows]
    assert FAIL not in [r["verdict"] for r in rows]
    assert audit(md)["yaml_root"] == "claude_md"


def test_other_root_before_invalid_claude_md_block_fails(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(OTHER_ROOT + "\n" + BAD_BLOCK)
    assert FAIL in _contract_verdicts(md)


def test_present_block_is_still_validated(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(BAD_BLOCK)
    assert FAIL in _contract_verdicts(md)


def test_malformed_block_fails(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(MALFORMED)
    assert _contract_verdicts(md) == [FAIL]


def test_malformed_claude_md_block_not_hidden_by_other_root(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(OTHER_ROOT + "\n" + MALFORMED)
    assert _contract_verdicts(md) == [FAIL]


def test_no_parser_with_no_block_is_na(tmp_path, monkeypatch):
    monkeypatch.setattr(document_walker, "HAVE_YAML", False)
    monkeypatch.setattr("skills_kit_lib.audit.HAVE_YAML", False)
    md = tmp_path / "CLAUDE.md"
    md.write_text(BODY)
    assert _contract_verdicts(md) == [NA]


def test_no_parser_with_claude_md_block_is_judgment(tmp_path, monkeypatch):
    monkeypatch.setattr(document_walker, "HAVE_YAML", False)
    md = tmp_path / "CLAUDE.md"
    md.write_text(OTHER_ROOT + "\n" + VALID)
    assert _contract_verdicts(md) == [JUDGMENT]


def test_verdict_ignores_dimension_classifier_and_siblings(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "x.cpp").write_text("int x;\n")
    (b / "noise.md").write_text("[broken](nowhere.md)\n")
    for d in (a, b):
        (d / "CLAUDE.md").write_text(BODY)
    assert audit(a / "CLAUDE.md")["yaml_contract"] == audit(b / "CLAUDE.md")["yaml_contract"]
    assert audit(a / "CLAUDE.md")["universal"] == audit(b / "CLAUDE.md")["universal"]
