"""Tests for the json_entries manifest phase (_phase_json_entries), as
distinct from tests/bootstrap/test_json_check.py which tests the
check_json_entries/merge_json_entries primitives directly.
"""

from bootstrap_lib.engine import _process_manifest
from bootstrap_lib.json_check import JsonCheckResult


class TestJsonEntriesReChecksAfterMerge:
    """ini_settings re-checks its write and reports "write reported success,
    but re-check failed" on a failing re-check (see _phase_ini_settings).
    json_entries reported the merge's own .passed as "merged" with no
    re-check at all -- a merge that reports success but leaves the file
    unchanged (or leaves it in a state that still fails the check) was
    reported as a clean "merged" with no failure.
    """

    def test_failing_recheck_after_a_passing_merge_is_a_failure(
            self, tmp_path, monkeypatch):
        ref = tmp_path / "ref.json"
        target = tmp_path / "target.json"
        ref.write_text('{"entry": {"source": "local"}}')
        target.write_text('{"entry": {"source": "remote"}}')

        monkeypatch.setattr(
            "bootstrap_lib.json_check.merge_json_entries",
            lambda *a, **k: JsonCheckResult(passed=True, target=str(target), message="merged ok"),
        )
        monkeypatch.setattr(
            "bootstrap_lib.json_check.check_json_entries",
            lambda *a, **k: JsonCheckResult(passed=False, target=str(target), message="still mismatched"),
        )

        manifest = {
            "json_entries": [
                {
                    "reference": str(ref),
                    "target": str(target),
                    "merge_fields": ["source"],
                }
            ],
        }
        action_entries = []
        ok_entries = []

        failures = _process_manifest(
            manifest, "darwin", str(tmp_path), str(tmp_path),
            action_entries, ok_entries, plugin_name="test",
        )

        assert len(failures) == 1
        assert not any("merged" in e and "FAILED" not in e for e in action_entries)
        assert any("FAILED" in e for e in action_entries)
