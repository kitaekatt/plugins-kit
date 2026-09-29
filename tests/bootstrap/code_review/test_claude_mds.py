"""Tests for bootstrap_lib.code_review.claude_mds.

CLAUDE.md ancestor walk, submit-gate parsing, scope matching, and
end-to-end collection. Vendor-neutral: the same primitives back both
p4-kit and git-kit code-review skills.
"""

import os
from pathlib import Path

import pytest

from bootstrap_lib.code_review import claude_mds as pr


# ---------------------------------------------------------------------------
# collect_claude_mds -- ancestor walk
# ---------------------------------------------------------------------------


class TestCollectClaudeMds:
    def test_walks_to_workspace_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("root rule\n")
        sub = tmp_path / "src" / "module"
        sub.mkdir(parents=True)
        (sub.parent / "CLAUDE.md").write_text("src rule\n")
        target = sub / "file.cpp"
        target.write_text("code\n")

        result = pr.collect_claude_mds(target, tmp_path)
        # Nearest first
        assert len(result) == 2
        assert Path(result[0]).read_text() == "src rule\n"
        assert Path(result[1]).read_text() == "root rule\n"

    def test_agents_md_fallback_and_shadowing(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("root agents\n")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "CLAUDE.md").write_text("src claude\n")
        (sub / "AGENTS.md").write_text("src agents (shadowed)\n")
        target = sub / "file.cpp"
        target.write_text("code\n")

        result = pr.collect_claude_mds(target, tmp_path)
        assert [Path(p).read_text() for p in result] == [
            "src claude\n",
            "root agents\n",
        ]

    def test_no_claude_md(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        target = sub / "file.cpp"
        target.write_text("")
        assert pr.collect_claude_mds(target, tmp_path) == []

    def test_stops_at_workspace_root(self, tmp_path):
        # CLAUDE.md ABOVE workspace root should not be collected
        outer = tmp_path / "outer"
        outer.mkdir()
        (tmp_path / "CLAUDE.md").write_text("outer rule\n")  # outside workspace
        ws_root = outer / "ws"
        ws_root.mkdir()
        (ws_root / "CLAUDE.md").write_text("ws rule\n")
        sub = ws_root / "src"
        sub.mkdir()
        target = sub / "file.cpp"
        target.write_text("")

        result = pr.collect_claude_mds(target, ws_root)
        assert len(result) == 1
        assert Path(result[0]).read_text() == "ws rule\n"

    def test_no_workspace_root_walks_to_fs_root(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("rule\n")
        sub = tmp_path / "src"
        sub.mkdir()
        target = sub / "file.cpp"
        target.write_text("")
        result = pr.collect_claude_mds(target, None)
        # At minimum, should find tmp_path/CLAUDE.md
        assert any(Path(r).read_text() == "rule\n" for r in result)


# ---------------------------------------------------------------------------
# parse_submit_gates -- block detection and field extraction
# ---------------------------------------------------------------------------


class TestParseSubmitGatesWellFormed:
    def test_single_gate_basic(self):
        text = (
            "**Submit gate:** ./build.sh configbinaries must pass before submit.\n"
            "Applies to:\n"
            "- GameConfigs/Real/\n"
            "- GameConfigs/Test/\n"
            "\n"
            "These are the inputs to the C# ConfigTool validator.\n"
        )
        gates, warnings = pr.parse_submit_gates(text, "/x/CLAUDE.md")
        assert warnings == []
        assert len(gates) == 1
        g = gates[0]
        assert g["source"] == "/x/CLAUDE.md"
        assert g["summary"] == "./build.sh configbinaries must pass before submit"
        assert g["scope_paths"] == ["GameConfigs/Real/", "GameConfigs/Test/"]
        assert g["rationale"] == "These are the inputs to the C# ConfigTool validator."
        assert g["line_no"] == 1

    def test_summary_continuation_lines_fold_in(self):
        text = (
            "**Submit gate:** run the validator\n"
            "to make sure nothing broke.\n"
            "Applies to:\n"
            "- src/\n"
        )
        gates, warnings = pr.parse_submit_gates(text, "x")
        assert warnings == []
        assert gates[0]["summary"] == "run the validator to make sure nothing broke"

    def test_multiple_gates_in_one_file(self):
        text = (
            "**Submit gate:** A.\n"
            "Applies to:\n"
            "- a/\n"
            "\n"
            "Rationale A.\n"
            "\n"
            "**Submit gate:** B.\n"
            "Applies to:\n"
            "- b/\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert len(gates) == 2
        assert gates[0]["summary"] == "A"
        assert gates[0]["scope_paths"] == ["a/"]
        assert gates[0]["rationale"] == "Rationale A."
        assert gates[1]["summary"] == "B"
        assert gates[1]["scope_paths"] == ["b/"]
        assert gates[1]["rationale"] == ""

    def test_rationale_optional(self):
        text = (
            "**Submit gate:** quick check.\n"
            "Applies to:\n"
            "- src/\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert gates[0]["rationale"] == ""

    def test_applies_to_variant_phrasing(self):
        """The longer phrasing 'Applies to any CL that touches files under:' also works."""
        text = (
            "**Submit gate:** do the thing.\n"
            "Applies to any CL that touches files under:\n"
            "- foo/\n"
        )
        gates, warnings = pr.parse_submit_gates(text, "x")
        assert warnings == []
        assert gates[0]["scope_paths"] == ["foo/"]

    def test_star_bullets_also_accepted(self):
        text = (
            "**Submit gate:** x.\n"
            "Applies to:\n"
            "* a/\n"
            "* b/\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert gates[0]["scope_paths"] == ["a/", "b/"]

    def test_glob_scope_preserved(self):
        text = (
            "**Submit gate:** x.\n"
            "Applies to:\n"
            "- **/*.csv\n"
            "- GameConfigs/**/*.yaml\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert gates[0]["scope_paths"] == ["**/*.csv", "GameConfigs/**/*.yaml"]

    def test_marker_case_insensitive(self):
        text = (
            "**SUBMIT GATE:** x.\n"
            "Applies to:\n"
            "- a/\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert len(gates) == 1

    def test_heading_terminates_rationale(self):
        text = (
            "**Submit gate:** x.\n"
            "Applies to:\n"
            "- a/\n"
            "\n"
            "Rationale line.\n"
            "## Next heading\n"
            "This belongs to the next section, not the gate.\n"
        )
        gates, _ = pr.parse_submit_gates(text, "x")
        assert gates[0]["rationale"] == "Rationale line."

    def test_no_gates_returns_empty(self):
        text = "# Just a CLAUDE.md\n\nNo gates here.\n"
        gates, warnings = pr.parse_submit_gates(text, "x")
        assert gates == []
        assert warnings == []


class TestParseSubmitGatesMalformed:
    def test_missing_applies_to_warns_and_skips(self):
        text = "**Submit gate:** do something.\nNo applies-to here.\n"
        gates, warnings = pr.parse_submit_gates(text, "/x/CLAUDE.md")
        assert gates == []
        assert len(warnings) == 1
        assert "no 'Applies to:' section" in warnings[0]
        assert "/x/CLAUDE.md:1" in warnings[0]

    def test_empty_scope_list_warns_and_skips(self):
        text = (
            "**Submit gate:** do x.\n"
            "Applies to:\n"
            "\n"
            "Rationale.\n"
        )
        gates, warnings = pr.parse_submit_gates(text, "x")
        assert gates == []
        assert len(warnings) == 1
        assert "empty scope list" in warnings[0]

    def test_malformed_gate_does_not_eat_subsequent_gate(self):
        """A bad gate should not consume a following well-formed one."""
        text = (
            "**Submit gate:** bad gate, no scope.\n"
            "Some prose with no Applies-to.\n"
            "\n"
            "**Submit gate:** good gate.\n"
            "Applies to:\n"
            "- src/\n"
        )
        gates, warnings = pr.parse_submit_gates(text, "x")
        assert len(gates) == 1
        assert gates[0]["summary"] == "good gate"
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# match_gate_scope_to_files -- prefix and glob matching
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A real on-disk workspace root so resolve() works."""
    return tmp_path


def _make(workspace: Path, *rel_paths: str) -> list[str]:
    """Materialize files at the given workspace-relative paths and return absolute paths."""
    out: list[str] = []
    for rel in rel_paths:
        p = workspace / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        out.append(str(p))
    return out


class TestMatchPrefixScope:
    def test_directory_prefix_with_trailing_slash(self, workspace):
        files = _make(
            workspace,
            "GameConfigs/Real/a.csv",
            "GameConfigs/Real/sub/b.csv",
            "GameConfigs/Test/c.csv",
            "OtherDir/d.csv",
        )
        matched = pr.match_gate_scope_to_files(
            ["GameConfigs/Real/"], files, workspace
        )
        assert sorted(matched) == sorted(
            [
                str(workspace / "GameConfigs/Real/a.csv"),
                str(workspace / "GameConfigs/Real/sub/b.csv"),
            ]
        )

    def test_directory_prefix_without_trailing_slash(self, workspace):
        files = _make(workspace, "foo/bar/x.txt", "foo/barbaz/y.txt")
        matched = pr.match_gate_scope_to_files(["foo/bar"], files, workspace)
        # Must match foo/bar/ but NOT foo/barbaz/ (no false-prefix match).
        assert matched == [str(workspace / "foo/bar/x.txt")]

    def test_multiple_scopes_any_match(self, workspace):
        files = _make(workspace, "a/x.txt", "b/y.txt", "c/z.txt")
        matched = pr.match_gate_scope_to_files(["a/", "b/"], files, workspace)
        assert sorted(matched) == sorted(
            [str(workspace / "a/x.txt"), str(workspace / "b/y.txt")]
        )

    def test_preserves_input_order(self, workspace):
        files = _make(workspace, "a/x.txt", "a/y.txt", "a/z.txt")
        matched = pr.match_gate_scope_to_files(["a/"], files, workspace)
        assert matched == files

    def test_no_match_returns_empty(self, workspace):
        files = _make(workspace, "x/a.txt")
        matched = pr.match_gate_scope_to_files(["y/"], files, workspace)
        assert matched == []


class TestMatchGlobScope:
    def test_simple_glob(self, workspace):
        files = _make(workspace, "data/a.csv", "data/a.yaml", "data/sub/b.csv")
        matched = pr.match_gate_scope_to_files(["*.csv"], files, workspace)
        # fnmatch * is greedy across /, so this should match both csvs.
        assert sorted(matched) == sorted(
            [str(workspace / "data/a.csv"), str(workspace / "data/sub/b.csv")]
        )

    def test_anchored_glob(self, workspace):
        files = _make(workspace, "GameConfigs/x.yaml", "OtherDir/y.yaml")
        matched = pr.match_gate_scope_to_files(
            ["GameConfigs/*.yaml"], files, workspace
        )
        assert matched == [str(workspace / "GameConfigs/x.yaml")]


class TestMatchEdgeCases:
    def test_empty_scope_list_returns_empty(self, workspace):
        files = _make(workspace, "a/x.txt")
        assert pr.match_gate_scope_to_files([], files, workspace) == []

    def test_empty_files_returns_empty(self, workspace):
        assert pr.match_gate_scope_to_files(["a/"], [], workspace) is not None
        assert pr.match_gate_scope_to_files(["a/"], [], workspace) == []

    def test_skips_none_and_empty_strings(self, workspace):
        files = _make(workspace, "a/x.txt")
        with_holes = [None, ""] + files  # type: ignore[list-item]
        matched = pr.match_gate_scope_to_files(["a/"], with_holes, workspace)
        assert matched == files

    def test_no_workspace_falls_back_to_absolute_path(self, workspace):
        files = _make(workspace, "a/x.txt")
        # Pass workspace=None; matcher should test scopes against the absolute path.
        # An author-friendly scope like "a/" won't match an absolute path, so
        # the caller must instead pass the absolute prefix in that case.
        matched = pr.match_gate_scope_to_files(["a/"], files, None)
        assert matched == []

    @pytest.mark.skipif(os.name != "nt", reason="case-insensitive matching is Windows-only")
    def test_case_insensitive_on_windows(self, workspace):
        files = _make(workspace, "GameConfigs/Real/a.csv")
        matched = pr.match_gate_scope_to_files(
            ["gameconfigs/real/"], files, workspace
        )
        assert matched == files


# ---------------------------------------------------------------------------
# collect_submit_gates -- read CLAUDE.md, parse, match, emit warnings
# ---------------------------------------------------------------------------


class TestCollectSubmitGates:
    def test_end_to_end_match(self, workspace, tmp_path):
        files = _make(workspace, "GameConfigs/Real/a.csv", "OtherDir/b.txt")
        claude_md = workspace / "GameConfigs" / "CLAUDE.md"
        claude_md.write_text(
            "**Submit gate:** ./build.sh configbinaries.\n"
            "Applies to:\n"
            "- GameConfigs/Real/\n"
            "\n"
            "Rationale prose.\n"
        )
        gates = pr.collect_submit_gates([str(claude_md)], files, workspace)
        assert len(gates) == 1
        g = gates[0]
        assert g["summary"] == "./build.sh configbinaries"
        assert g["matched_files"] == [str(workspace / "GameConfigs/Real/a.csv")]
        assert g["rationale"] == "Rationale prose."
        assert g["source"] == str(claude_md)

    def test_unmatched_gate_is_dropped(self, workspace):
        files = _make(workspace, "OtherDir/b.txt")
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text(
            "**Submit gate:** x.\n"
            "Applies to:\n"
            "- GameConfigs/Real/\n"
        )
        gates = pr.collect_submit_gates([str(claude_md)], files, workspace)
        assert gates == []

    def test_malformed_gate_warns_to_stderr(self, workspace, capsys):
        files = _make(workspace, "a/x.txt")
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text("**Submit gate:** bad.\nNo applies-to here.\n")
        gates = pr.collect_submit_gates([str(claude_md)], files, workspace)
        assert gates == []
        captured = capsys.readouterr()
        assert "no 'Applies to:' section" in captured.err

    def test_unreadable_claude_md_warns_to_stderr(self, workspace, capsys):
        files = _make(workspace, "a/x.txt")
        gates = pr.collect_submit_gates(
            [str(workspace / "does-not-exist" / "CLAUDE.md")], files, workspace
        )
        assert gates == []
        captured = capsys.readouterr()
        assert "could not read" in captured.err

    def test_multiple_gates_one_file(self, workspace):
        files = _make(workspace, "a/x.txt", "b/y.txt")
        claude_md = workspace / "CLAUDE.md"
        claude_md.write_text(
            "**Submit gate:** alpha.\n"
            "Applies to:\n"
            "- a/\n"
            "\n"
            "**Submit gate:** beta.\n"
            "Applies to:\n"
            "- b/\n"
        )
        gates = pr.collect_submit_gates([str(claude_md)], files, workspace)
        assert [g["summary"] for g in gates] == ["alpha", "beta"]
        assert gates[0]["matched_files"] == [str(workspace / "a/x.txt")]
        assert gates[1]["matched_files"] == [str(workspace / "b/y.txt")]
