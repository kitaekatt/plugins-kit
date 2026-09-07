"""Tests for bootstrap_lib.code_review.triviality.

Covers the pure-mechanical triviality profile -- one passing typo case plus each
disqualifier the design enumerates (keyword flip, link change, heading rename,
6+ lines, YAML touch, unparseable) -- and the mechanical_checks scan (ASCII +
absolute paths over the changed lines only).
"""

from bootstrap_lib.code_review import triviality


def _hunk(header, *lines):
    """Assemble a git-style diff section: a header line, one @@ hunk, its lines."""
    body = "\n".join(lines)
    return f"diff --git a/x.md b/x.md\n{header}\n{body}\n"


# ---------------------------------------------------------------------------
# passing case
# ---------------------------------------------------------------------------


class TestTrivialPass:
    def test_paragraph_typo_is_trivial(self):
        pre = "# Title\n\nSome paragraph with teh typo here.\n"
        diff = _hunk(
            "@@ -3,1 +3,1 @@",
            "-Some paragraph with teh typo here.",
            "+Some paragraph with the typo here.",
        )
        prof = triviality.triviality_profile(diff, pre)
        assert prof == {"trivial": True, "reasons": []}

    def test_no_hunks_is_not_trivial(self):
        # A rename-only or mode-only section has no content to inspect.
        prof = triviality.triviality_profile("diff --git a/x.md b/x.md\n", None)
        assert prof == {"trivial": False, "reasons": ["no_hunks"]}

    def test_empty_diff_fails_closed(self):
        assert triviality.triviality_profile("", None) == {
            "trivial": False,
            "reasons": ["no_diff"],
        }


# ---------------------------------------------------------------------------
# disqualifiers
# ---------------------------------------------------------------------------


class TestDisqualifiers:
    def test_keyword_flip_must_to_must_not(self):
        pre = "You MUST rebuild.\n"
        diff = _hunk("@@ -1,1 +1,1 @@", "-You MUST rebuild.", "+You MUST NOT rebuild.")
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "keyword_changed" in prof["reasons"]

    def test_link_target_change(self):
        pre = "See [docs](old/path.md).\n"
        diff = _hunk(
            "@@ -1,1 +1,1 @@",
            "-See [docs](old/path.md).",
            "+See [docs](new/path.md).",
        )
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "reference_changed" in prof["reasons"]

    def test_heading_rename(self):
        pre = "## Foo\n"
        diff = _hunk("@@ -1,1 +1,1 @@", "-## Foo", "+## Bar")
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "structure_changed" in prof["reasons"]

    def test_six_changed_lines_is_too_large(self):
        # A pure add of 6 plain-text lines: only disqualifier is size.
        diff = _hunk(
            "@@ -0,0 +1,6 @@",
            "+one", "+two", "+three", "+four", "+five", "+six",
        )
        prof = triviality.triviality_profile(diff, None)
        assert prof["trivial"] is False
        assert prof["reasons"] == ["too_large"]

    def test_yaml_frontmatter_touch(self):
        pre = "---\ntitle: x\n---\n\nBody.\n"
        diff = _hunk("@@ -2,1 +2,1 @@", "-title: x", "+title: y")
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "yaml_touched" in prof["reasons"]

    def test_yaml_fenced_block_touch(self):
        pre = "# Doc\n\n```yaml\nkey: 1\n```\n"
        diff = _hunk("@@ -4,1 +4,1 @@", "-key: 1", "+key: 2")
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "yaml_touched" in prof["reasons"]

    def test_list_nesting_change(self):
        pre = "- a\n- b\n"
        diff = _hunk("@@ -2,1 +2,1 @@", "-- b", "+  - b")
        prof = triviality.triviality_profile(diff, pre)
        assert prof["trivial"] is False
        assert "structure_changed" in prof["reasons"]

    def test_unparseable_diff_fails_closed(self):
        # A hunk body line that is neither context/add/remove is unparseable.
        bad = "@@ -1,1 +1,1 @@\n!not a valid body line\n"
        prof = triviality.triviality_profile(bad, "x\n")
        assert prof == {"trivial": False, "reasons": ["unparseable"]}

    def test_non_applying_hunk_fails_closed(self):
        # Context line that doesn't match the pre-image -> reconstruct fails.
        diff = _hunk("@@ -5,1 +5,1 @@", "-nope", "+nope2")
        prof = triviality.triviality_profile(diff, "one\n")
        assert prof["trivial"] is False
        assert "unparseable" in prof["reasons"]

    def test_mismatched_pre_image_fails_closed(self):
        diff = _hunk("@@ -1,2 +1,2 @@", " context from another file", "-old", "+new")
        prof = triviality.triviality_profile(diff, "context from this file\nold\n")
        assert prof == {"trivial": False, "reasons": ["diff_mismatch"]}


# ---------------------------------------------------------------------------
# mechanical_checks
# ---------------------------------------------------------------------------


class TestMechanicalChecks:
    def test_clean_change_passes_both(self):
        diff = _hunk("@@ -1,1 +1,1 @@", "-the cat", "+the dog")
        assert triviality.mechanical_checks(diff) == {
            "ascii_clean": True, "no_abs_paths": True,
        }

    def test_non_ascii_flagged(self):
        diff = _hunk("@@ -1,1 +1,1 @@", "-plain", "+smärt quote")
        assert triviality.mechanical_checks(diff)["ascii_clean"] is False

    def test_windows_abs_path_flagged(self):
        diff = _hunk("@@ -0,0 +1,1 @@", "+see C:/Users/x/file.txt")
        assert triviality.mechanical_checks(diff)["no_abs_paths"] is False

    def test_posix_abs_path_flagged(self):
        diff = _hunk("@@ -0,0 +1,1 @@", "+see /Users/x/file.txt for details")
        assert triviality.mechanical_checks(diff)["no_abs_paths"] is False

    def test_scan_is_over_changed_lines_only(self):
        # An absolute path present only in a CONTEXT line must not trip the scan.
        diff = _hunk(
            "@@ -1,2 +1,2 @@",
            " context with C:/Users/x path",
            "-old body",
            "+new body",
        )
        assert triviality.mechanical_checks(diff)["no_abs_paths"] is True
