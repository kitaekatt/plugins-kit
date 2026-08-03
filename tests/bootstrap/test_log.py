"""Tests for bootstrap lib/log.py."""

import os
import re
from datetime import datetime, timedelta, timezone

from bootstrap_lib.log import LOG_FILENAME, MAX_LOG_LINES, write_log_block


class TestWriteLogBlock:
    def test_creates_log_file(self, data_dir):
        write_log_block(data_dir, "Test", ["test entry"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        assert os.path.exists(log_path)

    def test_writes_header_and_entries(self, data_dir):
        write_log_block(data_dir, "Engine", ["first", "second"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        # No start_time → no footer
        assert len(lines) == 3
        assert lines[0].startswith("--- Engine ")
        assert lines[0].strip().endswith(" ---")
        assert "first" in lines[1]
        assert "second" in lines[2]

    def test_entries_are_plain_text(self, data_dir):
        write_log_block(data_dir, "Engine", ["timestamped"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        # Entry lines are plain text (no timestamp prefix); header has the timestamp
        assert lines[1].strip() == "timestamped"
        assert re.match(r"--- Engine \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ---", lines[0])

    def test_separate_blocks_have_separate_headers(self, data_dir):
        write_log_block(data_dir, "Shell", ["entry one"])
        write_log_block(data_dir, "Engine", ["entry two"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 4
        assert lines[0].startswith("--- Shell ")
        assert "entry one" in lines[1]
        assert lines[2].startswith("--- Engine ")
        assert "entry two" in lines[3]

    def test_empty_entries_noop(self, data_dir):
        write_log_block(data_dir, "Engine", [])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        assert not os.path.exists(log_path)

    def test_trims_at_max_lines(self, data_dir):
        # Write more than MAX_LOG_LINES across multiple blocks
        for i in range(MAX_LOG_LINES + 50):
            write_log_block(data_dir, "Test", [f"entry-{i}"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) <= MAX_LOG_LINES
        # Newest entries should be kept
        assert "entry-" in lines[-1]

    def test_footer_with_start_time(self, data_dir):
        start = datetime.now(timezone.utc) - timedelta(seconds=2.5)
        write_log_block(data_dir, "Engine", ["entry"], start_time=start)
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) == 3  # header + entry + footer
        assert re.match(r"--- Engine done in \d+\.\ds ---", lines[2].strip())

    def test_header_uses_start_time(self, data_dir):
        start = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
        write_log_block(data_dir, "Engine", ["entry"], start_time=start)
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        assert "2025-06-15T12:30:00Z" in lines[0]

    def test_no_footer_without_start_time(self, data_dir):
        write_log_block(data_dir, "Engine", ["entry"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            content = f.read()
        assert "done in" not in content

    def test_unwritable_log_does_not_abort_the_pass(self, data_dir, monkeypatch, capsys):
        """An unwritable log is a lost record, not a lost SessionStart.

        Real case (2026-08-03): a plugin data dir ended up with an empty DACL on
        Windows, and the PermissionError raised here took down the whole engine
        -- every plugin after it went unprovisioned and the user got a traceback
        instead of a bootstrap.
        """
        def _denied(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        # Scoped to the module's global namespace, not builtins -- patching
        # builtins.open breaks pytest's own capture machinery.
        monkeypatch.setattr("bootstrap_lib.log.open", _denied, raising=False)

        write_log_block(data_dir, "Engine", ["entry"])

        assert "could not write" in capsys.readouterr().err


class TestTrimBlockBoundary:
    """B20: trimming must not decapitate a block — the kept tail starts at a header."""

    def test_trim_starts_at_block_header(self, data_dir):
        # Many small blocks, then trigger a trim
        for i in range(0, MAX_LOG_LINES + 60, 3):
            write_log_block(data_dir, f"Block{i}", [f"entry-a-{i}", f"entry-b-{i}"])
        log_path = os.path.join(data_dir, LOG_FILENAME)
        with open(log_path) as f:
            lines = f.readlines()
        assert len(lines) <= MAX_LOG_LINES
        # First kept line is a block header (not an orphaned entry or footer)
        assert lines[0].startswith("--- Block")
        assert " done in " not in lines[0]
