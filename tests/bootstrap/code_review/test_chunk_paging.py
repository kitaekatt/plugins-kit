"""write_chunks tells a reader how to page each chunk without a failed Read.

The Read tool refuses a page over about 25,000 tokens. Each index entry
therefore carries the line count, the longest line, and a page size such that
no `page_lines` consecutive lines, terminators included, exceed 100,000 bytes,
and a page plan (`pages`, [offset, limit] pairs) tiles the chunk within that cap.
"""

from __future__ import annotations

import random

from bootstrap_lib.code_review.chunking import (
    READ_PAGE_MAX_BYTES,
    READ_PAGE_MAX_LINES,
    chunk_read_geometry,
    write_chunks,
)


def _entry(tmp_path, text: str) -> dict:
    return write_chunks(
        [{"text": text, "files": ["//d/a"], "bytes": len(text.encode("utf-8"))}], tmp_path
    )[0]


def test_long_lines_shrink_the_page(tmp_path):
    text = ("x" * 299 + "\n") * 3500  # about 1 MB of 300-byte lines
    entry = _entry(tmp_path, text)
    assert entry["lines"] == 3500
    assert entry["longest_line_bytes"] == 299
    # Each line is 299 bytes plus its terminator as written: CRLF on Windows, where write_text translates.
    line_bytes = len((tmp_path / entry["path"]).read_bytes()) // 3500
    assert entry["page_lines"] == 100_000 // line_bytes
    assert entry["page_lines"] * line_bytes <= 100_000


def test_one_long_line_among_short_lines_keeps_a_large_page(tmp_path):
    text = "x" * 10_000 + "\n" + ("y" * 39 + "\n") * 3000
    entry = _entry(tmp_path, text)
    assert entry["longest_line_bytes"] == 10_000
    assert entry["page_lines"] >= 400  # the longest-line rule answered 10


def test_every_window_fits_the_byte_cap():
    rng = random.Random(7)
    for _ in range(40):
        widths = [rng.choice((5, 80, 400, 6000, 30_000)) for _ in range(rng.randint(1, 600))]
        data = b"".join(b"z" * w + b"\n" for w in widths)
        _lines, _longest, page = chunk_read_geometry(data)
        sizes = [w + 1 for w in widths]
        n = min(page, len(sizes))
        assert all(sum(sizes[i:i + n]) <= READ_PAGE_MAX_BYTES for i in range(len(sizes) - n + 1))
        if page < 800 and page < len(sizes):
            # One more line would overflow somewhere: the page is the largest that fits.
            assert any(sum(sizes[i:i + page + 1]) > READ_PAGE_MAX_BYTES for i in range(len(sizes) - page))


def test_short_lines_cap_at_800(tmp_path):
    entry = _entry(tmp_path, "ab\n" * 5000)
    assert entry["lines"] == 5000
    assert entry["longest_line_bytes"] == 2
    assert entry["page_lines"] == 800


def test_final_line_without_newline_counts_and_bytes_are_utf8(tmp_path):
    entry = _entry(tmp_path, "a\nééé")  # last line is 6 bytes, no terminator
    assert entry["lines"] == 2
    assert entry["longest_line_bytes"] == 6


def test_crlf_terminator_is_not_counted_in_line_bytes(tmp_path):
    entry = _entry(tmp_path, "abc\r\nde\r\n")
    assert entry["lines"] == 2
    assert entry["longest_line_bytes"] == 3


def test_single_enormous_line_still_gets_a_page_of_one(tmp_path):
    entry = _entry(tmp_path, "y" * 500_000 + "\n")
    assert entry["page_lines"] == 1


def test_empty_chunk_text(tmp_path):
    entry = _entry(tmp_path, "")
    assert entry["lines"] == 0
    assert entry["page_lines"] == 800


def _assert_plan(pages, widths):
    """Pages tile 1..len(widths) in order, each within the byte and line caps."""
    expect = 1
    for offset, limit in pages:
        assert offset == expect
        assert 1 <= limit <= READ_PAGE_MAX_LINES
        window = widths[offset - 1:offset - 1 + limit]
        assert len(window) == limit
        assert limit == 1 or sum(window) <= READ_PAGE_MAX_BYTES
        expect += limit
    assert expect == len(widths) + 1


def test_skewed_chunk_gets_a_short_page_plan(tmp_path):
    # The Perforce CL 178745 shape: ~283 KB, 728 lines, nine adjacent ~20 KB run records.
    lines = ["a" * 60] * 300 + ["r" * 20_000] * 9 + ["b" * 60] * 419
    entry = _entry(tmp_path, "\n".join(lines) + "\n")
    widths = [len(p) + 1 for p in (tmp_path / entry["path"]).read_bytes().split(b"\n")[:-1]]
    assert entry["lines"] == 728
    assert entry["page_lines"] <= 5  # the uniform size the plan replaces
    _assert_plan(entry["pages"], widths)
    assert len(entry["pages"]) <= 12
    assert len(entry["pages"]) < entry["lines"] // entry["page_lines"] // 10


def test_pages_stay_near_the_byte_floor_for_wide_lines(tmp_path):
    widths_text = ["w" * 30_000] * 6 + ["s" * 40] * 500 + ["w" * 30_000] * 2
    entry = _entry(tmp_path, "\n".join(widths_text) + "\n")
    widths = [len(p) + 1 for p in (tmp_path / entry["path"]).read_bytes().split(b"\n")[:-1]]
    _assert_plan(entry["pages"], widths)
    assert len(entry["pages"]) <= -(-sum(widths) // READ_PAGE_MAX_BYTES) * 2


def test_short_lines_page_at_800(tmp_path):
    entry = _entry(tmp_path, "ab\n" * 2000)
    assert entry["pages"] == [[1, 800], [801, 800], [1601, 400]]


def test_line_over_the_cap_gets_its_own_page(tmp_path):
    entry = _entry(tmp_path, "a\nb\n" + "y" * 150_000 + "\nc\n")
    assert entry["pages"] == [[1, 2], [3, 1], [4, 1]]


def test_empty_chunk_has_no_pages(tmp_path):
    assert _entry(tmp_path, "")["pages"] == []
