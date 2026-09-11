"""Whole-post-image parser diagnostics must never become false clean coverage."""

from __future__ import annotations

import pytest
import yaml

from bootstrap_lib.code_review.mechanical import MechanicalSnapshot, REGISTRY, _run_checks
from bootstrap_lib.code_review.mechanical import structured_parse


CHECK = next(check for check in REGISTRY if check.check_id == "structured_parse")


@pytest.mark.parametrize(("file", "text", "line"), [
    ("data.json", '{"value": 1,\n"old": invalid}\n', 2),
    ("data.json", '{"value": 1\n', 2),
    ("data.yaml", "value: 1\nold: [\n", 3),
    ("data.yaml", "value: 1\nold: bad\x00value\n", 2),
    ("data.toml", "value = 1\nold =\n", 2),
    ("data.toml", "value = 1\nold = ", 2),
])
def test_unchanged_and_eof_errors_are_reported(file: str, text: str, line: int) -> None:
    snapshot = MechanicalSnapshot(file, "diff", "", ((1, "changed"),), (), text)
    checks, findings, diagnostics = _run_checks(snapshot, (CHECK,))
    assert checks == ["structured_parse"]
    assert diagnostics == []
    assert len(findings) == 1
    assert findings[0]["line"] == line


def test_missing_parser_location_is_uncovered(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_without_location(text: str) -> None:
        raise yaml.YAMLError("parse failed without a location")

    monkeypatch.setattr(structured_parse.yaml, "safe_load", fail_without_location)
    snapshot = MechanicalSnapshot("data.yaml", "diff", "", ((1, "changed"),), (), "value: 1")
    checks, findings, diagnostics = _run_checks(snapshot, (CHECK,))
    assert checks == []
    assert findings == []
    assert "parser supplied no usable location" in diagnostics[0]
