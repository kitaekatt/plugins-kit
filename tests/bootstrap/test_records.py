"""The pass record (bootstrap_lib/records.py).

The contract under test: the record is COMPLETE and independent of every
display filter, so presentation is free to shorten anything. Rationale in
engine-internals.md ("The pass record").
"""

import json

import pytest

from bootstrap_lib.records import (
    EVENTS_FILENAME, MASK, MAX_DETAIL_CHARS, MAX_EVENTS_BYTES, Entry,
    PassRecorder, RecordingList, entry_list, redact, reprefix,
)


def read_events(data_dir):
    path = data_dir / EVENTS_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def recorder(tmp_path):
    return PassRecorder(str(tmp_path), autoflush=False)


class TestPassRecorder:
    def test_records_are_written_as_json_lines(self, tmp_path, recorder):
        recorder.record("check", "uv: passed", sev="ok")
        recorder.record("check", "venv: created", sev="action")
        recorder.flush()

        events = read_events(tmp_path)
        assert [e["text"] for e in events] == ["uv: passed", "venv: created"]
        assert [e["sev"] for e in events] == ["ok", "action"]

    def test_nothing_is_written_until_flush(self, tmp_path, recorder):
        """Buffered so a pass costs two writes, not hundreds -- and so each
        pass's block lands contiguously under O_APPEND."""
        recorder.record("check", "uv: passed", sev="ok")
        assert read_events(tmp_path) == []

    def test_every_record_carries_the_pass_id_and_a_sequence(self, tmp_path, recorder):
        for i in range(3):
            recorder.record("check", f"entry {i}")
        recorder.flush()

        events = read_events(tmp_path)
        assert [e["seq"] for e in events] == [1, 2, 3]
        assert len({e["pass"] for e in events}) == 1

    def test_long_text_is_never_truncated(self, tmp_path, recorder):
        """The whole point: the record keeps what the display cuts."""
        long = "x" * 5000
        recorder.record("check", long, sev="action")
        recorder.flush()
        assert read_events(tmp_path)[0]["text"] == long

    def test_failure_dicts_are_recorded_verbatim(self, tmp_path, recorder):
        """agent_msg/user_msg reach no message surface for elevation-suppressed
        items; without this they would exist nowhere."""
        failure = {"type": "env_check", "name": "parsec-host",
                   "agent_msg": "tell the user to ...", "user_msg": "needs admin",
                   "elevation": {"method": "command", "command": "winget ..."}}
        recorder.record("failure", "parsec-host", sev="fail", failure=failure)
        recorder.flush()
        assert read_events(tmp_path)[0]["failure"] == failure

    def test_emit_payloads_are_recorded(self, tmp_path, recorder):
        response = {
            "systemMessage": "what the user saw",
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": "what Claude was told"},
        }
        recorder.record_emit("pending", response)
        recorder.flush()

        event = read_events(tmp_path)[0]
        assert event["kind"] == "emit"
        assert event["channel"] == "pending"
        assert event["system_message"] == "what the user saw"
        assert event["additional_context"] == "what Claude was told"

    def test_flush_clears_the_buffer(self, tmp_path, recorder):
        recorder.record("check", "once")
        recorder.flush()
        recorder.flush()
        assert len(read_events(tmp_path)) == 1

    def test_a_broken_data_dir_never_raises(self, tmp_path):
        """Observability is never load-bearing: a recorder that cannot write
        must degrade to "no record", never to a failed bootstrap pass."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        r = PassRecorder(str(blocker / "sub"), autoflush=False)
        r.record("check", "entry")
        r.flush()  # must not raise

    def test_unserializable_detail_does_not_lose_the_record(self, tmp_path, recorder):
        recorder.record("check", "entry", detail={"obj": object()})
        recorder.flush()
        assert read_events(tmp_path)[0]["text"] == "entry"


class TestRotation:
    def test_rotates_at_the_size_bound_keeping_one_generation(self, tmp_path, recorder):
        path = tmp_path / EVENTS_FILENAME
        path.write_text("x" * (MAX_EVENTS_BYTES + 1), encoding="utf-8")

        recorder.record("check", "after rotation")
        recorder.flush()

        assert (tmp_path / (EVENTS_FILENAME + ".1")).exists()
        assert [e["text"] for e in read_events(tmp_path)] == ["after rotation"]

    def test_does_not_rotate_below_the_bound(self, tmp_path, recorder):
        (tmp_path / EVENTS_FILENAME).write_text("small\n", encoding="utf-8")
        recorder.record("check", "appended")
        recorder.flush()
        assert not (tmp_path / (EVENTS_FILENAME + ".1")).exists()

    def test_concurrent_rotators_keep_both_generations(self, tmp_path):
        path = tmp_path / EVENTS_FILENAME
        first = PassRecorder(str(tmp_path), autoflush=False)
        second = PassRecorder(str(tmp_path), autoflush=False)

        path.write_text("generation-a\n" + "x" * MAX_EVENTS_BYTES, encoding="utf-8")
        first._rotate_if_needed(str(path))
        path.write_text("generation-b\n" + "y" * MAX_EVENTS_BYTES, encoding="utf-8")
        second._rotate_if_needed(str(path))

        assert "generation-a" in (tmp_path / (EVENTS_FILENAME + ".1")).read_text()
        assert "generation-b" in path.read_text()


class TestRedaction:
    def test_secret_named_keys_are_masked(self):
        assert redact({"api_key": "sk-live-123"})["api_key"] == MASK
        assert redact({"PASSWORD": "hunter2"})["PASSWORD"] == MASK

    def test_innocent_keys_are_untouched(self):
        assert redact({"name": "uv"})["name"] == "uv"

    def test_secret_shaped_substrings_in_free_text_are_masked(self):
        out = redact("curl -H 'Authorization: Bearer abc123' https://x")
        assert "abc123" not in out and MASK in out

    def test_masks_command_line_secret_flags(self):
        assert "s3cret" not in redact("login --password s3cret --user me")

    def test_redaction_is_recursive_and_structure_preserving(self):
        out = redact({"env": [{"token": "t"}, {"name": "ok"}]})
        assert out["env"][0]["token"] == MASK
        assert out["env"][1]["name"] == "ok"

    def test_oversized_values_are_bounded_and_say_so(self):
        out = redact("y" * (MAX_DETAIL_CHARS + 500))
        assert len(out) < MAX_DETAIL_CHARS + 200
        assert "omitted" in out

    def test_secrets_are_masked_before_reaching_disk(self, tmp_path, recorder):
        """Record-time, not render-time: a secret must not exist in the file."""
        recorder.record("check", "entry", detail={"api_token": "sk-live-999"})
        recorder.flush()
        assert "sk-live-999" not in (tmp_path / EVENTS_FILENAME).read_text()


class TestRecordingList:
    def test_appends_reach_the_record_without_touching_call_sites(self, tmp_path, recorder):
        """Recording at the LIST is what let ~129 callers stay unchanged."""
        entries = entry_list(recorder, "action", plugin="p4-kit")
        entries.append("venv: created")
        recorder.flush()

        event = read_events(tmp_path)[0]
        assert event["text"] == "venv: created"
        assert event["plugin"] == "p4-kit"
        assert event["sev"] == "action"

    def test_still_behaves_as_a_list(self, recorder):
        entries = entry_list(recorder, "ok")
        entries.append("a")
        entries.extend(["b", "c"])
        assert entries == ["a", "b", "c"]
        assert isinstance(entries, list)

    def test_extend_records_each_item(self, tmp_path, recorder):
        entry_list(recorder, "ok").extend(["a", "b"])
        recorder.flush()
        assert len(read_events(tmp_path)) == 2

    def test_all_list_write_forms_are_recorded(self, tmp_path, recorder):
        entries = entry_list(recorder, "action")
        entries += [Entry("iadd")]
        entries.insert(0, "insert")
        entries[0] = "setitem"
        entries[1:] = ["slice-a", "slice-b"]
        recorder.flush()

        assert [event["text"] for event in read_events(tmp_path)] == [
            "iadd", "insert", "setitem", "slice-a", "slice-b",
        ]

    def test_append_rich_carries_display_and_detail(self, tmp_path, recorder):
        entries = entry_list(recorder, "action")
        entries.append_rich("uv sync failed: <500 lines>",
                            display="uv sync failed", detail={"stderr": "..."})
        recorder.flush()

        event = read_events(tmp_path)[0]
        assert event["text"] == "uv sync failed: <500 lines>"
        assert event["display"] == "uv sync failed"
        assert event["detail"] == {"stderr": "..."}
        assert entries == ["uv sync failed: <500 lines>"]

    def test_without_a_recorder_it_is_a_plain_list(self):
        entries = entry_list(None, "action")
        entries.append("x")
        assert entries == ["x"]
        assert not isinstance(entries, RecordingList)


class TestSecretShapesFoundInReview:
    """Regression tests for redaction gaps found in code review.

    The original patterns anchored the keyword with `\b`, which matches the toy
    form (`api_key=x`) and misses every form bootstrap actually handles: `_` is
    a word character, so there is no boundary before `API` in
    `ANTHROPIC_API_KEY`.
    """

    @pytest.mark.parametrize("text,secret", [
        ("ANTHROPIC_API_KEY=sk-ant-abc123", "sk-ant-abc123"),
        ("GH_TOKEN=ghp_xyz789", "ghp_xyz789"),
        ("MY_SECRET=hunter2", "hunter2"),
        ("OPENROUTER_API_KEY: sk-or-v1-deadbeef", "sk-or-v1-deadbeef"),
        ("export DB_PASSWORD=s3cr3t", "s3cr3t"),
    ])
    def test_env_var_shaped_secrets_are_masked(self, text, secret):
        out = redact(text)
        assert secret not in out
        assert MASK in out

    def test_authorization_header_value_is_masked_whole(self):
        r"""Masking only the first \S+ eats "Bearer" and leaves the token."""
        assert "abc123" not in redact("Authorization: Bearer abc123")


class TestEntryAttributesSurviveTransit:
    """Entries are built into a plain local list by a phase helper, then the
    caller extends a RecordingList with them. Anything the entry knows has to
    ride on the ENTRY to survive that hand-off."""

    def test_reprefix_preserves_short_and_detail(self):
        e = Entry("uv: failed - long tail", short="uv: failed",
                  detail={"stderr": "..."})
        out = reprefix(e, "config: ")
        assert out == "config: uv: failed - long tail"
        assert out.short == "config: uv: failed"
        assert out.detail == {"stderr": "..."}

    def test_reprefix_of_a_plain_string_is_just_a_prefix(self):
        out = reprefix("plain entry", "env: ")
        assert out == "env: plain entry"
        assert out.short is None

    def test_extending_a_recording_list_records_the_entry_attributes(self, tmp_path, recorder):
        """The hand-off that used to lose them: a plain list built by a phase
        helper, extended into the recording list afterwards."""
        local = []
        local.append(Entry("uv: install command failed - `winget ...`",
                           short="uv: install failed",
                           detail={"install_output": "winget said no"}))

        entry_list(recorder, "action").extend(local)
        recorder.flush()

        event = read_events(tmp_path)[0]
        assert event["display"] == "uv: install failed"
        assert event["detail"] == {"install_output": "winget said no"}
