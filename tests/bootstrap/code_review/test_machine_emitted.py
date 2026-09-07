"""Tests for bootstrap_lib.code_review.machine_emitted -- the shared signature list.

Banner literals are ASSEMBLED from fragments rather than written at the start of
a source line: this repo's pre-commit guard refuses a staged file carrying a
machine-emitted-artifact banner, and it reads the SAME list under test here.
"""

from bootstrap_lib.code_review.machine_emitted import (
    added_head,
    detect_machine_emitted,
    detect_signature,
    detect_signature_bytes,
    local_head,
    local_size,
)

GEN = "Generated"
EDIT = "EDIT"


class TestDetectSignature:
    def test_generated_by_comment_banner(self):
        assert detect_signature("# " + GEN + " by protoc") == "generated-by banner"

    def test_slash_comment_and_case_insensitive(self):
        assert detect_signature("// auto-" + GEN.lower() + " by tool") is not None

    def test_at_generated_marker(self):
        assert detect_signature(" * @" + GEN.lower() + "\n") == "at-generated marker"

    def test_auto_generated_banner(self):
        label = detect_signature("<!-- Auto-" + GEN.lower() + " file -->")
        assert label == "auto-generated banner"

    def test_shouted_do_not_edit_banner(self):
        assert detect_signature("# DO NOT " + EDIT + " -- rebuild instead") == (
            "do-not-edit banner"
        )

    def test_mixed_case_do_not_edit_prose_is_not_a_signature(self):
        """Instructional prose is not a banner -- the shouted form is."""
        assert detect_signature("# Do not edit it in place: it is a default") is None

    def test_prose_about_generated_files_is_not_a_signature(self):
        text = "The stub is auto-" + GEN.lower() + " by the codegen step.\n"
        assert detect_signature(text) is None

    def test_no_match_returns_none(self):
        assert detect_signature("def f():\n    return 1\n") is None

    def test_bytes_variant_agrees_with_text_variant(self):
        text = "# " + GEN + " by protoc\n"
        assert detect_signature_bytes(text.encode("utf-8")) == detect_signature(text)

    def test_bytes_variant_tolerates_non_utf8(self):
        assert detect_signature_bytes(b"\xff\xfe binary garbage") is None


class TestAddedHead:
    def test_only_added_lines_without_the_plus(self):
        section = "<<x>>\n-removed\n context\n+kept\n"
        assert added_head(section) == "kept"

    def test_file_header_lines_are_not_content(self):
        section = "+++ b/x.py\n+real\n"
        assert added_head(section) == "real"

    def test_respects_max_lines(self):
        section = "".join("+line %d\n" % n for n in range(50))
        assert len(added_head(section, max_lines=3).splitlines()) == 3


class TestLocalHead:
    def test_missing_path_is_empty(self, tmp_path):
        assert local_head(str(tmp_path / "nope")) == ""
        assert local_head(None) == ""

    def test_reads_leading_lines(self, tmp_path):
        p = tmp_path / "f.py"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        assert local_head(str(p), max_lines=2) == "a\nb"

    def test_size_of_missing_path_is_none(self, tmp_path):
        assert local_size(str(tmp_path / "nope")) is None
        assert local_size(None) is None


class TestDetectMachineEmitted:
    def test_banner_in_added_content(self):
        section = "@@ -0,0 +1,2 @@\n+# " + GEN + " by tool\n+def f(): ...\n"
        assert detect_machine_emitted(section) == "generated-by banner"

    def test_banner_on_disk_when_the_hunk_is_far_below(self, tmp_path):
        p = tmp_path / "stub.py"
        p.write_text("# " + GEN + " by tool\ndef f(): ...\n", encoding="utf-8")
        assert detect_machine_emitted("<<x>>\n+def zzz(): ...\n", str(p)) is not None

    def test_hand_written_file_is_not_machine_emitted(self, tmp_path):
        p = tmp_path / "src.py"
        p.write_text("def f():\n    return 1\n", encoding="utf-8")
        assert detect_machine_emitted("<<x>>\n+def f(): ...\n", str(p)) is None

    def test_deep_added_banner_in_handwritten_file_stays_reviewable(self, tmp_path):
        p = tmp_path / "src.py"
        p.write_text("def f():\n    return 1\n", encoding="utf-8")
        section = (
            "@@ -500,1 +500,1 @@\n"
            "-old line\n"
            "+# " + GEN + " by tool\n"
        )
        assert detect_machine_emitted(section, str(p)) is None
