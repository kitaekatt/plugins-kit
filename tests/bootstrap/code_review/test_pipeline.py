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
    canonical_local,
    emit_bundle,
    matches_claim,
    preimage_relpath,
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
# matches_claim / preimage_relpath
# ---------------------------------------------------------------------------


class TestMatchesClaim:
    def test_empty_globs_never_match(self):
        assert matches_claim("a/CLAUDE.md", []) is False

    def test_recursive_glob_matches_any_depth(self):
        g = ["**/CLAUDE.md"]
        assert matches_claim("CLAUDE.md", g) is True            # root, no slash
        assert matches_claim("a/CLAUDE.md", g) is True
        assert matches_claim("a/b/c/CLAUDE.md", g) is True

    def test_recursive_glob_matches_depot_paths(self):
        assert matches_claim("//depot/proj/SKILL.md", ["**/SKILL.md"]) is True
        assert matches_claim("//depot/proj/foo.cpp", ["**/SKILL.md"]) is False

    def test_backslash_normalized(self):
        assert matches_claim("a\\b\\CLAUDE.md", ["**/CLAUDE.md"]) is True

    def test_non_recursive_glob_matches_whole_path(self):
        assert matches_claim("src/x.py", ["src/*.py"]) is True
        assert matches_claim("other/x.py", ["src/*.py"]) is False

    def test_basename_that_is_not_the_target_does_not_match(self):
        assert matches_claim("a/CLAUDE.md.bak", ["**/CLAUDE.md"]) is False

    def test_star_dot_md_matches_every_markdown_at_any_depth(self):
        # The single `**/*.md` glob the code-review skills now use supersedes the
        # older two-glob (CLAUDE.md/SKILL.md) form: it claims CLAUDE.md, SKILL.md,
        # and generic docs at any depth INCLUDING the repo root.
        g = ["**/*.md"]
        assert matches_claim("CLAUDE.md", g) is True             # root, no slash
        assert matches_claim("SKILL.md", g) is True
        assert matches_claim("README.md", g) is True             # root generic doc
        assert matches_claim("a/CLAUDE.md", g) is True
        assert matches_claim("a/b/SKILL.md", g) is True
        assert matches_claim("docs/design/notes.md", g) is True  # generic project doc
        assert matches_claim("//depot/proj/Foo.md", g) is True   # p4 depot path

    def test_star_dot_md_excludes_md_html_and_non_markdown(self):
        # Markdeep `.md.html` is NOT `.md` -- it must stay with the generic reviewers.
        g = ["**/*.md"]
        assert matches_claim("Docs/Server/ServerDesign.md.html", g) is False
        assert matches_claim("a/foo.markdown", g) is False
        assert matches_claim("a/foo.cpp", g) is False


class TestClaimExclusions:
    """`!pattern` carve-outs.

    Motivating defect (2026-07-28): git-kit claimed every changed `.md`, which
    pulled a skill's `references/*.md` away from the generic reviewers and handed
    it to md-domain's audit_project_doc lane -- an auditor whose criteria exclude
    anything inside a skills tree. No md-domain audit lane reads that file's prose, so
    claiming it removed its only real review. Without negation the claim could not
    express "every `.md` EXCEPT skill references".
    """

    SKILL_REFS = ["**/*.md", "!**/skills/*/references/*.md"]

    def test_excluded_shape_is_not_claimed(self):
        g = self.SKILL_REFS
        assert matches_claim("plugins/bootstrap/skills/bootstrap/references/engine-internals.md", g) is False
        assert matches_claim("plugins/git-kit/skills/git-code-review/references/md-domain-review.md", g) is False

    def test_everything_else_still_claimed(self):
        g = self.SKILL_REFS
        assert matches_claim("CLAUDE.md", g) is True
        assert matches_claim("plugins/skills-kit/skills/md-domain/SKILL.md", g) is True
        assert matches_claim("docs/design/notes.md", g) is True
        # A skill's OWN docs that are not references stay claimed.
        assert matches_claim("plugins/x/skills/y/SKILL.md", g) is True

    def test_exclusion_matches_at_the_root_too(self):
        # `**/` means "at any depth INCLUDING the root" -- a multi-segment tail
        # must honour that too, or a repo whose skills/ sits at the top level
        # silently keeps the fake gate.
        g = self.SKILL_REFS
        assert matches_claim("skills/foo/references/bar.md", g) is False

    def test_exclusion_wins_regardless_of_order(self):
        f = "a/skills/s/references/r.md"
        assert matches_claim(f, ["**/*.md", "!**/skills/*/references/*.md"]) is False
        assert matches_claim(f, ["!**/skills/*/references/*.md", "**/*.md"]) is False

    def test_exclusion_beats_an_exact_positive(self):
        # No positive pattern can re-claim an excluded file, however specific.
        g = ["a/skills/s/references/r.md", "!**/skills/*/references/*.md"]
        assert matches_claim("a/skills/s/references/r.md", g) is False

    def test_only_exclusions_claims_nothing(self):
        assert matches_claim("a/b.md", ["!**/*.md"]) is False
        assert matches_claim("a/b.cpp", ["!**/*.md"]) is False

    def test_exclusions_apply_to_depot_paths(self):
        # Shared lib: p4-kit passes depot paths, not repo-relative paths.
        g = self.SKILL_REFS
        assert matches_claim("//depot/proj/skills/s/references/r.md", g) is False
        assert matches_claim("//depot/proj/Docs/Design.md", g) is True

    def test_positive_only_lists_are_unchanged(self):
        # Regression guard for existing callers: no `!` means the old behavior.
        assert matches_claim("a/skills/s/references/r.md", ["**/*.md"]) is True
        assert matches_claim("a/CLAUDE.md", ["**/CLAUDE.md"]) is True
        assert matches_claim("src/x.py", ["src/*.py"]) is True


