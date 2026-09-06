"""Tests for find_transcript - specifically the resume session bug.

When Claude Code resumes a session, it creates a new transcript containing
only `file-history-snapshot` entries (no assistant usage data). The script
must skip these empty transcripts and return the most recent one with actual
usage data.
"""

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "plugins" / "cache-kit" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from cache_report import find_transcript


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def make_assistant_entry(timestamp: str = "2026-02-18T20:00:00.000Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "model": "claude-sonnet-4-5-20250929",
            "usage": {
                "input_tokens": 5,
                "output_tokens": 10,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 500,
            },
        },
    }


def make_snapshot_entry() -> dict:
    return {
        "type": "file-history-snapshot",
        "messageId": "abc123",
        "snapshot": {"messageId": "abc123", "trackedFileBackups": {}, "timestamp": "2026-02-18T20:30:00.000Z"},
        "isSnapshotUpdate": False,
    }


class TestFindTranscriptSkipsResumeTranscripts:
    def test_should_skip_resume_only_transcript_and_return_session_with_usage(self, tmp_path):
        """When the most recent transcript has only file-history-snapshot entries,
        find_transcript should skip it and return the next most recent that has usage data."""
        # Arrange: real session with usage data (older mtime)
        real_session = tmp_path / "356fe20e-aaaa-bbbb-cccc-000000000001.jsonl"
        write_jsonl(real_session, [make_assistant_entry()])

        # Resume transcript with no usage data (newer mtime)
        resume_session = tmp_path / "4401e02c-aaaa-bbbb-cccc-000000000002.jsonl"
        write_jsonl(resume_session, [make_snapshot_entry(), make_snapshot_entry()])

        # Ensure resume has a newer mtime
        resume_session.touch()

        # Act
        result = find_transcript(None, tmp_path)

        # Assert: should return the real session, not the resume transcript
        assert result == real_session

    def test_should_return_most_recent_when_all_have_usage_data(self, tmp_path):
        """When multiple transcripts all have usage data, return the most recent."""
        older = tmp_path / "session-older.jsonl"
        write_jsonl(older, [make_assistant_entry()])

        newer = tmp_path / "session-newer.jsonl"
        write_jsonl(newer, [make_assistant_entry()])
        newer.touch()

        result = find_transcript(None, tmp_path)

        assert result == newer

    def test_should_return_none_when_all_transcripts_are_resume_only(self, tmp_path):
        """When all transcripts have no usage data, return None."""
        resume = tmp_path / "resume-only.jsonl"
        write_jsonl(resume, [make_snapshot_entry()])

        result = find_transcript(None, tmp_path)

        assert result is None

    def test_should_return_none_when_project_dir_missing(self, tmp_path):
        result = find_transcript(None, tmp_path / "nonexistent")
        assert result is None
