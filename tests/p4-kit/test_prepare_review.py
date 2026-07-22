"""Tests for p4-kit scripts/prepare_review.py."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import prepare_review as pr


def _concat_diff_from_chunks(bundle: dict) -> str:
    """Read all chunk files for a bundle and concatenate -- the historical
    bundle["diff"] string, reconstructed from on-disk chunks.

    Tests that used to assert `bundle["diff"]` should call this instead.
    """
    bundle_dir = Path(bundle["bundle_dir"])
    return "".join(
        (bundle_dir / entry["path"]).read_text(encoding="utf-8")
        for entry in bundle["diff_chunks"]
    )


# ---------------------------------------------------------------------------
# run_p4 — subprocess invocation
# ---------------------------------------------------------------------------


class TestRunP4:
    def test_forces_utf8_decoding(self):
        """On Windows, default text decoding is cp1252 — CJK bytes abort the reader.

        `run_p4` must pin encoding to utf-8 with errors='replace' so diffs with
        non-Latin-1 content (CJK, emoji) decode cleanly on any platform.
        """
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            result = subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")
            return result

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_p4(["describe", "-du", "123"])

        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"
        assert captured.get("capture_output") is True
        # Must NOT pass text=True alongside encoding (the combination is fine
        # but text=True alone — without encoding — is the original bug).
        assert captured.get("text") is not True or captured.get("encoding") == "utf-8"

    def test_coalesces_none_stdout_to_empty_string(self):
        """If the subprocess produced no output, callers should see '' not None."""
        fake = subprocess.CompletedProcess(["p4"], 0, stdout=None, stderr=None)
        with patch.object(subprocess, "run", return_value=fake):
            rc, out, err = pr.run_p4(["info"])
        assert rc == 0
        assert out == ""
        assert err == ""

    def test_decodes_cjk_content(self, tmp_path):
        """End-to-end: a p4-like command emitting UTF-8 CJK bytes decodes cleanly."""
        # Use python itself as a stand-in for `p4` to emit known UTF-8 bytes.
        # We can't easily reroute the argv[0]="p4" inside run_p4, so patch
        # subprocess.run to simulate the Popen/read path with real bytes decoding.
        payload = "差分 diff with emoji 🧪 and CJK 互動\n"
        fake = subprocess.CompletedProcess(["p4"], 0, stdout=payload, stderr="")
        with patch.object(subprocess, "run", return_value=fake):
            rc, out, _ = pr.run_p4(["describe", "-du", "1"])
        assert rc == 0
        assert "互動" in out
        assert "🧪" in out


# ---------------------------------------------------------------------------
# parse_description
# ---------------------------------------------------------------------------


class TestParseDescription:
    def test_single_line(self):
        out = (
            "Change 12345 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tAdd inventory overflow check\n"
            "\n"
            "Affected files ...\n"
        )
        assert pr.parse_description(out) == "Add inventory overflow check"

    def test_multi_line(self):
        out = (
            "Change 12345 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tFix race in quest item pickup\n"
            "\t\n"
            "\tThe inventory lock was being released early.\n"
            "\n"
            "Affected files ...\n"
        )
        result = pr.parse_description(out)
        assert "Fix race in quest item pickup" in result
        assert "inventory lock was being released early" in result

    def test_no_description(self):
        assert pr.parse_description("") == ""


# ---------------------------------------------------------------------------
# parse_depot_files
# ---------------------------------------------------------------------------


class TestParseDepotFiles:
    def test_extracts_depot_paths(self):
        out = (
            "Differences ...\n"
            "\n"
            "==== //depot/foo/bar.cpp#3 (text) ====\n"
            "@@ -1,3 +1,4 @@\n"
            "==== //depot/foo/baz.h#1 (text) ====\n"
            "@@ -10,5 +10,6 @@\n"
        )
        assert pr.parse_depot_files(out) == [
            "//depot/foo/bar.cpp",
            "//depot/foo/baz.h",
        ]

    def test_no_files(self):
        assert pr.parse_depot_files("Change 1 ...\n") == []

    def test_ignores_non_header_lines(self):
        out = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "+ ==== fake header in diff ====\n"
            "==== //depot/b.cpp#2 (text) ====\n"
        )
        assert pr.parse_depot_files(out) == ["//depot/a.cpp", "//depot/b.cpp"]


# ---------------------------------------------------------------------------
# parse_file_actions
# ---------------------------------------------------------------------------


class TestParseFileActions:
    def test_affected_files_section(self):
        out = (
            "Change 100 by u@c on 2026/01/01\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/a.cpp#3 edit\n"
            "... //depot/new.py#1 add\n"
            "... //depot/gone.py#2 delete\n"
            "\n"
            "Differences ...\n"
            "==== //depot/a.cpp#3 (text) ====\n"
        )
        assert pr.parse_file_actions(out) == {
            "//depot/a.cpp": ("3", "edit"),
            "//depot/new.py": ("1", "add"),
            "//depot/gone.py": ("2", "delete"),
        }

    def test_shelved_files_section(self):
        out = (
            "Shelved files ...\n"
            "\n"
            "... //depot/x.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.parse_file_actions(out) == {"//depot/x.py": ("1", "add")}

    def test_move_actions(self):
        out = (
            "Affected files ...\n"
            "... //depot/new.py#1 move/add\n"
            "... //depot/old.py#5 move/delete\n"
            "Differences ...\n"
        )
        assert pr.parse_file_actions(out) == {
            "//depot/new.py": ("1", "move/add"),
            "//depot/old.py": ("5", "move/delete"),
        }

    def test_empty(self):
        assert pr.parse_file_actions("") == {}


# ---------------------------------------------------------------------------
# split_diff_sections
# ---------------------------------------------------------------------------


class TestSplitDiffSections:
    def test_splits_by_file_header(self):
        diff = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/b.cpp#2 (text) ====\n"
            "@@ -5 +5 @@\n"
            "-foo\n"
            "+bar\n"
        )
        preamble, sections = pr.split_diff_sections(diff)
        assert preamble == ""
        assert len(sections) == 2
        assert sections[0]["depot"] == "//depot/a.cpp"
        assert sections[0]["rev"] == "1"
        assert "@@ -1 +1 @@" in sections[0]["body"]
        assert sections[1]["depot"] == "//depot/b.cpp"
        assert sections[1]["rev"] == "2"

    def test_empty_body_for_add(self):
        diff = (
            "==== //depot/edit.py#2 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        _, sections = pr.split_diff_sections(diff)
        assert len(sections) == 2
        assert "@@" in sections[0]["body"]
        assert "@@" not in sections[1]["body"]

    def test_preamble_before_first_header(self):
        diff = "some preamble text\n==== //depot/a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        preamble, sections = pr.split_diff_sections(diff)
        assert "preamble" in preamble
        assert len(sections) == 1


# ---------------------------------------------------------------------------
# synthesize_add_hunk / synthesize_delete_hunk
# ---------------------------------------------------------------------------


class TestSynthesizeHunks:
    def test_add_hunk_prefixes_plus(self):
        content = "line one\nline two\nline three\n"
        hunk = pr.synthesize_add_hunk(content)
        assert "@@ -0,0 +1,3 @@" in hunk
        assert "+line one" in hunk
        assert "+line three" in hunk

    def test_add_hunk_empty_content(self):
        assert pr.synthesize_add_hunk("") == ""

    def test_delete_hunk_prefixes_minus(self):
        content = "old a\nold b\n"
        hunk = pr.synthesize_delete_hunk(content)
        assert "@@ -1,2 +0,0 @@" in hunk
        assert "-old a" in hunk
        assert "-old b" in hunk


# ---------------------------------------------------------------------------
# fetch_file_content — p4 print spec selection
# ---------------------------------------------------------------------------


class TestFetchFileContent:
    def test_shelved_add_uses_at_equals_cl(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "1", "12345", is_shelved=True, is_delete=False)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py@=12345"]

    def test_submitted_add_uses_hash_rev(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "7", "12345", is_shelved=False, is_delete=False)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#7"]

    def test_submitted_delete_uses_prior_rev(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content("//depot/x.py", "5", "12345", is_shelved=False, is_delete=True)
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#4"]

    def test_submitted_delete_at_rev_1_returns_none(self):
        # No rev 0 to fetch — pre-history has no content.
        with patch.object(pr, "run_p4") as mock:
            result = pr.fetch_file_content(
                "//depot/x.py", "1", "12345", is_shelved=False, is_delete=True
            )
        assert result is None
        assert mock.call_count == 0

    def test_shelved_delete_uses_head(self):
        with patch.object(pr, "run_p4", return_value=(0, "content\n", "")) as mock:
            pr.fetch_file_content(
                "//depot/x.py", "3", "12345", is_shelved=True, is_delete=True
            )
        assert mock.call_args_list[0][0][0] == ["print", "-q", "//depot/x.py#head"]

    def test_p4_failure_returns_none(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "error")):
            result = pr.fetch_file_content(
                "//depot/x.py", "1", "12345", is_shelved=False, is_delete=False
            )
        assert result is None


# ---------------------------------------------------------------------------
# extract_diff
# ---------------------------------------------------------------------------


class TestExtractDiff:
    def test_returns_content_after_marker(self):
        out = "header\n\nDifferences ...\n\n==== //a#1 (text) ====\n@@ -1 +1 @@\n"
        result = pr.extract_diff(out)
        assert result.startswith("==== //a#1")

    def test_no_differences_section(self):
        assert pr.extract_diff("just a header\n") == ""

    def test_no_actions_returns_raw(self):
        out = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        # actions=None → behave like a passthrough
        result = pr.extract_diff(out, actions=None)
        assert "==== //a.cpp#1" in result

    def test_synthesizes_add_hunk_for_pure_add(self):
        describe = (
            "Affected files ...\n"
            "... //depot/edit.py#2 edit\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.py#2 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/new.py#1"]:
                return (0, "def foo():\n    return 42\n", "")
            return (1, "", "unexpected p4 call")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="100", is_shelved=False)

        # Both files present in output
        assert "==== //depot/edit.py#2" in diff
        assert "==== //depot/new.py#1" in diff
        # Edit file's hunk preserved
        assert "@@ -1 +1 @@" in diff
        assert "-a" in diff
        assert "+b" in diff
        # Add file's synthesized hunk present
        assert "@@ -0,0 +1,2 @@" in diff
        assert "+def foo():" in diff
        assert "+    return 42" in diff

    def test_synthesizes_delete_hunk_for_delete(self):
        describe = (
            "Affected files ...\n"
            "... //depot/gone.py#5 delete\n"
            "Differences ...\n"
            "\n"
            "==== //depot/gone.py#5 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/gone.py#4"]:
                return (0, "old line 1\nold line 2\n", "")
            return (1, "", "unexpected")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="200", is_shelved=False)

        assert "@@ -1,2 +0,0 @@" in diff
        assert "-old line 1" in diff
        assert "-old line 2" in diff

    def test_shelved_add_uses_at_equals_cl(self):
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args == ["print", "-q", "//depot/new.py@=144072"]:
                return (0, "hello\n", "")
            return (1, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="144072", is_shelved=True)

        assert ["print", "-q", "//depot/new.py@=144072"] in calls
        assert "+hello" in diff

    def test_mixed_cl_adds_missing_from_differences_synthesized(self):
        """Mixed CL: pure-adds listed in Shelved files but omitted from Differences.

        Real p4 behavior on some servers: `p4 describe -du -S` for a shelved mixed
        CL emits `==== ...====` headers ONLY for edits. Pure-add files appear only
        in the Shelved files listing, never in the Differences section. The script
        must still synthesize sections (header + hunk) for these missing files,
        not drop them silently.
        """
        describe = (
            "Shelved files ...\n"
            "... //depot/edit.cpp#3 edit\n"
            "... //depot/add1.cpp#1 add\n"
            "... //depot/add2.cpp#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.cpp#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/add1.cpp@=99"]:
                return (0, "content of add1\n", "")
            if args == ["print", "-q", "//depot/add2.cpp@=99"]:
                return (0, "content of add2\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="99", is_shelved=True)

        # All three files must appear in the diff
        assert "==== //depot/edit.cpp#3" in diff
        assert "==== //depot/add1.cpp#1" in diff
        assert "==== //depot/add2.cpp#1" in diff
        # Edit preserved
        assert "-old" in diff
        assert "+new" in diff
        # Adds synthesized
        assert "+content of add1" in diff
        assert "+content of add2" in diff

    def test_warns_on_unhandled_add_to_stderr(self, capsys):
        describe = (
            "Affected files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file")):
            pr.extract_diff(describe, actions, cl="300", is_shelved=False)
        err = capsys.readouterr().err
        assert "could not synthesize" in err
        assert "//depot/new.py" in err

    def test_stderr_notes_synthesized_files(self, capsys):
        describe = (
            "Affected files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(0, "x\n", "")):
            pr.extract_diff(describe, actions, cl="400", is_shelved=False)
        err = capsys.readouterr().err
        assert "synthesized add hunks" in err
        assert "//depot/new.py" in err


# ---------------------------------------------------------------------------
# _p4_diff_to_sections -- thin adapter feeding bootstrap_lib's chunker
# ---------------------------------------------------------------------------


class TestP4DiffToSections:
    def test_passes_preamble_and_depot_as_identifier(self):
        diff = (
            "preamble line\n"
            "==== //depot/foo/bar.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/foo/baz.h#2 (text) ====\n"
            "@@ -5 +5 @@\n"
            "-a\n"
            "+b\n"
        )
        preamble, sections = pr._p4_diff_to_sections(diff)
        assert preamble == "preamble line\n"
        assert len(sections) == 2
        assert sections[0]["identifier"] == "//depot/foo/bar.cpp"
        assert sections[0]["text"].startswith("==== //depot/foo/bar.cpp#1")
        assert "-old" in sections[0]["text"]
        assert sections[1]["identifier"] == "//depot/foo/baz.h"


# ---------------------------------------------------------------------------
# has_describe_content
# ---------------------------------------------------------------------------


class TestHasDescribeContent:
    def test_true_when_marker_and_file_header(self):
        out = "Differences ...\n==== //depot/a.cpp#1 (text) ====\n"
        assert pr.has_describe_content(out)

    def test_true_when_add_only_has_header_no_hunk(self):
        # The bug fix: pure-add sections have no @@ but we still want to accept the describe.
        out = "Differences ...\n==== //depot/new.py#1 (text) ====\n\n"
        assert pr.has_describe_content(out)

    def test_false_when_no_differences_section(self):
        assert not pr.has_describe_content("Affected files ...\n")

    def test_false_when_differences_empty(self):
        assert not pr.has_describe_content("Differences ...\n(no files)\n")

    def test_true_for_pure_add_with_only_affected_files_section(self):
        """Pure-add CLs may emit no Differences-section headers at all.

        Perforce gives pure-add CLs no `==== ====` headers under
        `Differences ...` because there is no prior version to diff against.
        The synthesizable add action listed under `Affected files ...`
        is enough to mark the describe as reviewable -- extract_diff
        will fill in the diff body via p4 print.
        """
        out = (
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.has_describe_content(out)

    def test_true_for_pure_delete_with_only_shelved_files_section(self):
        out = (
            "Shelved files ...\n"
            "\n"
            "... //depot/old.py#3 delete\n"
            "\n"
            "Differences ...\n"
        )
        assert pr.has_describe_content(out)

    def test_false_for_edit_only_section_without_differences_headers(self):
        """Edits cannot be synthesized -- they need real diff bodies.

        A describe that lists only edits in Affected files but has no
        Differences-section headers means the diff is genuinely missing
        (e.g. a pending edit that hasn't been shelved yet). Reject it so
        callers fall through to the shelved fallback.
        """
        out = (
            "Affected files ...\n"
            "\n"
            "... //depot/changed.py#5 edit\n"
            "\n"
        )
        assert not pr.has_describe_content(out)


# ---------------------------------------------------------------------------
# fetch_describe — shelved fallback
# ---------------------------------------------------------------------------


class TestFetchDescribe:
    def test_committed_returns_first_with_is_shelved_false(self):
        committed_out = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"
        with patch.object(pr, "run_p4", return_value=(0, committed_out, "")) as mock:
            out, is_shelved = pr.fetch_describe("123")
            assert out == committed_out
            assert is_shelved is False
            assert mock.call_count == 1
            assert mock.call_args_list[0][0][0] == ["describe", "-du", "123"]

    def test_shelved_fallback_used(self):
        empty = "Affected files ...\n... //depot/a.cpp#1 edit\n"
        shelved = "Differences ...\n==== //a.cpp#1 (text) ====\n@@ -1 +1 @@\n"

        def side(args):
            if "-S" in args:
                return (0, shelved, "")
            return (0, empty, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            out, is_shelved = pr.fetch_describe("123")
            assert out == shelved
            assert is_shelved is True
            assert mock.call_count == 2

    def test_raises_when_neither_has_differences(self):
        empty = "Affected files ...\n"
        with patch.object(pr, "run_p4", return_value=(0, empty, "")):
            with pytest.raises(ValueError, match="no describe content"):
                pr.fetch_describe("123")

    def test_accepts_add_only_cl_without_hunks(self):
        # An add-only CL has file headers but no @@ — should still succeed.
        add_only = (
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, add_only, "")):
            out, is_shelved = pr.fetch_describe("123")
        assert out == add_only
        assert is_shelved is False

    def test_accepts_submitted_pure_add_with_no_differences_headers(self):
        """Submitted pure-add CLs whose `==== ====` headers are missing under Differences.

        Some Perforce describe responses for submitted pure-add CLs list the
        adds in `Affected files ...` but emit an empty `Differences ...`
        section -- there is nothing to diff against. extract_diff can still
        synthesize hunks via `p4 print #<rev>`, so fetch_describe should
        accept this output rather than rejecting it.
        """
        submitted_pure_add = (
            "Change 999 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, submitted_pure_add, "")) as mock:
            out, is_shelved = pr.fetch_describe("999")
        assert is_shelved is False
        # No fallback to -S needed since the submitted output is reviewable.
        assert mock.call_count == 1

    def test_pending_pure_add_routed_to_shelved_path(self):
        """Pending pure-add CLs must be read via -S so synthesis uses @=<CL>.

        Going through the regular describe would return is_shelved=False,
        and `p4 print #1` for a pending add would fail (no submitted rev
        exists). Forcing the -S path returns is_shelved=True so synthesis
        uses the shelved spec @=<CL>, which works.
        """
        pending_unshelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
        )
        pending_shelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
        )

        def side(args):
            if "-S" in args:
                return (0, pending_shelved, "")
            return (0, pending_unshelved, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            out, is_shelved = pr.fetch_describe("144098")
        assert is_shelved is True
        assert out == pending_shelved
        assert mock.call_count == 2

    def test_pending_unshelved_raises_pending_unshelved_error(self):
        """A pending CL with no shelved content raises PendingUnshelvedError.

        Distinct from a generic `no describe content` failure so build_bundle
        can react with auto-shelve + retry instead of propagating.
        """
        pending_unshelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
        )
        empty_shelved = (
            "Change 144098 by user@client on 2026/01/01 12:00:00 *pending*\n"
        )

        def side(args):
            if "-S" in args:
                return (0, empty_shelved, "")
            return (0, pending_unshelved, "")

        with patch.object(pr, "run_p4", side_effect=side):
            with pytest.raises(pr.PendingUnshelvedError):
                pr.fetch_describe("144098")


# ---------------------------------------------------------------------------
# resolve_local_paths
# ---------------------------------------------------------------------------


class TestResolveLocalPaths:
    def test_parses_ztag_where_output(self):
        out = (
            "... depotFile //depot/a.cpp\n"
            "... clientFile //ws/a.cpp\n"
            "... path C:\\workspace\\a.cpp\n"
            "\n"
            "... depotFile //depot/b.h\n"
            "... clientFile //ws/b.h\n"
            "... path C:\\workspace\\b.h\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result = pr.resolve_local_paths(["//depot/a.cpp", "//depot/b.h"])
        assert result == {
            "//depot/a.cpp": "C:\\workspace\\a.cpp",
            "//depot/b.h": "C:\\workspace\\b.h",
        }

    def test_empty_input(self):
        assert pr.resolve_local_paths([]) == {}

    def test_p4_failure_returns_none_for_each(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "error")):
            result = pr.resolve_local_paths(["//depot/a.cpp"])
        assert result == {"//depot/a.cpp": None}


# ---------------------------------------------------------------------------
# compute_minimal_dirs
# ---------------------------------------------------------------------------


class TestComputeMinimalDirs:
    def test_collapses_descendants(self, tmp_path):
        a = tmp_path / "a"
        ab = a / "b"
        c = tmp_path / "c"
        ab.mkdir(parents=True)
        c.mkdir()
        files = [str(a / "f1.cpp"), str(ab / "f2.cpp"), str(c / "f3.cpp")]
        result = pr.compute_minimal_dirs(files)
        # /a covers /a/b → only /a and /c remain, both recursive
        assert {(p.resolve(), r) for p, r in result} == {
            (a.resolve(), True),
            (c.resolve(), True),
        }

    def test_skips_none_and_missing(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        files = [None, str(real / "x.cpp"), str(tmp_path / "ghost" / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {(real.resolve(), True)}

    def test_empty(self):
        assert pr.compute_minimal_dirs([]) == []

    def test_unique_dir_kept(self, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        files = [str(a / "x.cpp"), str(a / "y.cpp"), str(a / "z.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert len(result) == 1
        assert result[0][0].resolve() == a.resolve()
        assert result[0][1] is True

    def test_sibling_dirs_both_kept(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        files = [str(a / "x.cpp"), str(b / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {
            (a.resolve(), True),
            (b.resolve(), True),
        }

    def test_workspace_root_does_not_absorb_descendants(self, tmp_path):
        """A CL touching a workspace-root file plus a deep file must NOT collapse
        to a recursive scan of the entire workspace. The root is kept as a
        non-recursive scan target (`<root>/*`); the deep dir keeps its own
        recursive scan separately. See module docstring for rationale."""
        ws = tmp_path / "ws"
        deep = ws / "plugins" / "p4-kit" / "scripts"
        deep.mkdir(parents=True)
        files = [str(ws / "CLAUDE.md"), str(deep / "prepare_review.py")]
        result = pr.compute_minimal_dirs(files, workspace_root=ws)
        assert {(p.resolve(), r) for p, r in result} == {
            (ws.resolve(), False),
            (deep.resolve(), True),
        }

    def test_workspace_root_only_is_non_recursive(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        files = [str(ws / "CLAUDE.md"), str(ws / "marketplace.json")]
        result = pr.compute_minimal_dirs(files, workspace_root=ws)
        assert result == [(ws.resolve(), False)]

    def test_no_workspace_root_arg_preserves_old_collapse(self, tmp_path):
        """When workspace_root is None, the function still collapses ancestors."""
        a = tmp_path / "a"
        ab = a / "b"
        ab.mkdir(parents=True)
        files = [str(a / "x.cpp"), str(ab / "y.cpp")]
        result = pr.compute_minimal_dirs(files)
        assert {(p.resolve(), r) for p, r in result} == {(a.resolve(), True)}


# ---------------------------------------------------------------------------
# find_unreconciled
# ---------------------------------------------------------------------------


class TestFindUnreconciled:
    def test_parses_ztag_output(self, tmp_path):
        d = tmp_path / "src"
        d.mkdir()
        out = (
            "... depotFile //depot/src/new.cpp\n"
            "... clientFile /ws/src/new.cpp\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/edited.cpp\n"
            "... clientFile /ws/src/edited.cpp\n"
            "... rev 3\n"
            "... action edit\n"
            "... type text\n"
            "\n"
            "... depotFile //depot/src/gone.cpp\n"
            "... clientFile /ws/src/gone.cpp\n"
            "... rev 2\n"
            "... action delete\n"
            "... type text\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result = pr.find_unreconciled([(d, True)])
        assert result == [
            {"local": "/ws/src/new.cpp", "depot": "//depot/src/new.cpp", "action": "add"},
            {"local": "/ws/src/edited.cpp", "depot": "//depot/src/edited.cpp", "action": "edit"},
            {"local": "/ws/src/gone.cpp", "depot": "//depot/src/gone.cpp", "action": "delete"},
        ]

    def test_uses_recursive_dir_specs(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        captured: list[list[str]] = []

        def fake_run_p4(args):
            captured.append(args)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.find_unreconciled([(a, True), (b, True)])

        assert captured[0][:3] == ["-ztag", "reconcile", "-n"]
        # Each dir is passed as a recursive `<dir>/...` spec.
        specs = captured[0][3:]
        assert any(s.endswith("/...") and str(a) in s for s in specs)
        assert any(s.endswith("/...") and str(b) in s for s in specs)

    def test_non_recursive_dir_uses_star_spec(self, tmp_path):
        """A `(dir, False)` entry must scan with `<dir>/*` (immediate children only),
        not `<dir>/...` -- this is what bounds the workspace-root scan."""
        root = tmp_path / "ws"
        deep = root / "plugins" / "scripts"
        deep.mkdir(parents=True)
        captured: list[list[str]] = []

        def fake_run_p4(args):
            captured.append(args)
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.find_unreconciled([(root, False), (deep, True)])

        specs = captured[0][3:]
        assert f"{root}/*" in specs
        assert f"{deep}/..." in specs
        # The recursive workspace-root spec must NOT appear.
        assert f"{root}/..." not in specs

    def test_empty_dirs_returns_empty(self):
        # No p4 call should be made when there's nothing to scan.
        with patch.object(pr, "run_p4") as mock:
            assert pr.find_unreconciled([]) == []
        assert mock.call_count == 0

    def test_no_files_to_reconcile_treated_as_empty(self, tmp_path):
        d = tmp_path / "x"
        d.mkdir()
        with patch.object(
            pr,
            "run_p4",
            return_value=(1, "", "/ws/x - no file(s) to reconcile.\n"),
        ):
            assert pr.find_unreconciled([(d, True)]) == []

    def test_p4_failure_returns_empty_with_warning(self, tmp_path, capsys):
        d = tmp_path / "x"
        d.mkdir()
        with patch.object(pr, "run_p4", return_value=(1, "", "fatal: bad workspace\n")):
            assert pr.find_unreconciled([(d, True)]) == []
        err = capsys.readouterr().err
        assert "reconcile check failed" in err

    def test_skips_entries_missing_required_fields(self, tmp_path):
        # Defensive: an incomplete record (no action, or no clientFile) is dropped.
        d = tmp_path / "x"
        d.mkdir()
        out = (
            "... depotFile //depot/orphan.cpp\n"
            "... rev 1\n"
            "\n"
            "... clientFile /ws/no-action.cpp\n"
            "... depotFile //depot/no-action.cpp\n"
            "\n"
            "... depotFile //depot/good.cpp\n"
            "... clientFile /ws/good.cpp\n"
            "... action add\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            result = pr.find_unreconciled([(d, True)])
        assert len(result) == 1
        assert result[0]["local"] == "/ws/good.cpp"


# ---------------------------------------------------------------------------
# build_bundle — integration
# ---------------------------------------------------------------------------


class TestBuildBundle:
    def test_full_pipeline(self, tmp_path):
        # Set up a fake workspace with one file and one CLAUDE.md
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("workspace rule\n")
        local_file = src / "foo.cpp"
        local_file.write_text("int x = 1;\n")

        describe_out = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tFix the thing\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            return (1, "", "")

        bundle_dir = tmp_path / "bundle"
        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("999", bundle_dir)

        assert bundle["cl"] == "999"
        assert bundle["description"] == "Fix the thing"
        assert bundle["bundle_dir"] == str(bundle_dir)
        # Diff content lives in chunk files on disk now; no inline `diff` field.
        assert "diff" not in bundle
        assert len(bundle["diff_chunks"]) >= 1
        assert "==== //depot/src/foo.cpp#1" in _concat_diff_from_chunks(bundle)
        assert len(bundle["changed_files"]) == 1
        cf = bundle["changed_files"][0]
        assert cf["depot"] == "//depot/src/foo.cpp"
        assert Path(cf["local"]) == local_file
        # The single-file CL should land in a single chunk (index 0).
        assert cf["chunk_index"] == 0
        assert len(cf["claude_mds"]) == 1
        assert Path(cf["claude_mds"][0]).read_text() == "workspace rule\n"
        assert len(bundle["unique_claude_mds"]) == 1
        assert bundle["unreconciled"] == []

    def test_unreconciled_files_surfaced(self, tmp_path):
        """build_bundle reports files missing from the CL via `p4 reconcile -n`."""
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        local_file = src / "foo.cpp"
        local_file.write_text("int x = 1;\n")
        # A sibling file that exists on disk but isn't in the CL.
        forgotten = src / "forgot.cpp"
        forgotten.write_text("int y = 2;\n")

        describe_out = (
            "Change 1000 by user@client on 2026/01/01\n"
            "\n"
            "\tEdit foo\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {local_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"
        reconcile_out = (
            "... depotFile //depot/src/forgot.cpp\n"
            f"... clientFile {forgotten}\n"
            "... rev 1\n"
            "... action add\n"
            "... type text\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (0, reconcile_out, "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1000", tmp_path / "bundle")

        assert len(bundle["unreconciled"]) == 1
        u = bundle["unreconciled"][0]
        assert u["action"] == "add"
        assert u["depot"] == "//depot/src/forgot.cpp"
        assert Path(u["local"]) == forgotten

    def test_root_level_cl_file_does_not_trigger_recursive_root_scan(self, tmp_path):
        """Regression: a CL touching a workspace-root file (e.g. CLAUDE.md) plus a
        deep file used to collapse to `<root>/...`, recursively scanning every
        untracked dir in the workspace (Binaries/, Intermediate/, IDE files, etc.
        that may not all be in .p4ignore). The fix bounds root to `<root>/*`."""
        ws = tmp_path / "ws"
        deep = ws / "plugins" / "p4-kit" / "scripts"
        deep.mkdir(parents=True)
        root_file = ws / "CLAUDE.md"
        root_file.write_text("rules\n")
        deep_file = deep / "prepare_review.py"
        deep_file.write_text("# code\n")

        describe_out = (
            "Change 7 by u@c on 2026/01/01\n"
            "\n"
            "\tEdit\n"
            "\n"
            "Affected files ...\n"
            "... //depot/CLAUDE.md#1 edit\n"
            "... //depot/plugins/p4-kit/scripts/prepare_review.py#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/CLAUDE.md#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
            "==== //depot/plugins/p4-kit/scripts/prepare_review.py#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-a\n"
            "+b\n"
        )
        where_out = (
            "... depotFile //depot/CLAUDE.md\n"
            f"... path {root_file}\n"
            "\n"
            "... depotFile //depot/plugins/p4-kit/scripts/prepare_review.py\n"
            f"... path {deep_file}\n"
        )
        info_out = f"... clientRoot {ws}\n"
        captured_reconcile_specs: list[str] = []

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                captured_reconcile_specs.extend(args[3:])
                return (1, "", "no file(s) to reconcile.\n")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            pr.build_bundle("7", tmp_path / "bundle")

        ws_resolved = ws.resolve()
        deep_resolved = deep.resolve()
        # Root scanned non-recursively
        assert f"{ws_resolved}/*" in captured_reconcile_specs
        # Recursive root scan must NOT appear (this was the bug)
        assert f"{ws_resolved}/..." not in captured_reconcile_specs
        # Deep dir keeps its own recursive scan (not absorbed by root)
        assert f"{deep_resolved}/..." in captured_reconcile_specs

    def test_mixed_edit_and_add_synthesizes_add_hunk(self, tmp_path):
        """Regression: a CL with edits + pure adds must include synthesized hunks for adds."""
        ws = tmp_path / "ws"
        ws.mkdir()
        edit_local = ws / "edit.py"
        add_local = ws / "new.py"
        edit_local.write_text("x = 2\n")
        add_local.write_text("")  # not needed; content comes from p4 print mock

        describe_out = (
            "Change 144072 by user@client on 2026/01/01\n"
            "\n"
            "\tMixed edit + add CL\n"
            "\n"
            "Affected files ...\n"
            "... //depot/edit.py#3 edit\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.py#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            "==== //depot/new.py#1 (text) ====\n"
            "\n"
        )
        where_out = (
            "... depotFile //depot/edit.py\n"
            f"... path {edit_local}\n"
            "\n"
            "... depotFile //depot/new.py\n"
            f"... path {add_local}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, describe_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args == ["print", "-q", "//depot/new.py#1"]:
                return (0, "def brief():\n    pass\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("144072", tmp_path / "bundle")

        # Both files in changed_files
        depots = [f["depot"] for f in bundle["changed_files"]]
        assert "//depot/edit.py" in depots
        assert "//depot/new.py" in depots

        diff = _concat_diff_from_chunks(bundle)
        # Edit hunk intact
        assert "+x = 2" in diff
        # Add hunk synthesized
        assert "@@ -0,0 +1,2 @@" in diff
        assert "+def brief():" in diff

    def test_add_only_shelved_cl(self, tmp_path):
        """A shelved CL containing only adds (no edits) must bundle full content."""
        ws = tmp_path / "ws"
        ws.mkdir()

        # Committed describe finds no Differences section → shelved fallback.
        committed_out = "Change 1 by u@c on 2026/01/01\n\n\tdesc\n\nShelved files ...\n"
        shelved_out = (
            "Change 1 by u@c on 2026/01/01\n"
            "\n"
            "\tAdd new modules\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/a.py#1 add\n"
            "... //depot/b.py#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/a.py#1 (text) ====\n"
            "\n"
            "==== //depot/b.py#1 (text) ====\n"
            "\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, committed_out, "")
            if args[:3] == ["describe", "-du", "-S"]:
                return (0, shelved_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, "", "")  # no local mapping needed for this test
            if args[:2] == ["-ztag", "info"]:
                return (0, f"... clientRoot {ws}\n", "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args == ["print", "-q", "//depot/a.py@=1"]:
                return (0, "content of a\n", "")
            if args == ["print", "-q", "//depot/b.py@=1"]:
                return (0, "content of b\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("1", tmp_path / "bundle")

        assert bundle["description"] == "Add new modules"
        diff = _concat_diff_from_chunks(bundle)
        assert "+content of a" in diff
        assert "+content of b" in diff

    def test_mixed_cl_adds_omitted_from_differences(self, tmp_path):
        """Regression (Spirit Crossing CL 144098): on some p4 servers, `describe -du -S`
        for a shelved mixed CL emits ==== headers ONLY for edits. Pure-adds appear only
        in the Shelved files listing. Previously these were silently dropped.
        """
        ws = tmp_path / "ws"
        ws.mkdir()

        committed_out = "Change 144098 by u@c on 2026/01/01 *pending*\n"
        shelved_out = (
            "Change 144098 by u@c on 2026/01/01 *pending*\n"
            "\n"
            "\tModule + facade wiring\n"
            "\n"
            "Shelved files ...\n"
            "\n"
            "... //depot/facade.cpp#4 edit\n"
            "... //depot/mod_a.cpp#1 add\n"
            "... //depot/mod_b.cpp#1 add\n"
            "... //depot/mod_c.cpp#1 add\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/facade.cpp#4 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-wire_old();\n"
            "+wire_new();\n"
        )

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"] and "-S" not in args:
                return (0, committed_out, "")
            if args[:3] == ["describe", "-du", "-S"]:
                return (0, shelved_out, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, "", "")
            if args[:2] == ["-ztag", "info"]:
                return (0, f"... clientRoot {ws}\n", "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args == ["print", "-q", "//depot/mod_a.cpp@=144098"]:
                return (0, "mod_a contents\n", "")
            if args == ["print", "-q", "//depot/mod_b.cpp@=144098"]:
                return (0, "mod_b contents\n", "")
            if args == ["print", "-q", "//depot/mod_c.cpp@=144098"]:
                return (0, "mod_c contents\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("144098", tmp_path / "bundle")

        # All four files in changed_files — not just the edit
        depots = [f["depot"] for f in bundle["changed_files"]]
        assert depots == [
            "//depot/facade.cpp",
            "//depot/mod_a.cpp",
            "//depot/mod_b.cpp",
            "//depot/mod_c.cpp",
        ]

        diff = _concat_diff_from_chunks(bundle)
        # Edit preserved
        assert "+wire_new();" in diff
        # Adds synthesized with full content, even though they had no ==== header in Differences
        assert "==== //depot/mod_a.cpp#1" in diff
        assert "==== //depot/mod_b.cpp#1" in diff
        assert "==== //depot/mod_c.cpp#1" in diff
        assert "+mod_a contents" in diff
        assert "+mod_b contents" in diff
        assert "+mod_c contents" in diff


# ---------------------------------------------------------------------------
# fetch_shelf_fingerprint
# ---------------------------------------------------------------------------


class TestFetchShelfFingerprint:
    def test_empty_when_p4_fails(self):
        """`p4 fstat ... @=<CL>` with no shelf returns non-zero; treat as empty."""
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file(s)")):
            assert pr.fetch_shelf_fingerprint("123") == {}

    def test_parses_multi_file_shelf(self):
        out = (
            "... depotFile //depot/a.cpp\n"
            "... headRev 3\n"
            "... digest AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            "\n"
            "... depotFile //depot/b.cpp\n"
            "... headRev 5\n"
            "... digest BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")) as mock:
            fp = pr.fetch_shelf_fingerprint("123")
        assert fp == {
            "//depot/a.cpp": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "//depot/b.cpp": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        }
        # Confirm the spec uses the shelved revspec.
        assert mock.call_args[0][0] == ["-ztag", "fstat", "-Ol", "//...@=123"]

    def test_delete_with_no_digest_recorded_as_empty(self):
        """Shelved deletes have no digest; record as empty string so file presence still counts."""
        out = (
            "... depotFile //depot/gone.cpp\n"
            "... headRev 7\n"
            "... headAction delete\n"
            "\n"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            fp = pr.fetch_shelf_fingerprint("123")
        assert fp == {"//depot/gone.cpp": ""}

    def test_no_trailing_blank_line_still_captured(self):
        """Last record may not end with blank line; must still be parsed."""
        out = (
            "... depotFile //depot/a.cpp\n"
            "... digest AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        )
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            fp = pr.fetch_shelf_fingerprint("123")
        assert fp == {"//depot/a.cpp": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}


# ---------------------------------------------------------------------------
# auto_shelve_cl
# ---------------------------------------------------------------------------


class TestAutoShelveCl:
    def test_shelve_failure_raises(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "perm denied")):
            with pytest.raises(ValueError, match="p4 shelve -c 123 failed"):
                pr.auto_shelve_cl("123")

    def test_shelve_success_but_empty_shelf_raises(self):
        """Pathological: shelve reports success but no shelved files appear."""
        def side(args):
            if args[0] == "shelve":
                return (0, "Change 123 files shelved.\n", "")
            return (0, "", "")  # fstat returns nothing -> empty fingerprint

        with patch.object(pr, "run_p4", side_effect=side):
            with pytest.raises(ValueError, match="no shelved files were found"):
                pr.auto_shelve_cl("123")

    def test_returns_post_shelve_fingerprint(self):
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest ABCDEF0123456789ABCDEF0123456789\n"
            "\n"
        )

        def side(args):
            if args[0] == "shelve":
                return (0, "Change 123 files shelved.\n", "")
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side) as mock:
            fp = pr.auto_shelve_cl("123")
        assert fp == {"//depot/x.cpp": "ABCDEF0123456789ABCDEF0123456789"}
        # First call is the shelve, second is the fingerprint fstat.
        assert mock.call_args_list[0][0][0] == ["shelve", "-c", "123"]
        assert mock.call_args_list[1][0][0] == ["-ztag", "fstat", "-Ol", "//...@=123"]


# ---------------------------------------------------------------------------
# cleanup_auto_shelve
# ---------------------------------------------------------------------------


class TestCleanupAutoShelve:
    def _write_bundle(self, tmp_path: Path, bundle: dict) -> Path:
        bundle_dir = tmp_path / bundle["cl"]
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
        return bundle_dir

    def test_missing_bundle_returns_1(self, tmp_path, capsys):
        rc = pr.cleanup_auto_shelve(tmp_path / "nope")
        assert rc == 1
        assert "no bundle.json" in capsys.readouterr().err

    def test_not_auto_shelved_silent_noop(self, tmp_path, capsys):
        """auto_shelved=false means we did not create the shelf -- do nothing, silently."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {"cl": "123", "auto_shelved": False, "shelf_fingerprint": {}},
        )
        with patch.object(pr, "run_p4") as mock:
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert capsys.readouterr().err == ""
        assert mock.call_count == 0  # no p4 calls at all

    def test_fingerprint_match_triggers_delete(self, tmp_path, capsys):
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
        )

        calls = []

        def side(args):
            calls.append(args)
            if args[0] == "-ztag" and args[1] == "fstat":
                return (0, fstat_out, "")
            if args[:2] == ["shelve", "-d"]:
                return (0, "Shelf deleted.\n", "")
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert any(c[:3] == ["shelve", "-d", "-c"] for c in calls)
        assert "deleted auto-created shelf" in capsys.readouterr().err

    def test_fingerprint_mismatch_skips_delete(self, tmp_path, capsys):
        """Author reshelved with different content; leave their work alone."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest CAFEBABE\n"
            "\n"
        )
        calls = []

        def side(args):
            calls.append(args)
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)
        assert "shelf changed" in capsys.readouterr().err

    def test_fingerprint_extra_file_skips_delete(self, tmp_path, capsys):
        """Author added a file to the shelf since we recorded it; don't delete."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
            "... depotFile //depot/y.cpp\n"
            "... digest 12345678\n"
            "\n"
        )
        calls = []

        def side(args):
            calls.append(args)
            return (0, fstat_out, "")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)

    def test_shelf_gone_noop(self, tmp_path, capsys):
        """Author already submitted or deleted the shelf; nothing to do."""
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        calls = []

        def side(args):
            calls.append(args)
            return (1, "", "no such file(s)")  # empty shelf

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 0
        assert not any(c[:2] == ["shelve", "-d"] for c in calls)
        assert "already gone" in capsys.readouterr().err

    def test_delete_failure_returns_1(self, tmp_path, capsys):
        bundle_dir = self._write_bundle(
            tmp_path,
            {
                "cl": "123",
                "auto_shelved": True,
                "shelf_fingerprint": {"//depot/x.cpp": "DEADBEEF"},
            },
        )
        fstat_out = (
            "... depotFile //depot/x.cpp\n"
            "... digest DEADBEEF\n"
            "\n"
        )

        def side(args):
            if args[0] == "-ztag" and args[1] == "fstat":
                return (0, fstat_out, "")
            return (1, "", "shelf is locked")

        with patch.object(pr, "run_p4", side_effect=side):
            rc = pr.cleanup_auto_shelve(bundle_dir)
        assert rc == 1
        assert "shelf is locked" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_bundle — auto-shelve integration
