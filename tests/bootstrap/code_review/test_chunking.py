"""Tests for bootstrap_lib.code_review.chunking.

Vendor-neutral: synthetic DiffSection inputs, no p4 or git format
assumptions. The p4 and git adapters are tested separately in their
own plugins.
"""

from bootstrap_lib.code_review.chunking import (
    _section_parent,
    partition_sections_into_chunks,
    write_chunks,
)


def _section(identifier: str, body_bytes: int) -> dict:
    """Build a synthetic diff section padded to roughly `body_bytes` bytes.

    Total bytes = header + hunk header + N lines * 10 bytes.
    """
    header = f"<<<file {identifier}>>>\n"
    line = "+xxxxxxxx\n"  # 10 bytes per line
    n = max(1, body_bytes // len(line))
    body = "@@ -0,0 +1,{n} @@\n".format(n=n) + (line * n)
    return {"identifier": identifier, "text": header + body}


class TestSectionParent:
    def test_single_char_top_level_dir(self):
        """G12: `a/file.py` (rfind == 1) must group as "a" -- the old
        `idx > 1` guard returned the whole identifier, so files in a
        one-char top dir each became their own group."""
        assert _section_parent("a/file.py") == "a"

    def test_multi_level_path(self):
        assert _section_parent("src/foo/bar.py") == "src/foo"

    def test_depot_path(self):
        assert _section_parent("//depot/foo/bar.cpp") == "//depot/foo"

    def test_no_slash_is_own_group(self):
        assert _section_parent("file.py") == "file.py"

    def test_leading_slash_only_is_own_group(self):
        assert _section_parent("/file.py") == "/file.py"


class TestPartitionSectionsIntoChunks:
    def test_empty_returns_no_chunks(self):
        assert partition_sections_into_chunks([], max_bytes=1024) == []

    def test_single_small_section_fits_one_chunk(self):
        sections = [_section("//d/a/foo.cpp", 100)]
        chunks = partition_sections_into_chunks(sections, max_bytes=1024 * 1024)
        assert len(chunks) == 1
        assert chunks[0]["files"] == ["//d/a/foo.cpp"]

    def test_balances_when_total_exceeds_max(self):
        """K = ceil(total / max). Two ~600 B sections with max=1000 -> K=2
        chunks of ~600 B each (not 1000 + 200)."""
        sections = [
            _section("//d/a/file1.cpp", 600),
            _section("//d/b/file2.cpp", 600),
        ]
        chunks = partition_sections_into_chunks(sections, max_bytes=1000)
        assert len(chunks) == 2
        assert chunks[0]["files"] == ["//d/a/file1.cpp"]
        assert chunks[1]["files"] == ["//d/b/file2.cpp"]
        assert all(c["bytes"] < 1000 for c in chunks)

    def test_never_splits_a_section_even_when_oversized(self):
        """A section larger than max_bytes lands alone in an oversized chunk."""
        sections = [_section("//d/huge.bin", 2048)]
        chunks = partition_sections_into_chunks(sections, max_bytes=512)
        assert len(chunks) == 1
        assert chunks[0]["bytes"] >= 2048
        assert chunks[0]["files"] == ["//d/huge.bin"]

    def test_prefers_group_boundary_over_balance(self):
        """When the chunk is past target, close at a group transition."""
        sections = [
            _section("//d/dirA/f1.cpp", 300),
            _section("//d/dirA/f2.cpp", 300),
            _section("//d/dirA/f3.cpp", 300),
            _section("//d/dirB/g1.cpp", 300),
            _section("//d/dirB/g2.cpp", 300),
            _section("//d/dirB/g3.cpp", 300),
        ]
        chunks = partition_sections_into_chunks(sections, max_bytes=1500)
        assert len(chunks) == 2
        assert chunks[0]["files"] == [
            "//d/dirA/f1.cpp", "//d/dirA/f2.cpp", "//d/dirA/f3.cpp",
        ]
        assert chunks[1]["files"] == [
            "//d/dirB/g1.cpp", "//d/dirB/g2.cpp", "//d/dirB/g3.cpp",
        ]

    def test_holds_off_close_until_group_transition(self):
        """When target hits mid-group, keep packing until the group ends.

        20 sections in dirA + 10 in dirB, ~131 B each (total ~3930 B).
        max=3000 -> K=2, target=1965. Naive close-at-target would split
        dirA in half. Group-aware logic holds until the dirA -> dirB
        transition, putting all 20 dirA sections in chunk0 (~2620 B,
        still under max).
        """
        sections = [_section(f"//d/dirA/f{i}.cpp", 80) for i in range(20)] + [
            _section(f"//d/dirB/g{i}.cpp", 80) for i in range(10)
        ]
        chunks = partition_sections_into_chunks(sections, max_bytes=3000)
        assert len(chunks) == 2
        assert all("/dirA/" in f for f in chunks[0]["files"])
        assert all("/dirB/" in f for f in chunks[1]["files"])
        assert len(chunks[0]["files"]) == 20
        assert len(chunks[1]["files"]) == 10

    def test_single_char_top_dir_groups_hold_together(self):
        """G12 behavioral companion: same shape as the hold-off test above
        but with one-char top-level dirs (`a/`, `b/`). Under the old
        `idx > 1` guard every section was its own group, so the close
        fired mid-`a/` instead of at the a -> b transition."""
        sections = [_section(f"a/f{i}.cpp", 80) for i in range(20)] + [
            _section(f"b/g{i}.cpp", 80) for i in range(10)
        ]
        chunks = partition_sections_into_chunks(sections, max_bytes=3000)
        assert len(chunks) == 2
        assert all(f.startswith("a/") for f in chunks[0]["files"])
        assert all(f.startswith("b/") for f in chunks[1]["files"])
        assert len(chunks[0]["files"]) == 20
        assert len(chunks[1]["files"]) == 10

    def test_hard_cap_forces_close_even_within_group(self):
        """When the next section would breach max, close even mid-group."""
        sections = [
            _section("//d/dirA/f1.cpp", 700),
            _section("//d/dirA/f2.cpp", 700),
            _section("//d/dirA/f3.cpp", 700),
        ]
        chunks = partition_sections_into_chunks(sections, max_bytes=1000)
        # Each section is 700+ B; two together would breach 1000.
        # So each section gets its own chunk.
        assert len(chunks) == 3
        for c in chunks:
            assert len(c["files"]) == 1

    def test_preamble_attaches_to_first_chunk(self):
        sections = [_section("//d/a.cpp", 100)]
        chunks = partition_sections_into_chunks(
            sections, max_bytes=1024 * 1024, preamble="preamble line\n"
        )
        assert len(chunks) == 1
        assert chunks[0]["text"].startswith("preamble line\n")

    def test_zero_sections_returns_no_chunks_without_warning(self, capsys):
        """Excluded-only input has no reviewable diff and needs no placeholder."""
        preamble = "diff --cc f.txt\nindex a29bdeb,e8a99e0..0000000\n@@@ -1,1 -1,1 +1,1 @@@\n"
        chunks = partition_sections_into_chunks([], max_bytes=1024, preamble=preamble)
        assert chunks == []
        assert capsys.readouterr().err == ""

    def test_preamble_is_checked_against_cap_before_first_section(self, capsys):
        preamble = "p" * 60
        sections = [
            {"identifier": "a.txt", "text": "a" * 60},
            {"identifier": "b.txt", "text": "b" * 60},
        ]
        chunks = partition_sections_into_chunks(
            sections, max_bytes=100, preamble=preamble
        )
        assert all(chunk["bytes"] <= 100 for chunk in chunks)
        assert [chunk["files"] for chunk in chunks] == [[], ["a.txt"], ["b.txt"]]
        assert capsys.readouterr().err == ""

    def test_empty_sections_and_empty_preamble_returns_empty(self):
        """A genuinely empty diff (no sections, no preamble) still yields no
        chunks -- the preamble-only path applies only when there IS text."""
        assert partition_sections_into_chunks([], max_bytes=1024, preamble="") == []

    def test_custom_group_by_callable(self):
        """Caller can override the natural-boundary group key."""
        sections = [
            _section("zone1:fileA", 600),
            _section("zone1:fileB", 600),
            _section("zone2:fileC", 600),
        ]
        # Group by everything before ':'.
        chunks = partition_sections_into_chunks(
            sections, max_bytes=1500, group_by=lambda i: i.split(":")[0]
        )
        # zone1 stays together; zone2 lands in chunk2.
        assert len(chunks) == 2
        assert chunks[0]["files"] == ["zone1:fileA", "zone1:fileB"]
        assert chunks[1]["files"] == ["zone2:fileC"]


class TestWriteChunks:
    def test_writes_files_and_returns_index(self, tmp_path):
        chunks = [
            {"text": "chunk0 text\n", "files": ["//d/a.cpp"], "bytes": 12},
            {"text": "chunk1 text\n", "files": ["//d/b.cpp", "//d/c.cpp"], "bytes": 12},
        ]
        index = write_chunks(chunks, tmp_path)
        assert (tmp_path / "chunks" / "chunk-000.diff").read_text() == "chunk0 text\n"
        assert (tmp_path / "chunks" / "chunk-001.diff").read_text() == "chunk1 text\n"
        assert index == [
            {"index": 0, "path": "chunks/chunk-000.diff", "files": ["//d/a.cpp"], "bytes": 12},
            {"index": 1, "path": "chunks/chunk-001.diff", "files": ["//d/b.cpp", "//d/c.cpp"], "bytes": 12},
        ]

    def test_removes_stale_chunks_from_prior_run(self, tmp_path):
        chunks_dir = tmp_path / "chunks"
        chunks_dir.mkdir()
        (chunks_dir / "chunk-000.diff").write_text("stale 0\n")
        (chunks_dir / "chunk-001.diff").write_text("stale 1\n")
        (chunks_dir / "chunk-099.diff").write_text("stale 99\n")

        new_chunks = [{"text": "fresh\n", "files": ["//d/x.cpp"], "bytes": 6}]
        write_chunks(new_chunks, tmp_path)

        survivors = sorted(p.name for p in chunks_dir.glob("chunk-*.diff"))
        assert survivors == ["chunk-000.diff"]
        assert (chunks_dir / "chunk-000.diff").read_text() == "fresh\n"

    def test_empty_chunks_writes_no_files(self, tmp_path):
        index = write_chunks([], tmp_path)
        assert index == []
        assert (tmp_path / "chunks").is_dir()
        assert list((tmp_path / "chunks").iterdir()) == []
