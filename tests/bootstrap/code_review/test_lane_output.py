"""Tests for executable reviewer-lane output parsing."""

from __future__ import annotations

import json
from pathlib import Path

from bootstrap_lib.code_review import lane_output


LANE = "reviewer_a_claude_md_compliance"


def _issue(file: str, citation: str = "Use pathlib.Path for file paths.") -> str:
    return json.dumps(
        [{
            "file": file,
            "lines": "4",
            "reason": "claude_md",
            "description": "bad path",
            "citation": citation,
        }]
    )


def test_parser_verifies_git_path_from_prepared_bundle(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("Use pathlib.Path for file paths.\n", encoding="utf-8")
    bundle = {
        "changed_files": [{
            "path": "src/a.py",
            "local": str(tmp_path / "src" / "a.py"),
            "claude_mds": [str(claude_md)],
        }]
    }

    issues = lane_output.parse_lane_output(
        _issue("src/a.py"), lane=LANE, bundle=bundle
    )

    assert issues[0]["citation_verification"] == "verified"


def test_parser_maps_p4_depot_and_local_spellings(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("Use pathlib.Path for file paths.\n", encoding="utf-8")
    local = str(tmp_path / "src" / "a.py")
    bundle = {
        "changed_files": [{
            "depot": "//depot/src/a.py",
            "local": local,
            "claude_mds": [str(claude_md)],
        }]
    }

    for file_name in ("//depot/src/a.py", local):
        issues = lane_output.parse_lane_output(
            _issue(file_name), lane=LANE, bundle=bundle
        )
        assert issues[0]["citation_verification"] == "verified"


def test_parser_retains_unverifiable_citation(tmp_path: Path) -> None:
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("Use pathlib.Path for file paths.\n", encoding="utf-8")
    bundle = {
        "changed_files": [{
            "path": "src/a.py",
            "claude_mds": [str(claude_md)],
        }]
    }

    issues = lane_output.parse_lane_output(
        _issue("src/a.py", "Invented rule"), lane=LANE, bundle=bundle
    )

    assert issues[0]["citation_verification"] == "unverifiable"
    assert issues[0]["citation"] == "Invented rule"


def test_cli_retains_unverifiable_and_marks_empty_chain_unchecked(
    tmp_path: Path, capsys
) -> None:
    response = tmp_path / "response.json"
    response.write_text(_issue("a.py", "Invented rule"), encoding="utf-8")
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps({"changed_files": [{"path": "a.py", "claude_mds": []}]}),
        encoding="utf-8",
    )

    assert lane_output.main([
        "--lane", LANE, "--response", str(response), "--bundle", str(bundle)
    ]) == 0
    issues = json.loads(capsys.readouterr().out)
    assert issues[0]["citation_verification"] == "unchecked"
    assert issues[0]["citation"] == "Invented rule"