class TestPreimageRelpath:
    def test_under_pre_images_dir(self):
        rel = preimage_relpath("a/b/CLAUDE.md")
        assert rel.startswith("pre-images/")

    def test_distinct_identifiers_get_distinct_paths(self):
        # Same basename, different dirs -> must not collide.
        assert preimage_relpath("a/CLAUDE.md") != preimage_relpath("b/CLAUDE.md")

    def test_deterministic(self):
        assert preimage_relpath("//depot/x/CLAUDE.md") == preimage_relpath("//depot/x/CLAUDE.md")


# ---------------------------------------------------------------------------
# assemble_bundle -- claim exclusion / claimed_files
# ---------------------------------------------------------------------------


class TestAssembleBundleClaims:
    def test_no_claim_globs_is_byte_identical_contract(self, tmp_path):
        """Default (no claim_globs) must not add a claimed_files key."""
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("a/CLAUDE.md", "src/b.py"),
            files=[
                {"identifier": "a/CLAUDE.md", "local": None},
                {"identifier": "src/b.py", "local": None},
            ],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
        )
        assert "claimed_files" not in core
        assert len(core["changed_files"]) == 2  # both files reviewed generically

    def test_claimed_file_excluded_from_chunks_and_changed_files(self, tmp_path):
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("a/CLAUDE.md", "src/b.py"),
            files=[
                {"identifier": "a/CLAUDE.md", "local": None, "pre_image": None},
                {"identifier": "src/b.py", "local": None},
            ],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
            claim_globs=["**/CLAUDE.md"],
        )
        # changed_files has only the non-claimed file, identifier dropped.
        assert len(core["changed_files"]) == 1
        assert "identifier" not in core["changed_files"][0]
        # claimed_files carries the claimed one, identifier retained.
        assert len(core["claimed_files"]) == 1
        assert core["claimed_files"][0]["identifier"] == "a/CLAUDE.md"
        assert core["claimed_files"][0]["pre_image"] is None
        # The claimed file's diff is NOT in any chunk.
        all_text = "".join(
            (tmp_path / "b" / c["path"]).read_text(encoding="utf-8")
            for c in core["diff_chunks"]
        )
        assert "a/CLAUDE.md" not in all_text
        assert "src/b.py" in all_text

    def test_empty_claim_match_still_emits_empty_claimed_files(self, tmp_path):
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("src/b.py"),
            files=[{"identifier": "src/b.py", "local": None}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=None,
            claim_globs=["**/CLAUDE.md"],
        )
        assert core["claimed_files"] == []
        assert len(core["changed_files"]) == 1

    def test_claimed_file_remains_in_ruleset_and_submit_gates(self, tmp_path):
        ws = tmp_path / "ws"
        sub = ws / "src"
        sub.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text(
            "**Submit gate:** Rebuild.\nApplies to:\n- src/\n", encoding="utf-8"
        )
        claude = sub / "CLAUDE.md"
        claude.write_text("child rules\n", encoding="utf-8")

        core = assemble_bundle(
            preamble="",
            sections=_sections_for("src/CLAUDE.md"),
            files=[{"identifier": "src/CLAUDE.md", "local": str(claude)}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=ws,
            claim_globs=["**/CLAUDE.md"],
        )
        # Claimed, so not in changed_files...
        assert core["changed_files"] == []
        assert len(core["claimed_files"]) == 1
        entry = core["claimed_files"][0]
        # ...but its CLAUDE.md chain still populates unique_claude_mds (self + root).
        assert any(c.endswith("CLAUDE.md") for c in core["unique_claude_mds"])
        assert len(core["unique_claude_mds"]) >= 2
        # claude_mds attached to the claimed entry, nearest-first, includes self.
        assert os.path.normcase(entry["claude_mds"][0]) == os.path.normcase(str(claude))
        # The submit gate (scope src/) still fires on the claimed file.
        assert len(core["submit_gates"]) == 1
        assert core["submit_gates"][0]["matched_files"] == [str(claude)]


# ---------------------------------------------------------------------------
# canonical_local -- emitted local agrees with the CLAUDE.md chain (case fix)
# ---------------------------------------------------------------------------


def _mixed_case_spelling(path_str: str) -> str:
    """Mimic p4 `where`: lowercase drive letter + forward slashes on Windows."""
    if os.name == "nt" and len(path_str) > 1 and path_str[1] == ":":
        return path_str[0].lower() + path_str[1:].replace("\\", "/")
    return path_str


class TestCanonicalLocal:
    def test_idempotent_on_resolved_path(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x\n", encoding="utf-8")
        resolved = str(f.resolve())
        assert canonical_local(resolved) == resolved

    def test_falsy_passthrough(self):
        assert canonical_local(None) is None
        assert canonical_local("") == ""

    def test_claimed_local_matches_claude_mds_self_entry(self, tmp_path):
        """The reported bug: a claimed CLAUDE.md's `local` differed in case from
        its own `claude_mds` self-entry, so a consumer removing the self-entry by
        string compare failed. The emitted local must now agree byte-for-byte."""
        ws = tmp_path / "ws"
        sub = ws / "sub"
        sub.mkdir(parents=True)
        claude = sub / "CLAUDE.md"
        claude.write_text("child rules\n", encoding="utf-8")

        core = assemble_bundle(
            preamble="",
            sections=_sections_for("sub/CLAUDE.md"),
            files=[{
                "identifier": "sub/CLAUDE.md",
                "local": _mixed_case_spelling(str(claude)),
                "pre_image": None,
            }],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=ws,
            claim_globs=["**/CLAUDE.md"],
        )
        entry = core["claimed_files"][0]
        # claude_mds[0] is the subject's own resolved CLAUDE.md; local must equal it.
        assert entry["local"] == entry["claude_mds"][0]

    def test_changed_file_local_is_canonicalized(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "a.py"
        f.write_text("x\n", encoding="utf-8")
        core = assemble_bundle(
            preamble="",
            sections=_sections_for("a.py"),
            files=[{"identifier": "a.py", "local": _mixed_case_spelling(str(f))}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024,
            workspace_root=ws,
        )
        assert core["changed_files"][0]["local"] == str(f.resolve())


# ---------------------------------------------------------------------------
# assemble_bundle -- triviality profile on claimed files
# ---------------------------------------------------------------------------


def _md_section(ident: str, header: str, *lines: str) -> dict:
    """A claimed-file diff section with a real unified-diff hunk."""
    body = "\n".join(lines)
    return {"identifier": ident, "text": f"diff --git a/{ident} b/{ident}\n{header}\n{body}\n"}


class TestAssembleBundleTriviality:
    def test_trivial_claimed_file_gets_profile_and_checks(self, tmp_path):
        pre = tmp_path / "pre.md"
        pre.write_text("# Notes\n\nsome text with teh typo\n", encoding="utf-8")
        core = assemble_bundle(
            preamble="",
            sections=[_md_section(
                "docs/notes.md", "@@ -3,1 +3,1 @@",
                "-some text with teh typo", "+some text with the typo",
            )],
            files=[{"identifier": "docs/notes.md", "local": None, "pre_image": str(pre)}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
            claim_globs=["**/*.md"],
        )
        entry = core["claimed_files"][0]
        assert entry["trivial"] is True
        assert entry["trivial_reasons"] == []
        assert entry["trivial_checks"] == {"ascii_clean": True, "no_abs_paths": True}

    def test_non_trivial_claimed_file_reports_reasons_no_checks(self, tmp_path):
        pre = tmp_path / "pre.md"
        pre.write_text("## Foo\n", encoding="utf-8")
        core = assemble_bundle(
            preamble="",
            sections=[_md_section("a/CLAUDE.md", "@@ -1,1 +1,1 @@", "-## Foo", "+## Bar")],
            files=[{"identifier": "a/CLAUDE.md", "local": None, "pre_image": str(pre)}],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
            claim_globs=["**/*.md"],
        )
        entry = core["claimed_files"][0]
        assert entry["trivial"] is False
        assert "structure_changed" in entry["trivial_reasons"]
        assert "trivial_checks" not in entry  # only emitted for trivial files

    def test_missing_pre_image_fails_closed_when_diff_needs_it(self, tmp_path):
        # pre_image points nowhere: an edit hunk cannot reconstruct -> not trivial.
        core = assemble_bundle(
            preamble="",
            sections=[_md_section("d/x.md", "@@ -1,1 +1,1 @@", "-old line", "+new line")],
            files=[{
                "identifier": "d/x.md", "local": None,
                "pre_image": str(tmp_path / "nonexistent.md"),
            }],
            bundle_dir=tmp_path / "b",
            max_chunk_bytes=1024 * 1024,
            workspace_root=None,
            claim_globs=["**/*.md"],
        )
        assert core["claimed_files"][0]["trivial"] is False


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
