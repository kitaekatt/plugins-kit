"""Tests for bootstrap_lib.code_review.pipeline.

Vendor-neutral: synthetic headers and sections, no p4 or git format
assumptions. The kit-specific front-halves (header grammar, range/CL
resolution, hygiene checks) are tested in tests/git-kit/ and
tests/p4-kit/, which also pin the delegating wrappers (run_git/run_p4,
split_git_diff_sections/split_diff_sections).
"""

import json
import os
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from bootstrap_lib.code_review.pipeline import (
    assemble_bundle,
    emit_bundle,
    run_vcs,
    split_sections,
)


# ---------------------------------------------------------------------------
# run_vcs
# ---------------------------------------------------------------------------


class TestRunVcs:
    def test_forces_utf8_decoding(self):
        """Encoding must be pinned to utf-8/replace -- Windows' default
        cp1252 decoder aborts on CJK/emoji diff content."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            rc, out, err = run_vcs("vcs-tool", ["status"])

        assert rc == 0
        assert out == "ok"
        assert captured["cmd"] == ["vcs-tool", "status"]
        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"
        assert captured.get("capture_output") is True

    def test_coalesces_none_output_to_empty_strings(self):
        fake = subprocess.CompletedProcess(["x"], 3, stdout=None, stderr=None)
        with patch.object(subprocess, "run", return_value=fake):
            rc, out, err = run_vcs("x", ["anything"])
        assert (rc, out, err) == (3, "", "")

    def test_cwd_passed_as_string(self, tmp_path):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            run_vcs("x", ["status"], cwd=tmp_path)
        assert captured.get("cwd") == str(tmp_path)

    def test_no_cwd_passes_none(self):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            run_vcs("x", ["status"])
        assert captured.get("cwd") is None


# ---------------------------------------------------------------------------
# split_sections
# ---------------------------------------------------------------------------


def _angle_header(line: str) -> Optional[dict]:
    """Toy header grammar: `<<ident>>` lines start a section."""
    if line.startswith("<<") and line.endswith(">>"):
        return {"ident": line[2:-2]}
    return None


class TestSplitSections:
    def test_splits_at_header_lines(self):
        text = "<<a>>\nbody a1\nbody a2\n<<b>>\nbody b\n"
        preamble, sections = split_sections(text, _angle_header)
        assert preamble == ""
        assert [s["ident"] for s in sections] == ["a", "b"]
        assert sections[0]["header"] == "<<a>>\n"
        assert sections[0]["body"] == "body a1\nbody a2\n"
        assert sections[1]["body"] == "body b\n"

    def test_header_fields_merged_into_section(self):
        def multi_field(line):
            if line == "HDR":
                return {"x": 1, "y": "two"}
            return None

        _, sections = split_sections("HDR\nbody\n", multi_field)
        assert sections == [{"x": 1, "y": "two", "header": "HDR\n", "body": "body\n"}]

    def test_preamble_before_first_header(self):
        preamble, sections = split_sections("loose text\n<<a>>\nb\n", _angle_header)
        assert preamble == "loose text\n"
        assert len(sections) == 1

    def test_all_preamble_when_nothing_matches(self):
        preamble, sections = split_sections("no headers here\nat all\n", _angle_header)
        assert preamble == "no headers here\nat all\n"
        assert sections == []

    def test_empty_input(self):
        assert split_sections("", _angle_header) == ("", [])

    def test_parse_header_sees_line_without_trailing_newline(self):
        seen: list[str] = []

        def recorder(line):
            seen.append(line)
            return None

        split_sections("one\ntwo\n", recorder)
        assert seen == ["one", "two"]

    def test_parse_header_dict_not_mutated_by_caller(self):
        fields = {"ident": "a"}

        def fixed(line):
            return fields if line == "H" else None

        _, sections = split_sections("H\nbody\n", fixed)
        assert sections[0]["body"] == "body\n"
        assert fields == {"ident": "a"}  # split_sections copies, never mutates


# ---------------------------------------------------------------------------
# assemble_bundle
# ---------------------------------------------------------------------------


def _sections_for(*idents: str) -> list[dict]:
    return [
        {"identifier": i, "text": f"<<{i}>>\n+line for {i}\n"} for i in idents
    ]


class TestAssembleBundle:
    def test_chunks_written_and_indexed(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("src/a.py", "src/b.py"),
            files=[
                {"identifier": "src/a.py", "local": None},
                {"identifier": "src/b.py", "local": None},
            ],
            bundle_dir=bundle_dir,
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
        )
        assert core["bundle_dir"] == str(bundle_dir)
        assert len(core["diff_chunks"]) == 1
        chunk_path = bundle_dir / core["diff_chunks"][0]["path"]
        text = chunk_path.read_text(encoding="utf-8")
        assert "+line for src/a.py" in text
        assert "+line for src/b.py" in text
        assert [cf["chunk_index"] for cf in core["changed_files"]] == [0, 0]

    def test_passthrough_fields_kept_and_identifier_dropped(self, tmp_path):
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("//depot/x.cpp"),
            files=[
                {
                    "identifier": "//depot/x.cpp",
                    "depot": "//depot/x.cpp",
                    "status": "A",
                    "local": None,
                }
            ],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=None,
        )
        cf = core["changed_files"][0]
        assert "identifier" not in cf
        assert cf["depot"] == "//depot/x.cpp"
        assert cf["status"] == "A"
        assert cf["chunk_index"] == 0
        assert cf["claude_mds"] == []

    def test_file_absent_from_diff_gets_none_chunk_index(self, tmp_path):
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("a"),
            files=[
                {"identifier": "a", "local": None},
                {"identifier": "not-in-diff", "local": None},
            ],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=None,
        )
        assert core["changed_files"][0]["chunk_index"] == 0
        assert core["changed_files"][1]["chunk_index"] is None

    def test_claude_mds_collected_and_deduped_in_order(self, tmp_path):
        ws = tmp_path / "ws"
        sub = ws / "src"
        sub.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("root rules\n", encoding="utf-8")
        (sub / "CLAUDE.md").write_text("src rules\n", encoding="utf-8")
        f1 = sub / "a.py"
        f2 = sub / "b.py"
        f1.write_text("a\n", encoding="utf-8")
        f2.write_text("b\n", encoding="utf-8")

        core = assemble_bundle(
            preamble="",
            sections=_sections_for("src/a.py", "src/b.py"),
            files=[
                {"identifier": "src/a.py", "local": str(f1)},
                {"identifier": "src/b.py", "local": str(f2)},
            ],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=ws,
        )
        # Nearest-ancestor first per file; unique list deduped, first-seen order.
        # Compare via normcase: collect_claude_mds resolve()s paths, which
        # canonicalizes drive-path case on Windows (a case-insensitive FS), so a
        # raw string compare against the tmp_path spelling is spuriously brittle.
        def _nc(paths):
            return [os.path.normcase(p) for p in paths]

        expected = _nc([str(sub / "CLAUDE.md"), str(ws / "CLAUDE.md")])
        assert _nc(core["changed_files"][0]["claude_mds"]) == expected
        assert _nc(core["unique_claude_mds"]) == expected

    def test_falsy_local_skips_claude_md_walk(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "CLAUDE.md").write_text("rules\n", encoding="utf-8")
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("x"),
            files=[{"identifier": "x", "local": None}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=ws,
        )
        assert core["changed_files"][0]["claude_mds"] == []
        assert core["unique_claude_mds"] == []

    def test_submit_gates_matched_to_files(self, tmp_path):
        ws = tmp_path / "ws"
        cfg = ws / "Config"
        cfg.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text(
            "**Submit gate:** Rebuild the config binaries.\n"
            "Applies to:\n"
            "- Config/\n",
            encoding="utf-8",
        )
        target = cfg / "values.csv"
        target.write_text("k,v\n", encoding="utf-8")

        core = assemble_bundle(
            preamble="",
            sections=_sections_for("Config/values.csv"),
            files=[{"identifier": "Config/values.csv", "local": str(target)}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=ws,
        )
        assert len(core["submit_gates"]) == 1
        gate = core["submit_gates"][0]
        assert gate["summary"] == "Rebuild the config binaries"
        assert gate["matched_files"] == [str(target)]

    def test_preamble_only_diff_yields_unattributed_chunk(self, tmp_path, capsys):
        core = assemble_bundle(
            preamble="unrecognized diff text\n",
            sections=[],
            files=[],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=None,
        )
        assert len(core["diff_chunks"]) == 1
        assert core["diff_chunks"][0]["files"] == []
        assert "0 sections" in capsys.readouterr().err

    def test_creates_bundle_dir(self, tmp_path):
        bundle_dir = tmp_path / "deep" / "nested" / "bundle"
        assemble_bundle(
            preamble="",
            sections=[],
            files=[],
            bundle_dir=bundle_dir,
            max_chunk_bytes=1024,
            workspace_root=None,
        )
        assert bundle_dir.is_dir()


# ---------------------------------------------------------------------------
# emit_bundle
# ---------------------------------------------------------------------------


class TestEmitBundle:
    def test_writes_json_and_mirrors_stdout(self, tmp_path, capsys):
        bundle = {"cl": "42", "diff_chunks": []}
        rc = emit_bundle(bundle, tmp_path)
        assert rc == 0
        on_disk = json.loads((tmp_path / "bundle.json").read_text(encoding="utf-8"))
        assert on_disk == bundle
        stdout = capsys.readouterr().out
        assert json.loads(stdout) == bundle
        assert stdout.endswith("\n")

    def test_disk_copy_has_trailing_newline(self, tmp_path, capsys):
        emit_bundle({"a": 1}, tmp_path)
        raw = (tmp_path / "bundle.json").read_text(encoding="utf-8")
        assert raw.endswith("}\n")