# ---------------------------------------------------------------------------


class TestBuildBundleAutoShelve:
    def _committed_describe(self) -> str:
        """Minimal committed describe output for a hand-off to the rest of build_bundle."""
        return (
            "Change 1 by u@c on 2026/01/01 12:00:00\n"
            "\n"
            "\tdesc\n"
            "\n"
            "Affected files ...\n"
            "\n"
            "... //depot/new.py#1 add\n"
            "\n"
            "Differences ...\n"
            "==== //depot/new.py#1 (text) ====\n"
            "@@ -0,0 +1,1 @@\n"
            "+hello\n"
        )

    def test_pending_unshelved_triggers_auto_shelve(self, tmp_path):
        """build_bundle catches PendingUnshelvedError, shelves, retries, marks auto_shelved=true."""
        shelved_describe = self._committed_describe()
        shelve_calls = []
        fstat_calls = 0

        def fake_fetch_describe(cl):
            # First call raises; second call (after auto-shelve) succeeds.
            if not shelve_calls:
                raise pr.PendingUnshelvedError("no shelf")
            return shelved_describe, True

        def fake_fingerprint(cl):
            nonlocal fstat_calls
            fstat_calls += 1
            # 1st call: pre-shelve check -> empty (no race).
            # 2nd call: post-shelve fingerprint.
            if fstat_calls == 1:
                return {}
            return {"//depot/new.py": "DEADBEEF"}

        def fake_auto_shelve(cl):
            shelve_calls.append(cl)
            return {"//depot/new.py": "DEADBEEF"}

        with patch.object(pr, "fetch_describe", side_effect=fake_fetch_describe), \
                patch.object(pr, "fetch_shelf_fingerprint", side_effect=fake_fingerprint), \
                patch.object(pr, "auto_shelve_cl", side_effect=fake_auto_shelve), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=None), \
                patch.object(pr, "find_unreconciled", return_value=[]), \
                patch.object(pr, "find_unresolved", return_value=[]):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        assert bundle["auto_shelved"] is True
        assert bundle["shelf_fingerprint"] == {"//depot/new.py": "DEADBEEF"}
        assert shelve_calls == ["123"]

    def test_pre_shelve_race_skips_auto_shelve(self, tmp_path):
        """If a shelf appears between PendingUnshelvedError and our re-check, don't auto-shelve."""
        shelved_describe = self._committed_describe()
        attempts = {"describe": 0, "shelve": 0}

        def fake_fetch_describe(cl):
            attempts["describe"] += 1
            if attempts["describe"] == 1:
                raise pr.PendingUnshelvedError("no shelf")
            return shelved_describe, True

        def fake_fingerprint(cl):
            # Race: someone else shelved between the failed describe and our check.
            return {"//depot/other.py": "FEEDFACE"}

        def fake_auto_shelve(cl):
            attempts["shelve"] += 1
            return {}

        with patch.object(pr, "fetch_describe", side_effect=fake_fetch_describe), \
                patch.object(pr, "fetch_shelf_fingerprint", side_effect=fake_fingerprint), \
                patch.object(pr, "auto_shelve_cl", side_effect=fake_auto_shelve), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=None), \
                patch.object(pr, "find_unreconciled", return_value=[]), \
                patch.object(pr, "find_unresolved", return_value=[]):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        # We did NOT shelve; someone else owns this shelf.
        assert attempts["shelve"] == 0
        assert bundle["auto_shelved"] is False
        assert bundle["shelf_fingerprint"] == {}

    def test_normal_path_records_no_auto_shelve(self, tmp_path):
        """When fetch_describe succeeds directly, no shelve happens and the bundle reflects that."""
        shelved_describe = self._committed_describe()

        with patch.object(pr, "fetch_describe", return_value=(shelved_describe, False)), \
                patch.object(pr, "auto_shelve_cl") as shelve_mock, \
                patch.object(pr, "fetch_shelf_fingerprint", return_value={}), \
                patch.object(pr, "resolve_local_paths", return_value={"//depot/new.py": None}), \
                patch.object(pr, "get_workspace_root", return_value=None), \
                patch.object(pr, "find_unreconciled", return_value=[]), \
                patch.object(pr, "find_unresolved", return_value=[]):
            bundle = pr.build_bundle("123", tmp_path / "bundle")

        assert shelve_mock.call_count == 0
        assert bundle["auto_shelved"] is False
        assert bundle["shelf_fingerprint"] == {}


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------


