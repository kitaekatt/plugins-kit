"""write_chunks tells a reader how to page each chunk without a failed Read.

The Read tool refuses a page over about 25,000 tokens. Each index entry
therefore carries the line count, the longest line, and a page size that keeps
`page_lines * longest_line_bytes` under 100,000 bytes.
"""

from __future__ import annotations

from bootstrap_lib.code_review.chunking import write_chunks


def _entry(tmp_path, text: str) -> dict:
    return write_chunks(
        [{"text": text, "files": ["//d/a"], "bytes": len(text.encode("utf-8"))}], tmp_path
    )[0]


def test_long_lines_shrink_the_page(tmp_path):
    text = ("x" * 299 + "\n") * 3500  # about 1 MB of 300-byte lines
    entry = _entry(tmp_path, text)
    assert entry["lines"] == 3500
    assert entry["longest_line_bytes"] == 299
    assert entry["page_lines"] == 100_000 // 299
    assert entry["page_lines"] * entry["longest_line_bytes"] <= 100_000


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
