"""Tests for engine display-marker filtering (`_read_new_log_entries`).

These tests guard against the regression where a missing or stale
`last_displayed_at` marker caused the engine to dump the entire historical
bootstrap log to the user as a single 40+ KB systemMessage.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from bootstrap_lib.engine import (
    _read_new_log_entries,
    _user_visible_log,
    emit_success_response,
)
from bootstrap_lib.log import LOG_FILENAME


def _write_log(data_dir, content):
    with open(os.path.join(data_dir, LOG_FILENAME), "w") as f:
        f.write(content)


def _write_marker(data_dir, ts):
    with open(os.path.join(data_dir, "last_displayed_at"), "w") as f:
        f.write(ts)


class TestReadNewLogEntries:
    def test_missing_marker_does_not_dump_history(self, data_dir):
        """A missing marker must NOT cause the entire log to be returned."""
        old_log = (
            "--- bootstrap@0.8.0 2026-03-09T23:19:17Z ---\n"
            "config: node: not found, attempting install\n"
            "config: node: FAILED - install attempted but still not found\n"
            "--- bootstrap@0.8.0 done in 2.0s ---\n"
            "--- bootstrap@0.8.0 2026-03-10T00:00:00Z ---\n"
            "config: node: not found, attempting install\n"
            "config: node: FAILED - install attempted but still not found\n"
            "--- bootstrap@0.8.0 done in 2.0s ---\n"
        )
        _write_log(data_dir, old_log)
        # Engine "now" is far in the future relative to the log entries.
        now = datetime(2026, 4, 7, 15, 30, 0, tzinfo=timezone.utc)
        out = _read_new_log_entries(data_dir, start_time=now)
        assert out == ""

    def test_stale_marker_does_not_dump_history(self, data_dir):
        """A marker far older than the floor must NOT re-include history."""
        old_log = (
            "--- bootstrap@0.8.0 2026-03-09T23:19:17Z ---\n"
            "config: node: FAILED - install attempted but still not found\n"
            "--- bootstrap@0.8.0 done in 2.0s ---\n"
        )
        _write_log(data_dir, old_log)
        _write_marker(data_dir, "2026-03-08T00:00:00Z")
        now = datetime(2026, 4, 7, 15, 30, 0, tzinfo=timezone.utc)
        out = _read_new_log_entries(data_dir, start_time=now)
        assert out == ""

    def test_current_run_shell_block_included(self, data_dir):
        """Shell entries written within the current run window must appear."""
        now = datetime(2026, 4, 7, 15, 30, 0, tzinfo=timezone.utc)
        # Shell block written ~5s before engine start — within the 120s floor.
        shell_ts = (now - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        log = (
            "--- bootstrap@0.8.0 2026-03-09T23:19:17Z ---\n"
            "ancient: stale entry\n"
            "--- bootstrap@0.8.0 done in 2.0s ---\n"
            f"--- Shell {shell_ts} ---\n"
            "PATH: added foo\n"
        )
        _write_log(data_dir, log)
        out = _read_new_log_entries(data_dir, start_time=now)
        assert "ancient" not in out
        assert "PATH: added foo" in out
        assert f"--- Shell {shell_ts} ---" in out

    def test_recent_marker_filters_normally(self, data_dir):
        """When marker is recent, only entries newer than marker are returned."""
        now = datetime(2026, 4, 7, 15, 30, 0, tzinfo=timezone.utc)
        log = (
            "--- bootstrap@0.8.14 2026-04-07T15:00:00Z ---\n"
            "old: already shown\n"
            "--- bootstrap@0.8.14 done in 1.0s ---\n"
            "--- Shell 2026-04-07T15:29:55Z ---\n"
            "fresh: new entry\n"
        )
        _write_log(data_dir, log)
        _write_marker(data_dir, "2026-04-07T15:00:00Z")
        out = _read_new_log_entries(data_dir, start_time=now)
        assert "old: already shown" not in out
        assert "fresh: new entry" in out

    def test_no_log_file(self, data_dir):
        out = _read_new_log_entries(data_dir, start_time=datetime.now(timezone.utc))
        assert out == ""

    def test_untimestamped_header_does_not_leak_block(self, data_dir):
        """A malformed header without a timestamp must not include its block."""
        now = datetime(2026, 4, 7, 15, 30, 0, tzinfo=timezone.utc)
        log = (
            "--- bootstrap@0.8.14 ---\n"
            "leaked: should not appear\n"
        )
        _write_log(data_dir, log)
        out = _read_new_log_entries(data_dir, start_time=now)
        assert out == ""

    def test_internal_scheduling_blocks_stay_log_only(self, data_dir):
        """Always, harvest, and lock outcomes stay in the log, not display."""
        now = datetime(2026, 9, 8, 16, 48, 0, tzinfo=timezone.utc)
        log = (
            "--- bootstrap always 2026-09-08T16:47:40Z ---\n"
            "env_check repo-sync: fixed - all repos in sync\n"
            "--- bootstrap harvest 2026-09-08T16:47:41Z ---\n"
            "registry-change: relaunched bootstrap pass\n"
            "--- bootstrap harvest 2026-09-08T16:47:42Z ---\n"
            "registry-change: relaunched bootstrap pass\n"
            "--- bootstrap lock 2026-09-08T16:47:43Z ---\n"
            "stand-down: engine 0.96.12 yielded to running engine pass\n"
            "--- bootstrap elevation 2026-09-08T16:47:44Z ---\n"
            "fix runner completed successfully\n"
        )
        _write_log(data_dir, log)

        out = _read_new_log_entries(data_dir, start_time=now)

        with open(os.path.join(data_dir, LOG_FILENAME)) as log_file:
            persisted = log_file.read()
        assert "repo-sync" in persisted
        assert persisted.count("registry-change") == 2
        assert "stand-down" in persisted
        assert "repo-sync" not in out
        assert "registry-change" not in out
        assert "stand-down" not in out
        assert "fix runner completed successfully" in out


class TestUserVisibleLog:
    """`_user_visible_log` strips log-only scheduling blocks."""

    LOCK = (
        "--- bootstrap lock 2026-08-19T15:19:08Z ---\n"
        "stand-down: engine 0.84.0 yielded to running engine pass (pid 18032)\n"
    )
    REAL = (
        "--- plugins-kit:bootstrap@0.84.0 2026-08-19T15:19:09Z ---\n"
        "env: env_check repo-sync: FAILED\n"
    )

    def test_lock_block_and_its_body_are_dropped(self):
        out = _user_visible_log(self.LOCK + self.REAL)
        assert "bootstrap lock" not in out
        assert "stand-down" not in out
        assert "env_check repo-sync: FAILED" in out

    def test_a_following_block_is_not_swallowed(self):
        # The hidden region must end at the next header, not run to EOF.
        out = _user_visible_log(self.REAL + self.LOCK + self.REAL)
        assert out.count("env_check repo-sync: FAILED") == 2

    def test_untimestamped_caller_channel_report_is_dropped(self):
        # _stand_down also reports inline on the caller's channel, with no
        # timestamp in the header.
        out = _user_visible_log(
            "--- bootstrap lock: stand-down: engine 0.84.0 yielded ---")
        assert out == ""

    def test_ordinary_log_is_untouched(self):
        assert _user_visible_log(self.REAL) == self.REAL.rstrip("\n")

    def test_quiet_only_pass_emits_no_system_message(self, data_dir, tmp_path):
        # Nothing left for the user means NO systemMessage at all -- a bare
        # "bootstrap complete:" header with an empty body is worse than silence.
        out = tmp_path / "pending.json"
        emit_success_response(self.LOCK, label="mkt:bootstrap@test",
                              output_file=str(out))
        payload = json.loads(out.read_text())
        assert "systemMessage" not in payload
        ac = payload["hookSpecificOutput"]["additionalContext"]
        assert "stand-down" in ac

    def test_emitted_channels_split_log_only_from_user_content(
        self, tmp_path, capsys
    ):
        """Both transports keep full agent context and filter the user's copy."""
        content = self.LOCK + self.REAL
        out = tmp_path / "pending.json"
        emit_success_response(content, label="mkt:bootstrap@test",
                              output_file=str(out))
        background_payload = json.loads(out.read_text())

        emit_success_response(content, label="mkt:bootstrap@test")
        stdout_payload = json.loads(capsys.readouterr().out)

        for payload in (background_payload, stdout_payload):
            agent_message = payload["hookSpecificOutput"]["additionalContext"]
            user_message = payload["systemMessage"]
            assert "bootstrap lock" in agent_message
            assert "stand-down" in agent_message
            assert "env_check repo-sync: FAILED" in agent_message
            assert "bootstrap lock" not in user_message
            assert "stand-down" not in user_message
            assert "env_check repo-sync: FAILED" in user_message