class TestMain:
    def test_no_args_returns_2(self, capsys):
        rc = pr.main(["prepare_review.py"])
        assert rc == 2
        assert "Usage" in capsys.readouterr().err

    def test_value_error_returns_1(self, capsys, tmp_path):
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "reviews"), \
                patch.object(pr, "build_bundle", side_effect=ValueError("nope")):
            rc = pr.main(["prepare_review.py", "123"])
        assert rc == 1
        assert "nope" in capsys.readouterr().err

    def test_success_prints_json_and_persists_bundle(self, capsys, tmp_path):
        """main() prints the bundle to stdout AND persists bundle.json next to chunks."""
        reviews_root = tmp_path / "reviews"
        fake_bundle = {"cl": "123", "bundle_dir": str(reviews_root / "123")}
        with patch.object(pr, "DEFAULT_BUNDLE_ROOT", reviews_root), \
                patch.object(pr, "build_bundle", return_value=fake_bundle):
            rc = pr.main(["prepare_review.py", "123"])
        assert rc == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == fake_bundle
        persisted = json.loads((reviews_root / "123" / "bundle.json").read_text())
        assert persisted == fake_bundle

    def test_cleanup_routes_to_cleanup_auto_shelve(self, tmp_path):
        bundle_dir = tmp_path / "bundle"
        with patch.object(pr, "cleanup_auto_shelve", return_value=0) as mock:
            rc = pr.main(["prepare_review.py", "--cleanup", str(bundle_dir)])
        assert rc == 0
        assert mock.call_args[0][0] == bundle_dir

    def test_cleanup_without_bundle_dir_returns_2(self, capsys):
        rc = pr.main(["prepare_review.py", "--cleanup"])
        assert rc == 2
        assert "Usage" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Binary/filetype guard in add/delete hunk synthesis (G5)
