"""Tests for git-kit scripts/prepare_review.py.

Named *_git.py (not test_prepare_review.py) because hyphenated test dirs
(git-kit, p4-kit) are not importable packages despite their __init__.py, so
pytest loads their test modules under top-level names -- a file matching
tests/p4-kit/test_prepare_review.py's basename collides when the full suite
runs in one process.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import git_kit_prepare_review as pr


def _concat_diff_from_chunks(bundle: dict) -> str:
    """Read all chunk files for a bundle and concatenate."""
    bundle_dir = Path(bundle["bundle_dir"])
    return "".join(
        (bundle_dir / entry["path"]).read_text(encoding="utf-8")
        for entry in bundle["diff_chunks"]
    )


# Captured from real `git diff --cached` output with core.quotepath at its
# default (true): a spaced path comes through unquoted, a non-ASCII path
# comes C-quoted with octal escapes. The trailing tab on the spaced `+++`
# line is git's own output, not an accident.
SPACED_AND_QUOTED_DIFF = (
    "diff --git a/has space.txt b/has space.txt\n"
    "new file mode 100644\n"
    "index 0000000..971280a\n"
    "--- /dev/null\n"
    "+++ b/has space.txt\t\n"
    "@@ -0,0 +1 @@\n"
    "+spaced content\n"
    "diff --git a/plain.txt b/plain.txt\n"
    "new file mode 100644\n"
    "index 0000000..b9bca01\n"
    "--- /dev/null\n"
    "+++ b/plain.txt\n"
    "@@ -0,0 +1 @@\n"
    "+plain\n"
    'diff --git "a/r\\303\\251sum\\303\\251.txt" "b/r\\303\\251sum\\303\\251.txt"\n'
    "new file mode 100644\n"
    "index 0000000..87f1863\n"
    "--- /dev/null\n"
    '+++ "b/r\\303\\251sum\\303\\251.txt"\n'
    "@@ -0,0 +1 @@\n"
    "+café line\n"
)


# ---------------------------------------------------------------------------
# run_git -- subprocess invocation
# ---------------------------------------------------------------------------


class TestRunGit:
    def test_forces_utf8_decoding(self):
        """Non-Latin-1 file content (CJK, emoji) in diffs would abort the
        subprocess reader on Windows under cp1252; encoding must be pinned."""
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_git(["status"])

        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"
        assert captured.get("capture_output") is True

    def test_coalesces_none_output_to_empty_strings(self):
        fake = subprocess.CompletedProcess(["git"], 0, stdout=None, stderr=None)
        with patch.object(subprocess, "run", return_value=fake):
            rc, out, err = pr.run_git(["status"])
        assert rc == 0
        assert out == ""
        assert err == ""

    def test_passes_cwd_when_given(self, tmp_path):
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(subprocess, "run", side_effect=fake_run):
            pr.run_git(["status"], cwd=tmp_path)
        assert captured.get("cwd") == str(tmp_path)


# ---------------------------------------------------------------------------
# detect_default_range -- G2: auto-detect on main/master without upstream
# ---------------------------------------------------------------------------


class TestDetectDefaultRangeFallback:
    def test_on_main_without_upstream_falls_back_to_origin_main(self, git_repo):
        """G2: on branch `main` with origin/main and no @{upstream}, auto-detect
        must pick origin/main..HEAD (the unpushed commits), not error out.

        The old `fb.endswith(branch)` skip dropped "origin/main" because
        "origin/main".endswith("main") is True.
        """
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        git_repo.commit_file("a.txt", "unpushed\n", "unpushed work")

        range_spec, reason = pr.detect_default_range()

        assert range_spec == "origin/main..HEAD"
        assert "origin/main" in reason

    def test_on_master_without_upstream_falls_back_to_origin_master(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("branch", "-m", "master")
        git_repo.git("update-ref", "refs/remotes/origin/master", "HEAD")
        git_repo.commit_file("a.txt", "unpushed\n", "unpushed work")

        range_spec, _ = pr.detect_default_range()

        assert range_spec == "origin/master..HEAD"

    def test_on_main_with_no_remote_ref_raises(self, git_repo):
        """Local `main` is the current branch (a no-op diff) and no other
        fallback exists -- auto-detect must error with the explicit-range hint."""
        git_repo.commit_file("a.txt", "base\n", "base")
        with pytest.raises(ValueError, match="explicit range"):
            pr.detect_default_range()

    def test_feature_branch_falls_back_to_origin_main(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("a.txt", "feature work\n", "feature work")

        range_spec, _ = pr.detect_default_range()

        assert range_spec == "origin/main..HEAD"

    def test_upstream_preferred_over_fallbacks(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        # @{upstream} only resolves when the remote itself is configured,
        # not just the remote-tracking ref.
        git_repo.git("remote", "add", "origin", "https://example.invalid/repo.git")
        git_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        git_repo.git("branch", "--set-upstream-to=origin/main", "main")
        git_repo.commit_file("a.txt", "unpushed\n", "unpushed work")

        range_spec, reason = pr.detect_default_range()

        assert range_spec == "refs/remotes/origin/main..HEAD"
        assert "upstream" in reason

    def test_detached_head_falls_back_to_origin_main(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        git_repo.commit_file("a.txt", "more\n", "more")
        git_repo.git("checkout", "-q", "--detach", "HEAD")

        range_spec, reason = pr.detect_default_range()

        assert range_spec == "origin/main..HEAD"
        assert "detached" in reason.lower()

    def test_detached_head_without_fallback_raises(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        # Rename the branch away so no main/master ref exists at all.
        git_repo.git("branch", "-m", "topic")
        git_repo.git("checkout", "-q", "--detach", "HEAD")

        with pytest.raises(ValueError, match="detached"):
            pr.detect_default_range()

    def test_rebase_in_progress_detected(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        # A rebase-merge dir inside .git is the detection signal.
        (git_repo.path / ".git" / "rebase-merge").mkdir()

        range_spec, reason = pr.detect_default_range()

        assert range_spec == "__rebase_in_progress__"
        assert "rebase" in reason.lower()

    def test_merge_takes_priority_over_rebase(self, git_repo):
        git_repo.commit_file("a.txt", "base\n", "base")
        (git_repo.path / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        (git_repo.path / ".git" / "rebase-apply").mkdir()

        range_spec, _ = pr.detect_default_range()

        assert range_spec == "__merge_in_progress__"

    def test_outside_a_repo_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)

        with pytest.raises(ValueError, match="not inside a git repository"):
            pr.detect_default_range()


# ---------------------------------------------------------------------------
# Merge-in-progress review -- G1: flagship path must produce reviewable chunks
# ---------------------------------------------------------------------------


class TestMergeInProgressReview:
    def _make_resolved_merge(self, git_repo, extra_feature_file=False):
        """Drive a real repo into a mid-merge state with the conflict
        resolved and staged (MERGE_HEAD still present)."""
        git_repo.commit_file("f.txt", "line1\n", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("f.txt", "feature-change\n", "feat")
        if extra_feature_file:
            git_repo.commit_file("featonly.txt", "feat new\n", "feat only")
        git_repo.git("checkout", "-q", "main")
        git_repo.commit_file("f.txt", "main-change\n", "main change")
        merge = git_repo.git("merge", "feature", check=False)
        assert merge.returncode != 0, "expected a conflict"
        (git_repo.path / "f.txt").write_text("resolved-content\n", encoding="utf-8")
        git_repo.git("add", "f.txt")
        assert (git_repo.path / ".git" / "MERGE_HEAD").exists()

    def test_detects_merge_sentinel(self, git_repo):
        self._make_resolved_merge(git_repo)
        range_spec, reason = pr.detect_default_range()
        assert range_spec == "__merge_in_progress__"
        assert "merge" in reason.lower()

    def test_all_conflicted_merge_produces_reviewable_chunks(self, git_repo, tmp_path):
        """G1: every changed file is a resolved conflict. With `git diff --cc`
        all output was combined-diff headers the splitter didn't parse, so
        the bundle carried zero chunks and the review was a confident no-op."""
        self._make_resolved_merge(git_repo)

        bundle = pr.build_bundle("__merge_in_progress__", tmp_path / "bundle")

        assert bundle["diff_chunks"], "mid-merge review produced zero diff chunks"
        diff = _concat_diff_from_chunks(bundle)
        assert "+resolved-content" in diff
        by_path = {f["path"]: f for f in bundle["changed_files"]}
        assert by_path["f.txt"]["chunk_index"] is not None

    def test_mixed_merge_includes_resolution_and_incoming_file(self, git_repo, tmp_path):
        """G1: a merge bringing a clean new file plus a resolved conflict must
        show BOTH in the chunks -- not just the clean file."""
        self._make_resolved_merge(git_repo, extra_feature_file=True)

        bundle = pr.build_bundle("__merge_in_progress__", tmp_path / "bundle")

        diff = _concat_diff_from_chunks(bundle)
        assert "+feat new" in diff
        assert "+resolved-content" in diff
        by_path = {f["path"]: f for f in bundle["changed_files"]}
        assert by_path["f.txt"]["chunk_index"] is not None
        assert by_path["featonly.txt"]["chunk_index"] is not None


# ---------------------------------------------------------------------------
# split_git_diff_sections -- G3: spaced and C-quoted non-ASCII paths
# ---------------------------------------------------------------------------


class TestSplitGitDiffSectionsQuoting:
    def test_spaced_and_quoted_paths_split_into_own_sections(self):
        """G3: an unquoted spaced path and a C-quoted non-ASCII path must each
        get their own section with the raw (unquoted) path as identifier --
        not fold into the previous section / preamble."""
        preamble, sections = pr.split_git_diff_sections(SPACED_AND_QUOTED_DIFF)

        assert preamble == ""
        assert [s["path"] for s in sections] == [
            "has space.txt",
            "plain.txt",
            "résumé.txt",
        ]
        assert "+spaced content" in sections[0]["body"]
        assert "+plain" in sections[1]["body"]
        assert "+café line" in sections[2]["body"]


# ---------------------------------------------------------------------------
# fetch_changed_files -- G3: raw paths via -z
# ---------------------------------------------------------------------------


class TestFetchChangedFilesQuoting:
    def test_spaced_and_non_ascii_paths_returned_raw(self, git_repo):
        """G3: `--name-status` without -z C-quotes non-ASCII paths; the parsed
        path must be the raw filename, not the quoted escape string."""
        git_repo.commit_file("base.txt", "a\n", "base")
        (git_repo.path / "has space.txt").write_text("spaced content\n", encoding="utf-8")
        (git_repo.path / "résumé.txt").write_text("café line\n", encoding="utf-8")
        git_repo.git("add", ".")

        files = pr.fetch_changed_files("__staged__")

        assert ("A", "has space.txt") in files
        assert ("A", "résumé.txt") in files


# ---------------------------------------------------------------------------
# _unquote_c_path / _parse_git_header_path -- header parsing units
# ---------------------------------------------------------------------------


class TestUnquoteCPath:
    def test_unquoted_passthrough(self):
        assert pr._unquote_c_path("plain/path.txt") == "plain/path.txt"

    def test_octal_utf8_bytes(self):
        assert pr._unquote_c_path('"r\\303\\251sum\\303\\251.txt"') == "résumé.txt"

    def test_escaped_quote_and_backslash(self):
        assert pr._unquote_c_path('"a\\"b.txt"') == 'a"b.txt'
        assert pr._unquote_c_path('"a\\\\b.txt"') == "a\\b.txt"

    def test_tab_escape(self):
        assert pr._unquote_c_path('"a\\tb.txt"') == "a\tb.txt"


class TestParseGitHeaderPath:
    def test_plain_header(self):
        assert pr._parse_git_header_path("diff --git a/src/foo.py b/src/foo.py") == "src/foo.py"

    def test_spaced_header(self):
        line = "diff --git a/has space.txt b/has space.txt"
        assert pr._parse_git_header_path(line) == "has space.txt"

    def test_quoted_both_sides(self):
        line = 'diff --git "a/r\\303\\251sum\\303\\251.txt" "b/r\\303\\251sum\\303\\251.txt"'
        assert pr._parse_git_header_path(line) == "résumé.txt"

    def test_quoted_b_side_only(self):
        line = 'diff --git a/old.txt "b/caf\\303\\251.txt"'
        assert pr._parse_git_header_path(line) == "café.txt"

    def test_quoted_a_side_unquoted_b_side(self):
        line = 'diff --git "a/caf\\303\\251.txt" b/renamed.txt'
        assert pr._parse_git_header_path(line) == "renamed.txt"

    def test_unquoted_spaced_rename_uses_last_split(self):
        # Captured from real `git mv "has space.txt" "renamed space.txt"`.
        line = "diff --git a/has space.txt b/renamed space.txt"
        assert pr._parse_git_header_path(line) == "renamed space.txt"

    def test_rename_b_side_is_identifier(self):
        line = "diff --git a/old_name.py b/new_name.py"
        assert pr._parse_git_header_path(line) == "new_name.py"

    def test_non_header_lines_return_none(self):
        assert pr._parse_git_header_path("+diff --git a/x b/x") is None
        assert pr._parse_git_header_path("index 0000000..b9bca01") is None
        assert pr._parse_git_header_path("diff --cc f.txt") is None
        assert pr._parse_git_header_path("") is None


# ---------------------------------------------------------------------------
# split_git_diff_sections -- structure
# ---------------------------------------------------------------------------


class TestSplitGitDiffSections:
    TWO_FILE_DIFF = (
        "diff --git a/src/a.py b/src/a.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "index 3333333..4444444 100644\n"
        "--- a/src/b.py\n"
        "+++ b/src/b.py\n"
        "@@ -5 +5 @@\n"
        "-foo\n"
        "+bar\n"
    )

    def test_splits_by_file_header(self):
        preamble, sections = pr.split_git_diff_sections(self.TWO_FILE_DIFF)
        assert preamble == ""
        assert len(sections) == 2
        assert sections[0]["path"] == "src/a.py"
        assert sections[0]["header"].startswith("diff --git a/src/a.py")
        assert "-old\n" in sections[0]["body"]
        assert sections[1]["path"] == "src/b.py"
        assert "+bar\n" in sections[1]["body"]

    def test_preamble_before_first_header(self):
        diff = "some preamble text\n" + self.TWO_FILE_DIFF
        preamble, sections = pr.split_git_diff_sections(diff)
        assert preamble == "some preamble text\n"
        assert len(sections) == 2

    def test_added_line_resembling_header_stays_in_body(self):
        diff = (
            "diff --git a/doc.md b/doc.md\n"
            "@@ -1 +1,2 @@\n"
            " context\n"
            "+diff --git a/fake b/fake\n"
        )
        _, sections = pr.split_git_diff_sections(diff)
        assert len(sections) == 1
        assert "+diff --git a/fake b/fake\n" in sections[0]["body"]

    def test_empty_input(self):
        preamble, sections = pr.split_git_diff_sections("")
        assert preamble == ""
        assert sections == []

    def test_combined_diff_headers_are_preamble_by_contract(self):
        """Captured `git diff --cc HEAD` output. The splitter deliberately
        does NOT parse combined-diff headers -- merge mode fetches a plain
        diff instead (see fetch_diff), and any combined text that reaches
        the chunker anyway is surfaced via its preamble-only-chunk guard
        rather than silently dropped."""
        combined = (
            "diff --cc f.txt\n"
            "index a29bdeb,e8a99e0..0000000\n"
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@@ -1,1 -1,1 +1,5 @@@\n"
            "- line1\n"
            "++<<<<<<< HEAD\n"
            "+ main-change\n"
            "++=======\n"
            "++feature-change\n"
            "++>>>>>>> feature\n"
        )
        preamble, sections = pr.split_git_diff_sections(combined)
        assert sections == []
        assert preamble == combined


# ---------------------------------------------------------------------------
# _git_diff_to_sections -- adapter feeding bootstrap_lib's chunker
# ---------------------------------------------------------------------------


class TestGitDiffToSections:
    def test_passes_preamble_and_path_as_identifier(self):
        diff = "preamble line\n" + TestSplitGitDiffSections.TWO_FILE_DIFF
        preamble, sections = pr._git_diff_to_sections(diff)
        assert preamble == "preamble line\n"
        assert len(sections) == 2
        assert sections[0]["identifier"] == "src/a.py"
        assert sections[0]["text"].startswith("diff --git a/src/a.py")
        assert "-old\n" in sections[0]["text"]
        assert sections[1]["identifier"] == "src/b.py"


# ---------------------------------------------------------------------------
# fetch_diff -- sentinel command routing
# ---------------------------------------------------------------------------


class TestFetchDiff:
    @pytest.mark.parametrize(
        "range_spec,expected_cmd",
        [
            ("__working_tree__", ["diff", "HEAD"]),
            ("__staged__", ["diff", "--cached"]),
            # G1: merge mode must use a plain diff, not `--cc`.
            ("__merge_in_progress__", ["diff", "HEAD"]),
            ("__rebase_in_progress__", ["diff", "HEAD"]),
            ("origin/main..HEAD", ["diff", "origin/main..HEAD"]),
        ],
    )
    def test_command_per_mode(self, range_spec, expected_cmd):
        with patch.object(pr, "run_git", return_value=(0, "out", "")) as mock:
            out = pr.fetch_diff(range_spec)
        assert out == "out"
        assert mock.call_args[0][0] == expected_cmd

    def test_failure_raises_value_error(self):
        with patch.object(pr, "run_git", return_value=(128, "", "fatal: bad ref")):
            with pytest.raises(ValueError, match="git diff failed"):
                pr.fetch_diff("nope..HEAD")


# ---------------------------------------------------------------------------
# fetch_changed_files -- statuses and renames
# ---------------------------------------------------------------------------


class TestFetchChangedFiles:
    def test_add_modify_delete_statuses(self, git_repo):
        git_repo.commit_file("keep.txt", "one\n", "base")
        git_repo.commit_file("gone.txt", "bye\n", "add gone")
        base = git_repo.git("rev-parse", "HEAD").stdout.strip()
        git_repo.commit_file("keep.txt", "two\n", "modify keep")
        (git_repo.path / "gone.txt").unlink()
        git_repo.git("add", "-A")
        git_repo.git("commit", "-qm", "delete gone")
        (git_repo.path / "new.txt").write_text("hi\n", encoding="utf-8")
        git_repo.git("add", "new.txt")
        git_repo.git("commit", "-qm", "add new")

        files = pr.fetch_changed_files(f"{base}..HEAD")

        assert ("M", "keep.txt") in files
        assert ("D", "gone.txt") in files
        assert ("A", "new.txt") in files

    def test_rename_reports_post_rename_path(self, git_repo):
        git_repo.commit_file("old_name.txt", "stable content here\n", "base")
        git_repo.git("mv", "old_name.txt", "new_name.txt")

        files = pr.fetch_changed_files("__staged__")

        assert files == [("R", "new_name.txt")]

    def test_non_ascii_rename_reports_raw_post_rename_path(self, git_repo):
        """G3 companion: a rename whose paths are C-quoted in non-z output."""
        git_repo.commit_file("plain.txt", "stable content here\n", "base")
        git_repo.git("mv", "plain.txt", "résumé.txt")

        files = pr.fetch_changed_files("__staged__")

        assert files == [("R", "résumé.txt")]

    def test_git_failure_returns_empty(self):
        with patch.object(pr, "run_git", return_value=(128, "", "fatal")):
            assert pr.fetch_changed_files("bad..range") == []

    def test_no_changes_returns_empty(self, git_repo):
        git_repo.commit_file("a.txt", "x\n", "base")
        assert pr.fetch_changed_files("__staged__") == []


# ---------------------------------------------------------------------------
# fetch_description
# ---------------------------------------------------------------------------


class TestFetchDescription:
    @pytest.mark.parametrize(
        "range_spec,expected",
        [
            ("__working_tree__", "(uncommitted working-tree changes)"),
            ("__staged__", "(staged-but-uncommitted changes)"),
            ("__merge_in_progress__", "(in-progress merge)"),
            ("__rebase_in_progress__", "(in-progress rebase)"),
        ],
    )
    def test_sentinel_markers(self, range_spec, expected):
        assert pr.fetch_description(range_spec) == expected

    def test_joins_commit_subjects(self, git_repo):
        git_repo.commit_file("a.txt", "1\n", "base")
        base = git_repo.git("rev-parse", "HEAD").stdout.strip()
        git_repo.commit_file("a.txt", "2\n", "first change")
        git_repo.commit_file("a.txt", "3\n", "second change")

        desc = pr.fetch_description(f"{base}..HEAD")

        assert "first change" in desc
        assert "second change" in desc
        assert ";" in desc

    def test_more_than_five_subjects_elided(self):
        out = "\n".join(f"subject {i}" for i in range(6)) + "\n"
        with patch.object(pr, "run_git", return_value=(0, out, "")):
            desc = pr.fetch_description("base..HEAD")
        assert "subject 4" in desc
        assert "subject 5" not in desc
        assert "(+1 more)" in desc

    def test_log_failure_returns_empty(self):
        with patch.object(pr, "run_git", return_value=(128, "", "fatal")):
            assert pr.fetch_description("bad..range") == ""


# ---------------------------------------------------------------------------
# parse_range_arg / _safe_dir_name
# ---------------------------------------------------------------------------


class TestParseRangeArg:
    def test_staged_flag(self):
        assert pr.parse_range_arg("--staged") == "__staged__"

    def test_working_flag(self):
        assert pr.parse_range_arg("--working") == "__working_tree__"

    def test_two_dot_range_passthrough(self):
        assert pr.parse_range_arg("main..feature") == "main..feature"

    def test_three_dot_range_passthrough(self):
        assert pr.parse_range_arg("main...feature") == "main...feature"

    def test_bare_ref_becomes_ref_to_head(self):
        assert pr.parse_range_arg("origin/main") == "origin/main..HEAD"


class TestSafeDirName:
    def test_slashes_replaced_with_disambiguating_suffix(self):
        """G13: a sanitizer-altered spec carries a hash suffix of the
        original so distinct specs cannot share a bundle dir."""
        assert pr._safe_dir_name("origin/main..HEAD") == "origin-main..HEAD-7d83249d"

    def test_sentinels_unchanged(self):
        assert pr._safe_dir_name("__staged__") == "__staged__"

    def test_already_safe_spec_gets_no_suffix(self):
        assert pr._safe_dir_name("main..HEAD") == "main..HEAD"

    def test_unsafe_chars_collapsed_and_trimmed(self):
        assert pr._safe_dir_name("@{upstream}..HEAD") == "upstream-..HEAD-0d4922a4"

    def test_colliding_sanitizations_get_distinct_names(self):
        """G13: `feature/x..HEAD` used to sanitize to the same name as the
        literal branch `feature-x..HEAD`."""
        slashed = pr._safe_dir_name("feature/x..HEAD")
        literal = pr._safe_dir_name("feature-x..HEAD")
        assert literal == "feature-x..HEAD"
        assert slashed != literal
        assert slashed.startswith("feature-x..HEAD-")

    def test_deterministic_across_calls(self):
        assert pr._safe_dir_name("feature/x..HEAD") == pr._safe_dir_name(
            "feature/x..HEAD"
        )

    def test_fully_unsafe_spec_reduces_to_hash_only(self):
        name = pr._safe_dir_name("@{}")
        assert name
        assert not name.startswith("-")


# ---------------------------------------------------------------------------
# find_untracked_or_unstaged
# ---------------------------------------------------------------------------


class TestFindUntrackedOrUnstaged:
    def _repo_with_src(self, git_repo):
        src = git_repo.path / "src"
        src.mkdir()
        (src / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        git_repo.git("add", ".")
        git_repo.git("commit", "-qm", "base")
        return src

    def test_untracked_file_in_touched_dir(self, git_repo):
        src = self._repo_with_src(git_repo)
        (src / "forgot.py").write_text("y = 2\n", encoding="utf-8")

        items = pr.find_untracked_or_unstaged(git_repo.path, [src])

        assert len(items) == 1
        assert items[0]["kind"] == "untracked"
        assert items[0]["path"] == "src/forgot.py"
        assert Path(items[0]["local"]) == (src / "forgot.py").resolve()

    def test_unstaged_modified_and_deleted(self, git_repo):
        src = self._repo_with_src(git_repo)
        (src / "other.py").write_text("z = 3\n", encoding="utf-8")
        git_repo.git("add", ".")
        git_repo.git("commit", "-qm", "add other")
        (src / "tracked.py").write_text("x = 99\n", encoding="utf-8")
        (src / "other.py").unlink()

        items = pr.find_untracked_or_unstaged(git_repo.path, [src])

        kinds = {i["path"]: i["kind"] for i in items}
        assert kinds["src/tracked.py"] == "unstaged_modified"
        assert kinds["src/other.py"] == "unstaged_deleted"

    def test_staged_uncommitted(self, git_repo):
        src = self._repo_with_src(git_repo)
        (src / "staged.py").write_text("s = 4\n", encoding="utf-8")
        git_repo.git("add", "src/staged.py")

        items = pr.find_untracked_or_unstaged(git_repo.path, [src])

        assert len(items) == 1
        assert items[0]["kind"] == "staged_uncommitted"

    def test_files_outside_touched_dirs_excluded(self, git_repo):
        src = self._repo_with_src(git_repo)
        elsewhere = git_repo.path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "noise.py").write_text("n = 5\n", encoding="utf-8")

        items = pr.find_untracked_or_unstaged(git_repo.path, [src])

        assert items == []

    def test_status_failure_returns_empty(self, git_repo, tmp_path):
        with patch.object(pr, "run_git", return_value=(128, "", "fatal")):
            assert pr.find_untracked_or_unstaged(git_repo.path, [tmp_path]) == []


# ---------------------------------------------------------------------------
# find_merge_conflicts
# ---------------------------------------------------------------------------


class TestFindMergeConflicts:
    def test_unresolved_conflict_reported_once(self, git_repo):
        git_repo.commit_file("f.txt", "line1\n", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("f.txt", "feature-change\n", "feat")
        git_repo.git("checkout", "-q", "main")
        git_repo.commit_file("f.txt", "main-change\n", "main change")
        merge = git_repo.git("merge", "feature", check=False)
        assert merge.returncode != 0

        items = pr.find_merge_conflicts(git_repo.path)

        # `git ls-files -u` emits one row per stage; collapsed to one entry.
        assert len(items) == 1
        assert items[0]["path"] == "f.txt"
        assert Path(items[0]["local"]) == (git_repo.path / "f.txt").resolve()

    def test_clean_repo_returns_empty(self, git_repo):
        git_repo.commit_file("f.txt", "x\n", "base")
        assert pr.find_merge_conflicts(git_repo.path) == []


# ---------------------------------------------------------------------------
# build_bundle -- integration
# ---------------------------------------------------------------------------


class TestBuildBundle:
    def test_full_pipeline(self, git_repo, tmp_path):
        (git_repo.path / "CLAUDE.md").write_text("workspace rule\n", encoding="utf-8")
        src = git_repo.path / "src"
        src.mkdir()
        (src / "foo.py").write_text("x = 0\n", encoding="utf-8")
        git_repo.git("add", ".")
        git_repo.git("commit", "-qm", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("src/foo.py", "x = 1\n", "change foo")

        bundle_dir = tmp_path / "bundle"
        bundle = pr.build_bundle("main..HEAD", bundle_dir, auto_reason=None)

        assert bundle["vcs"] == "git"
        assert bundle["range"] == "main..HEAD"
        assert bundle["branch"] == "feature"
        assert bundle["head_sha"]
        assert bundle["description"] == "change foo"
        assert bundle["bundle_dir"] == str(bundle_dir)
        assert "auto_detected_reason" not in bundle
        assert len(bundle["diff_chunks"]) == 1
        diff = _concat_diff_from_chunks(bundle)
        assert "-x = 0" in diff
        assert "+x = 1" in diff
        assert len(bundle["changed_files"]) == 1
        cf = bundle["changed_files"][0]
        assert cf["path"] == "src/foo.py"
        assert cf["status"] == "M"
        assert cf["chunk_index"] == 0
        assert Path(cf["local"]) == (src / "foo.py").resolve()
        assert len(cf["claude_mds"]) == 1
        assert Path(cf["claude_mds"][0]).read_text(encoding="utf-8") == "workspace rule\n"
        assert len(bundle["unique_claude_mds"]) == 1
        assert bundle["untracked_or_unstaged"] == []
        assert bundle["merge_conflicts"] == []
        assert bundle["submit_gates"] == []

    def test_ledger_fields_and_hit_roundtrip(self, git_repo, tmp_path):
        (git_repo.path / "src").mkdir()
        (git_repo.path / "src" / "foo.py").write_text("x = 0\n", encoding="utf-8")
        git_repo.git("add", ".")
        git_repo.git("commit", "-qm", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("src/foo.py", "x = 1\n", "change foo")

        led = tmp_path / "ledger.json"
        first = pr.build_bundle("main..HEAD", tmp_path / "b1", ledger_path=led)
        # change_id is the range; baseline is the range base SHA; no hits yet.
        assert first["change_id"] == "main..HEAD"
        assert first["ledger_baseline"] and len(first["ledger_baseline"]) >= 7
        assert first["ledger_hits"] == []

        pr.ledger.record_declined(
            led, first["change_id"], first["ledger_baseline"],
            [{"kind": "code_review", "file": "src/foo.py", "reason": "bug",
              "description": "constant assignment never used"}],
        )
        second = pr.build_bundle("main..HEAD", tmp_path / "b2", ledger_path=led)
        assert len(second["ledger_hits"]) == 1
        assert second["ledger_hits"][0]["label"] == "constant assignment never used"

    def test_auto_reason_carried_into_bundle(self, git_repo, tmp_path):
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        git_repo.commit_file("a.txt", "unpushed\n", "unpushed work")

        bundle = pr.build_bundle(
            "origin/main..HEAD", tmp_path / "bundle", auto_reason="why not"
        )

        assert bundle["auto_detected_reason"] == "why not"

    def test_untracked_sibling_surfaced(self, git_repo, tmp_path):
        src = git_repo.path / "src"
        src.mkdir()
        (src / "foo.py").write_text("x = 0\n", encoding="utf-8")
        git_repo.git("add", ".")
        git_repo.git("commit", "-qm", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("src/foo.py", "x = 1\n", "change foo")
        (src / "forgot.py").write_text("y = 2\n", encoding="utf-8")

        bundle = pr.build_bundle("main..HEAD", tmp_path / "bundle")

        assert len(bundle["untracked_or_unstaged"]) == 1
        assert bundle["untracked_or_unstaged"][0]["kind"] == "untracked"
        assert bundle["untracked_or_unstaged"][0]["path"] == "src/forgot.py"

    def test_outside_a_repo_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)

        with pytest.raises(ValueError, match="not inside a git repository"):
            pr.build_bundle("main..HEAD", tmp_path / "bundle")


# ---------------------------------------------------------------------------
# main -- argv handling
# ---------------------------------------------------------------------------


class TestMain:
    def test_too_many_args_usage_error(self, capsys):
        rc = pr.main(["prepare_review.py", "a..b", "extra"])
        assert rc == 2
        assert "Usage:" in capsys.readouterr().err

    def test_auto_detect_failure_returns_one(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        monkeypatch.chdir(outside)
        monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "bundles")

        rc = pr.main(["prepare_review.py"])

        assert rc == 1
        assert "Error:" in capsys.readouterr().err

    def test_explicit_range_writes_bundle_and_prints_json(
        self, git_repo, tmp_path, monkeypatch, capsys
    ):
        git_repo.commit_file("a.txt", "base\n", "base")
        git_repo.git("checkout", "-qb", "feature")
        git_repo.commit_file("a.txt", "changed\n", "change a")
        bundle_root = tmp_path / "bundles"
        monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", bundle_root)

        rc = pr.main(["prepare_review.py", "main..HEAD"])

        assert rc == 0
        stdout_bundle = json.loads(capsys.readouterr().out)
        assert stdout_bundle["range"] == "main..HEAD"
        bundle_dir = bundle_root / "main..HEAD"
        on_disk = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))
        assert on_disk == stdout_bundle
        chunk = bundle_dir / stdout_bundle["diff_chunks"][0]["path"]
        assert "+changed" in chunk.read_text(encoding="utf-8")

    def test_staged_flag_routes_to_sentinel(self, git_repo, tmp_path, monkeypatch, capsys):
        git_repo.commit_file("a.txt", "base\n", "base")
        (git_repo.path / "a.txt").write_text("staged change\n", encoding="utf-8")
        git_repo.git("add", "a.txt")
        monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "bundles")

        rc = pr.main(["prepare_review.py", "--staged"])

        assert rc == 0
        bundle = json.loads(capsys.readouterr().out)
        assert bundle["range"] == "__staged__"
        assert bundle["description"] == "(staged-but-uncommitted changes)"


# ---------------------------------------------------------------------------
# --claim: exclusion + claimed_files + pre-image materialization
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_positionals_only(self):
        assert pr._parse_args(["main..HEAD"]) == (["main..HEAD"], [], False)

    def test_claim_flag_collects_globs(self):
        pos, claims, _ = pr._parse_args(
            ["main..HEAD", "--claim", "**/CLAUDE.md", "--claim", "**/SKILL.md"]
        )
        assert pos == ["main..HEAD"]
        assert claims == ["**/CLAUDE.md", "**/SKILL.md"]

    def test_claim_equals_form(self):
        assert pr._parse_args(["--claim=**/CLAUDE.md"]) == ([], ["**/CLAUDE.md"], False)

    def test_sentinel_flags_stay_positional(self):
        assert pr._parse_args(["--staged", "--claim", "**/CLAUDE.md"]) == (
            ["--staged"], ["**/CLAUDE.md"], False
        )

    def test_review_generated_flag(self):
        assert pr._parse_args(["main..HEAD", "--review-generated"]) == (
            ["main..HEAD"], [], True
        )

    def test_claim_without_value_raises(self):
        with pytest.raises(ValueError):
            pr._parse_args(["main..HEAD", "--claim"])


class TestRangeBase:
    def test_double_dot_takes_left(self):
        assert pr._range_base("origin/main..HEAD") == "origin/main"

    def test_sentinels_use_head(self):
        for s in ("__working_tree__", "__staged__", "__merge_in_progress__", "__rebase_in_progress__"):
            assert pr._range_base(s) == "HEAD"


class TestMaterializePreimageGit:
    def test_edit_yields_base_content(self, git_repo, tmp_path):
        git_repo.commit_file("CLAUDE.md", "base rules\n", "base")
        (git_repo.path / "CLAUDE.md").write_text("changed rules\n", encoding="utf-8")
        git_repo.commit_file("CLAUDE.md", "changed rules\n", "change")

        dest = pr.materialize_preimage("HEAD~1..HEAD", "CLAUDE.md", tmp_path / "b")

        assert dest is not None
        assert Path(dest).read_text(encoding="utf-8") == "base rules\n"

    def test_added_file_yields_none(self, git_repo, tmp_path):
        git_repo.commit_file("seed.txt", "x\n", "seed")
        git_repo.commit_file("CLAUDE.md", "new file\n", "add claude")

        # CLAUDE.md did not exist at HEAD~1 -> no pre-image.
        assert pr.materialize_preimage("HEAD~1..HEAD", "CLAUDE.md", tmp_path / "b") is None


class TestBuildBundleClaims:
    def test_claimed_claude_md_held_back_with_preimage(self, git_repo, tmp_path):
        git_repo.commit_file("CLAUDE.md", "base rules\n", "base claude")
        (git_repo.path / "src").mkdir()
        git_repo.commit_file("src/app.py", "print(1)\n", "base app")
        (git_repo.path / "CLAUDE.md").write_text("new rules\n", encoding="utf-8")
        (git_repo.path / "src" / "app.py").write_text("print(2)\n", encoding="utf-8")
        git_repo.git("add", "-A")
        git_repo.git("commit", "-qm", "edit both")

        bundle = pr.build_bundle(
            "HEAD~1..HEAD", tmp_path / "bundle", claim_globs=["**/CLAUDE.md", "**/SKILL.md"]
        )

        # CLAUDE.md is claimed, app.py stays in the generic review.
        assert [f["path"] for f in bundle["changed_files"]] == ["src/app.py"]
        assert len(bundle["claimed_files"]) == 1
        claimed = bundle["claimed_files"][0]
        assert claimed["path"] == "CLAUDE.md"
        assert claimed["status"] == "M"
        assert Path(claimed["pre_image"]).read_text(encoding="utf-8") == "base rules\n"
        assert claimed["claude_mds"]  # nearest-first chain, includes self
        # The claimed file's diff is not in any chunk.
        diff = _concat_diff_from_chunks(bundle)
        assert "app.py" in diff
        assert "new rules" not in diff

    def test_no_claim_flag_leaves_bundle_without_claimed_files(self, git_repo, tmp_path):
        git_repo.commit_file("CLAUDE.md", "base\n", "base")
        git_repo.commit_file("CLAUDE.md", "changed\n", "change")

        bundle = pr.build_bundle("HEAD~1..HEAD", tmp_path / "bundle")

        assert "claimed_files" not in bundle
        assert [f["path"] for f in bundle["changed_files"]] == ["CLAUDE.md"]

    def test_main_with_claim_emits_claimed_files(self, git_repo, tmp_path, monkeypatch, capsys):
        git_repo.commit_file("CLAUDE.md", "base\n", "base")
        git_repo.commit_file("CLAUDE.md", "changed\n", "change")
        monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "bundles")

        rc = pr.main(["prepare_review.py", "HEAD~1..HEAD", "--claim", "**/CLAUDE.md"])

        assert rc == 0
        bundle = json.loads(capsys.readouterr().out)
        assert [c["path"] for c in bundle["claimed_files"]] == ["CLAUDE.md"]
        assert bundle["changed_files"] == []


class TestBuildBundleGenerated:
    # Assembled rather than written literally: a banner at the start of a line
    # in a tracked file is what this repo's own pre-commit guard refuses.
    BANNER = "# " + "Generated by" + " some-codegen -- edits will be lost"

    def test_generated_file_held_back_from_the_reviewers(self, git_repo, tmp_path):
        git_repo.commit_file("seed.txt", "x\n", "seed")
        (git_repo.path / "src").mkdir()
        git_repo.commit_file("src/app.py", "print(2)\n", "app")
        git_repo.commit_file("stub.py", self.BANNER + "\ndef a(): ...\n", "stub")

        bundle = pr.build_bundle("HEAD~2..HEAD", tmp_path / "bundle")

        assert [f["path"] for f in bundle["changed_files"]] == ["src/app.py"]
        entry = bundle["generated_files"][0]
        assert entry["path"] == "stub.py"
        assert entry["generated_axis"] == "content"
        assert entry["generated_signature"] == "generated-by banner"
        assert entry["size_bytes"] == (git_repo.path / "stub.py").stat().st_size
        diff = _concat_diff_from_chunks(bundle)
        assert "app.py" in diff
        assert "stub.py" not in diff

    def test_unbannered_file_at_a_declared_plugin_path_is_held_back(
        self, git_repo, tmp_path
    ):
        """The motivating case: a generated stub carrying no banner at all.

        Content detection is blind to it; the durable plugin-data path it lives
        under is what says a plugin wrote it.
        """
        git_repo.commit_file("seed.txt", "x\n", "seed")
        (git_repo.path / ".plugin-data" / "a-marketplace" / "a-plugin").mkdir(
            parents=True
        )
        git_repo.commit_file(
            ".plugin-data/a-marketplace/a-plugin/api.py",
            "from __future__ import annotations\nimport sys as _sys\n",
            "stub",
        )

        bundle = pr.build_bundle("HEAD~1..HEAD", tmp_path / "bundle")

        assert bundle["changed_files"] == []
        entry = bundle["generated_files"][0]
        assert entry["path"] == ".plugin-data/a-marketplace/a-plugin/api.py"
        assert entry["generated_axis"] == "declared_path"
        assert entry["generated_signature"] == "declared plugin-data path (durable)"

    def test_hand_written_file_keeps_its_full_review(self, git_repo, tmp_path):
        git_repo.commit_file("seed.txt", "x\n", "seed")
        big = "".join("def f_%d(): return %d\n" % (n, n) for n in range(5000))
        git_repo.commit_file("big.py", big, "big hand-written file")

        bundle = pr.build_bundle("HEAD~1..HEAD", tmp_path / "bundle")

        assert "generated_files" not in bundle
        assert [f["path"] for f in bundle["changed_files"]] == ["big.py"]

    def test_review_generated_override(self, git_repo, tmp_path, monkeypatch, capsys):
        git_repo.commit_file("seed.txt", "x\n", "seed")
        git_repo.commit_file("stub.py", self.BANNER + "\ndef a(): ...\n", "stub")
        monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", tmp_path / "bundles")

        rc = pr.main(["prepare_review.py", "HEAD~1..HEAD", "--review-generated"])

        assert rc == 0
        bundle = json.loads(capsys.readouterr().out)
        assert "generated_files" not in bundle
        assert [f["path"] for f in bundle["changed_files"]] == ["stub.py"]
