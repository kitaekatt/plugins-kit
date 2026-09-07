"""_emit_unsupported_platform's background-mode pending write must not clobber
an undisplayed verdict from a prior pass. It used to write via _write_atomic
(unconditional overwrite); the crash sink (_emit_engine_crash) deliberately
uses _write_pending_if_absent for the same reason -- an unconsumed pending
file is evidence the user has not yet seen, and a hard-error pass overwriting
it silently drops that evidence.
"""

import json
import types

import bootstrap_lib.engine as engine


def _args(console=False, background=False):
    return types.SimpleNamespace(console=console, background=background)


_MSG = (
    "Unsupported Linux distribution: detected Fedora Linux 39. Bootstrap "
    "supports only Ubuntu among Linux distributions."
)


class TestEmitUnsupportedPlatformPreservesPending:
    def test_pre_existing_pending_content_is_preserved(self, tmp_path):
        pending = tmp_path / "bootstrap_display.pending"
        pending.write_text(json.dumps({"continue": True, "systemMessage": "EARLIER, UNDISPLAYED"}))

        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args(background=True))

        resp = json.loads(pending.read_text())
        assert resp["systemMessage"] == "EARLIER, UNDISPLAYED"

    def test_no_pre_existing_pending_still_writes(self, tmp_path):
        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args(background=True))
        pending = tmp_path / "bootstrap_display.pending"
        assert pending.is_file()
        resp = json.loads(pending.read_text())
        assert _MSG in resp["systemMessage"]


class TestEmitUnsupportedPlatformRecorder:
    def test_accepts_an_optional_recorder_and_records_the_emit(self, tmp_path):
        records = []

        class _FakeRecorder:
            def record_emit(self, channel, response):
                records.append((channel, response))

        engine._emit_unsupported_platform(
            _MSG, str(tmp_path), _args(background=True), recorder=_FakeRecorder())
        assert records
        assert records[0][0] in ("pending", "unsupported_platform")

    def test_recorder_defaults_to_none_and_does_not_raise(self, tmp_path):
        # No recorder passed -- must not raise, same as every other call site
        # in this module that tolerates a missing recorder.
        engine._emit_unsupported_platform(_MSG, str(tmp_path), _args(background=True))