# ---------------------------------------------------------------------------


class TestIsTextFiletype:
    @pytest.mark.parametrize(
        "filetype",
        [
            "text", "text+x", "text+ko", "ktext", "xtext", "ltext",
            "unicode", "utf8", "utf16", "symlink",
            # Unknown/missing types default to text (historical behavior).
            "", None, "weirdtype",
        ],
    )
    def test_text_like(self, filetype):
        assert pr._is_text_filetype(filetype) is True

    @pytest.mark.parametrize(
        "filetype",
        [
            "binary", "binary+l", "binary+lFS64", "xbinary", "ubinary",
            "apple", "resource", "uresource", "tempobj", "ctempobj",
            "Binary",  # case-insensitive
        ],
    )
    def test_binary_like(self, filetype):
        assert pr._is_text_filetype(filetype) is False


class TestSplitDiffSectionsCapturesType:
    def test_type_field_captured_from_header(self):
        diff = (
            "==== //depot/a.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "==== //depot/b.uasset#2 (binary+l) ====\n"
            "\n"
        )
        _, sections = pr.split_diff_sections(diff)
        assert sections[0]["type"] == "text"
        assert sections[1]["type"] == "binary+l"


class TestExtractDiffBinaryGuard:
    def test_binary_add_in_differences_gets_placeholder_not_content(self):
        """A shelved binary add with a ==== header must NOT be content-inlined."""
        describe = (
            "Shelved files ...\n"
            "... //depot/Content/big.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/Content/big.uasset#1 (binary+l) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args[:1] == ["fstat"] and "fileSize" in args:
                return (0, "... fileSize 123456\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="99", is_shelved=True)

        assert "==== //depot/Content/big.uasset#1 (binary+l)" in diff
        assert "(binary file added: 123456 bytes)" in diff
        # Content was never fetched -- no mojibake inlining.
        assert not any(args[:1] == ["print"] for args in calls)
        assert "@@ -0,0" not in diff

    def test_binary_delete_in_differences_gets_placeholder(self):
        describe = (
            "Affected files ...\n"
            "... //depot/old.bin#5 delete\n"
            "Differences ...\n"
            "\n"
            "==== //depot/old.bin#5 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args[:1] == ["fstat"] and "fileSize" in args:
                # Size of the prior rev (#4).
                assert args[-1] == "//depot/old.bin#4"
                return (0, "... fileSize 2048\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="100", is_shelved=False)

        assert "(binary file deleted: 2048 bytes)" in diff
        assert "@@ -1," not in diff

    def test_binary_add_size_unavailable_says_size_unknown(self):
        describe = (
            "Shelved files ...\n"
            "... //depot/x.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/x.uasset#1 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "fstat failed")):
            diff = pr.extract_diff(describe, actions, cl="7", is_shelved=True)
        assert "(binary file added: size unknown)" in diff

    def test_binary_add_omitted_from_differences_uses_fstat_type(self):
        """Files with no ==== header have no inline type; fstat supplies it."""
        describe = (
            "Shelved files ...\n"
            "... //depot/edit.cpp#3 edit\n"
            "... //depot/asset.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/edit.cpp#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)
        calls: list[list[str]] = []

        def fake_run_p4(args):
            calls.append(args)
            if args[:1] == ["fstat"] and "type,headType" in args:
                return (0, "... type binary+l\n", "")
            if args[:1] == ["fstat"] and "fileSize" in args:
                return (0, "... fileSize 99\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="50", is_shelved=True)

        # Synthesized header carries the discovered type, not hardcoded (text).
        assert "==== //depot/asset.uasset#1 (binary+l) ====" in diff
        assert "(binary file added: 99 bytes)" in diff
        assert not any(args[:1] == ["print"] for args in calls)
        # The text edit is untouched.
        assert "+new" in diff

    def test_text_add_omitted_from_differences_still_inlined(self):
        """fstat says text -> the historical synthesis path still runs."""
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args[:1] == ["fstat"] and "type,headType" in args:
                return (0, "... type text\n", "")
            if args == ["print", "-q", "//depot/new.py@=5"]:
                return (0, "x = 1\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="5", is_shelved=True)

        assert "==== //depot/new.py#1 (text) ====" in diff
        assert "+x = 1" in diff

    def test_fstat_failure_defaults_to_text_synthesis(self):
        """fstat unavailable -> behave exactly as before the guard existed."""
        describe = (
            "Shelved files ...\n"
            "... //depot/new.py#1 add\n"
            "Differences ...\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)

        def fake_run_p4(args):
            if args == ["print", "-q", "//depot/new.py@=5"]:
                return (0, "y = 2\n", "")
            return (1, "", f"unexpected: {args}")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            diff = pr.extract_diff(describe, actions, cl="5", is_shelved=True)

        assert "==== //depot/new.py#1 (text) ====" in diff
        assert "+y = 2" in diff

    def test_stderr_notes_skipped_binaries(self, capsys):
        describe = (
            "Shelved files ...\n"
            "... //depot/x.uasset#1 add\n"
            "Differences ...\n"
            "\n"
            "==== //depot/x.uasset#1 (binary) ====\n"
            "\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "")):
            pr.extract_diff(describe, actions, cl="7", is_shelved=True)
        err = capsys.readouterr().err
        assert "skipped binary content" in err
        assert "//depot/x.uasset" in err

    def test_binary_edit_with_real_hunks_untouched(self):
        """The guard only applies to add/delete synthesis; sections that
        already carry @@ hunks pass through regardless of type."""
        describe = (
            "Affected files ...\n"
            "... //depot/a.bin#2 edit\n"
            "Differences ...\n"
            "\n"
            "==== //depot/a.bin#2 (binary) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )
        actions = pr.parse_file_actions(describe)
        with patch.object(pr, "run_p4", return_value=(1, "", "")):
            diff = pr.extract_diff(describe, actions, cl="1", is_shelved=False)
        assert "+new" in diff
        assert "binary file" not in diff


class TestFetchFiletypeAndSize:
    def test_fetch_filetype_prefers_type_over_headtype(self):
        out = "... headType text\n... type binary\n"
        with patch.object(pr, "run_p4", return_value=(0, out, "")) as mock:
            ft = pr.fetch_filetype("//d/x", "1", "9", is_shelved=True, is_delete=False)
        assert ft == "binary"
        assert mock.call_args[0][0] == ["fstat", "-T", "type,headType", "//d/x@=9"]

    def test_fetch_filetype_falls_back_to_headtype(self):
        out = "... headType binary+l\n"
        with patch.object(pr, "run_p4", return_value=(0, out, "")):
            ft = pr.fetch_filetype("//d/x", "3", "9", is_shelved=False, is_delete=False)
        assert ft == "binary+l"

    def test_fetch_filetype_returns_none_on_failure(self):
        with patch.object(pr, "run_p4", return_value=(1, "", "err")):
            assert pr.fetch_filetype("//d/x", "1", "9", True, False) is None

    def test_fetch_file_size_parses_filesize(self):
        with patch.object(pr, "run_p4", return_value=(0, "... fileSize 777\n", "")) as mock:
            size = pr.fetch_file_size("//d/x", "2", "9", is_shelved=False, is_delete=False)
        assert size == 777
        assert mock.call_args[0][0] == ["fstat", "-Ol", "-T", "fileSize", "//d/x#2"]

    def test_fetch_file_size_none_when_missing(self):
        with patch.object(pr, "run_p4", return_value=(0, "... depotFile //d/x\n", "")):
            assert pr.fetch_file_size("//d/x", "2", "9", False, False) is None

    def test_delete_at_rev_1_has_no_spec(self):
        with patch.object(pr, "run_p4") as mock:
            assert pr.fetch_filetype("//d/x", "1", "9", False, True) is None
            assert pr.fetch_file_size("//d/x", "1", "9", False, True) is None
        assert mock.call_count == 0


# ---------------------------------------------------------------------------
# --claim: arg parsing, pre-image materialization, build_bundle exclusion
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_cl_only(self):
        assert pr._parse_args(["12345"]) == (["12345"], [])

    def test_claim_flags_collected(self):
        pos, claims = pr._parse_args(["12345", "--claim", "**/CLAUDE.md", "--claim", "**/SKILL.md"])
        assert pos == ["12345"]
        assert claims == ["**/CLAUDE.md", "**/SKILL.md"]

    def test_claim_equals_form(self):
        assert pr._parse_args(["12345", "--claim=**/SKILL.md"]) == (["12345"], ["**/SKILL.md"])

    def test_claim_without_value_raises(self):
        with pytest.raises(ValueError):
            pr._parse_args(["12345", "--claim"])


class TestMaterializePreimageP4:
    def test_add_action_yields_none_without_p4_call(self, tmp_path):
        with patch.object(pr, "run_p4") as mock:
            assert pr.materialize_preimage("//depot/x/CLAUDE.md", "add", tmp_path) is None
        assert mock.call_count == 0

    def test_edit_prints_have_revision(self, tmp_path):
        captured = {}

        def fake_run_p4(args):
            captured["args"] = args
            # p4 print -q -o <dest> <spec>: write the file it was told to.
            Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[3]).write_text("have content\n", encoding="utf-8")
            return (0, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            dest = pr.materialize_preimage("//depot/x/CLAUDE.md", "edit", tmp_path)

        assert dest is not None
        assert captured["args"][:3] == ["print", "-q", "-o"]
        assert captured["args"][4] == "//depot/x/CLAUDE.md#have"
        assert Path(dest).read_text(encoding="utf-8") == "have content\n"

    def test_print_failure_yields_none(self, tmp_path):
        with patch.object(pr, "run_p4", return_value=(1, "", "no such file")):
            assert pr.materialize_preimage("//depot/x/CLAUDE.md", "edit", tmp_path) is None


class TestBuildBundleClaims:
    def _describe(self):
        return (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n"
            "\tEdit rules and code\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/CLAUDE.md#3 edit\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/CLAUDE.md#3 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old rule\n"
            "+new rule\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-int x = 0;\n"
            "+int x = 1;\n"
        )

    def test_claimed_claude_md_excluded_and_preimage_materialized(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("workspace rule\n", encoding="utf-8")
        claude = src / "CLAUDE.md"
        claude.write_text("new rule\n", encoding="utf-8")
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")

        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
            "\n"
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            if args[:3] == ["print", "-q", "-o"]:
                Path(args[3]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[3]).write_text("old rule\n", encoding="utf-8")
                return (0, "", "")
            return (1, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle(
                "999", tmp_path / "bundle", claim_globs=["**/CLAUDE.md", "**/SKILL.md"]
            )

        # foo.cpp reviewed generically; CLAUDE.md claimed.
        assert [f["depot"] for f in bundle["changed_files"]] == ["//depot/src/foo.cpp"]
        assert len(bundle["claimed_files"]) == 1
        claimed = bundle["claimed_files"][0]
        assert claimed["depot"] == "//depot/src/CLAUDE.md"
        assert claimed["action"] == "edit"
        assert Path(claimed["pre_image"]).read_text(encoding="utf-8") == "old rule\n"
        assert claimed["claude_mds"]  # nearest-first, includes self
        # Claimed file's diff excluded from chunks; generic file present.
        diff = _concat_diff_from_chunks(bundle)
        assert "//depot/src/foo.cpp" in diff
        assert "//depot/src/CLAUDE.md" not in diff
        # Claimed file still contributes to the ruleset collection.
        assert any("CLAUDE.md" in c for c in bundle["unique_claude_mds"])

    def test_no_claim_flag_has_no_claimed_files_key(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new rule\n", encoding="utf-8")
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
            "\n"
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            return (1, "", "")

        with patch.object(pr, "run_p4", side_effect=fake_run_p4):
            bundle = pr.build_bundle("999", tmp_path / "bundle")

        assert "claimed_files" not in bundle
        assert len(bundle["changed_files"]) == 2


# ---------------------------------------------------------------------------
# Submitted-CL guard: --claim requires a pending CL (#have would be POST-change)
# ---------------------------------------------------------------------------


class TestSubmittedClaimGuard:
    def _submitted_describe(self):
        # NO *pending* marker -> a submitted CL.
        return (
            "Change 555 by user@client on 2026/01/01 12:00:00\n"
            "\n"
            "\tEdit rules\n"
            "\n"
            "Affected files ...\n"
            "... //depot/src/CLAUDE.md#4 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/CLAUDE.md#4 (text) ====\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        )

    def _fake(self, ws, claude):
        where_out = (
            "... depotFile //depot/src/CLAUDE.md\n"
            f"... path {claude}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, self._submitted_describe(), "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            return (1, "", "")

        return fake_run_p4

    def test_submitted_cl_with_claim_raises(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new\n", encoding="utf-8")
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, claude)):
            with pytest.raises(ValueError, match="submitted"):
                pr.build_bundle(
                    "555", tmp_path / "bundle", claim_globs=["**/CLAUDE.md"]
                )

    def test_submitted_cl_without_claim_ok(self, tmp_path):
        # Without --claim, a submitted CL is still reviewable (informational).
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        claude = src / "CLAUDE.md"
        claude.write_text("new\n", encoding="utf-8")
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, claude)):
            bundle = pr.build_bundle("555", tmp_path / "bundle")
        assert bundle["cl"] == "555"
        assert "claimed_files" not in bundle


# ---------------------------------------------------------------------------
# Ledger wiring in the bundle
# ---------------------------------------------------------------------------


class TestBundleLedgerWiring:
    def _fake(self, ws, foo):
        describe = (
            "Change 999 by user@client on 2026/01/01 12:00:00 *pending*\n"
            "\n\tEdit foo\n\n"
            "Affected files ...\n"
            "... //depot/src/foo.cpp#1 edit\n"
            "\n"
            "Differences ...\n"
            "\n"
            "==== //depot/src/foo.cpp#1 (text) ====\n"
            "@@ -1 +1 @@\n-int x = 0;\n+int x = 1;\n"
        )
        where_out = (
            "... depotFile //depot/src/foo.cpp\n"
            f"... path {foo}\n"
        )
        info_out = f"... clientRoot {ws}\n"

        def fake_run_p4(args):
            if args[:2] == ["describe", "-du"]:
                return (0, describe, "")
            if args[:2] == ["-ztag", "where"]:
                return (0, where_out, "")
            if args[:2] == ["-ztag", "info"]:
                return (0, info_out, "")
            if args[:3] == ["-ztag", "reconcile", "-n"]:
                return (1, "", "no file(s) to reconcile.\n")
            if args[:4] == ["-ztag", "resolve", "-n", "-c"]:
                return (1, "", "no file(s) to resolve.\n")
            return (1, "", "")

        return fake_run_p4

    def test_bundle_carries_ledger_fields(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        led = tmp_path / "ledger.json"
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            bundle = pr.build_bundle("999", tmp_path / "bundle", ledger_path=led)
        assert bundle["change_id"] == "999"
        assert isinstance(bundle["ledger_baseline"], str) and bundle["ledger_baseline"]
        assert bundle["ledger_hits"] == []

    def test_recorded_finding_flows_back_as_hit(self, tmp_path):
        ws = tmp_path / "ws"
        src = ws / "src"
        src.mkdir(parents=True)
        foo = src / "foo.cpp"
        foo.write_text("int x = 1;\n", encoding="utf-8")
        led = tmp_path / "ledger.json"
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            first = pr.build_bundle("999", tmp_path / "b1", ledger_path=led)
        # Record a declined finding at the baseline the first run computed.
        pr.ledger.record_declined(
            led, first["change_id"], first["ledger_baseline"],
            [{"kind": "code_review", "file": "src/foo.cpp", "reason": "bug",
              "description": "off by one in loop"}],
        )
        with patch.object(pr, "run_p4", side_effect=self._fake(ws, foo)):
            second = pr.build_bundle("999", tmp_path / "b2", ledger_path=led)
        assert len(second["ledger_hits"]) == 1
        assert second["ledger_hits"][0]["label"] == "off by one in loop"
