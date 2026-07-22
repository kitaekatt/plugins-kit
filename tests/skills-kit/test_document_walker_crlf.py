"""CRLF-tolerance regression tests for fenced-yaml-block extraction.

Bug history: the root CLAUDE.md audit reported "no fenced yaml block with a
claude_md root key found" even though a ```yaml claude_md block was present.
The failure was reproduced on a CRLF file and initially suspected to be a
fence-detection regex that didn't tolerate \\r\\n. Investigation (this test
file) shows the fence regex (document_walker._YAML_BLOCK_RE) and pyyaml both
already tolerate CRLF line endings fine -- and Path.read_text() normalizes
CRLF to LF via Python's universal-newlines text mode before the text ever
reaches the regex, so a real CRLF-intolerance bug was not reachable through
any read path in this library to begin with.

The actual mechanism: collect_yaml_units() swallowed ANY pyyaml parse
exception with a bare `except Exception: continue`, so a fenced block that
textually contained a recognized contract root key but had a genuine YAML
syntax error (the real defect: an unquoted `{}` inside a `keywords: [...]`
flow sequence in the root CLAUDE.md) was silently indistinguishable from "no
block at all" -- producing the exact misleading message that was mistaken
for a CRLF bug. The fix surfaces a distinct "yaml-parse-error" state instead
of silently falling through to "no-contract-yaml-block".

These tests lock in both facts: CRLF input with valid YAML parses cleanly
(regression guard), and CRLF input with a genuinely broken embedded YAML
block is reported as a parse error, not swallowed as "no block found".
"""

from skills_kit_lib.audit import FAIL, audit
from skills_kit_lib.document_walker import collect_yaml_units, extract_skill_type_unit


def _crlf(text: str) -> str:
    return text.replace("\n", "\r\n")


VALID_CLAUDE_MD_BODY = """# Test

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: test
    covers:
      - testing
  insights:
    - id: test_insight
      keywords: [a, b, c]
      summary: test
      detail: test detail
      origin: test
      added: "2026-01-01"
```
"""

# Reproduces the real defect: an unquoted `{}` inside a flow-sequence item
# is a genuine YAML syntax error, independent of line endings.
BROKEN_CLAUDE_MD_BODY = """# Test

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: test
    covers:
      - testing
  insights:
    - id: test_insight
      keywords: [a, plugins {}, c]
      summary: test
      detail: test detail
      origin: test
      added: "2026-01-01"
```
"""


class TestCrlfFenceExtraction:
    def test_crlf_valid_block_extracts_and_parses(self):
        body = _crlf(VALID_CLAUDE_MD_BODY)
        assert "\r\n" in body
        units, detected_root_no_parser, parse_error = collect_yaml_units(body)
        assert parse_error is None
        assert detected_root_no_parser is None
        roots = [root for root, _ in units]
        assert "claude_md" in roots

    def test_crlf_valid_block_via_extract_skill_type_unit(self):
        body = _crlf(VALID_CLAUDE_MD_BODY)
        data, err, root = extract_skill_type_unit(body)
        assert err == ""
        assert root == "claude_md"
        assert data is not None and "claude_md" in data

    def test_crlf_audit_passes_on_valid_block(self, tmp_path):
        p = tmp_path / "CLAUDE.md"
        p.write_bytes(_crlf(VALID_CLAUDE_MD_BODY).encode("utf-8"))
        report = audit(p)
        fails = [r for r in report["yaml_contract"] if r["verdict"] == FAIL]
        assert fails == [], fails


class TestMalformedBlockIsNotMisreportedAsMissing:
    def test_broken_block_surfaces_parse_error_not_no_block(self):
        body = _crlf(BROKEN_CLAUDE_MD_BODY)
        units, detected_root_no_parser, parse_error = collect_yaml_units(body)
        assert units == []
        assert parse_error is not None
        root, message = parse_error
        assert root == "claude_md"
        assert message

    def test_broken_block_extract_skill_type_unit_reports_parse_error(self):
        body = _crlf(BROKEN_CLAUDE_MD_BODY)
        data, err, root = extract_skill_type_unit(body)
        assert data is None
        assert err.startswith("yaml-parse-error")
        assert root == "claude_md"

    def test_broken_block_audit_fails_with_parse_error_message(self, tmp_path):
        p = tmp_path / "CLAUDE.md"
        p.write_bytes(_crlf(BROKEN_CLAUDE_MD_BODY).encode("utf-8"))
        report = audit(p)
        fails = [r for r in report["yaml_contract"] if r["verdict"] == FAIL]
        assert len(fails) == 1
        assert "failed to parse" in fails[0]["note"]
        assert "no fenced yaml block" not in fails[0]["note"]
