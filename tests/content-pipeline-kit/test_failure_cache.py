"""The negative failure cache, keyed by the consumer's attempt key.

Generalized from a consumer's tested deterministic-failure cache suite.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from content_pipeline.execution.failure_cache import (
    FINGERPRINT_VERSION,
    FailureCache,
    FailureRecord,
    error_fingerprint,
)


def _record(unit_id="unit", attempt_key="attempt", version=1, code="invalid-output", stage="generate"):
    return FailureRecord(
        stage=stage,
        unit_id=unit_id,
        attempt_key=attempt_key,
        error_fingerprint="error-hash",
        fingerprint_version=version,
        code=code,
        summary="bad output",
    )


def test_exact_attempt_hits_and_a_changed_attempt_or_version_misses(tmp_path: Path):
    cache = FailureCache(tmp_path / "failures.json")
    record = _record()
    cache.record(record)

    assert cache.lookup("generate", "unit", "attempt") == record
    assert cache.lookup("generate", "unit", "other") is None
    assert cache.lookup("generate", "unit", "attempt", fingerprint_version=2) is None


def test_a_new_record_replaces_the_units_entry(tmp_path: Path):
    cache = FailureCache(tmp_path / "failures.json")
    cache.record(_record(attempt_key="first"))
    cache.record(_record(attempt_key="second"))

    assert cache.lookup("generate", "unit", "first") is None
    assert cache.lookup("generate", "unit", "second").attempt_key == "second"


def test_clear_removes_only_one_stage_of_one_unit(tmp_path: Path):
    cache = FailureCache(tmp_path / "failures.json")
    first = _record(unit_id="u1")
    second = _record(unit_id="u2")
    other_stage = _record(unit_id="u1", stage="place")
    for record in (first, second, other_stage):
        cache.record(record)

    cache.clear("generate", "u1")

    assert cache.lookup("generate", "u1", "attempt") is None
    assert cache.lookup("generate", "u2", "attempt") == second
    assert cache.lookup("place", "u1", "attempt") == other_stage


def test_clearing_an_absent_entry_writes_nothing(tmp_path: Path):
    path = tmp_path / "failures.json"
    FailureCache(path).clear("generate", "unit")
    assert not path.exists()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"schema_version": 2, "failures": {}}',
        '{"schema_version": 1, "failures": {"x": {"stage": "s"}}}',
    ],
    ids=["not-json", "wrong-schema", "missing-fields"],
)
def test_a_corrupt_file_fails_open_and_reports(tmp_path: Path, content):
    path = tmp_path / "failures.json"
    path.write_text(content, encoding="ascii")
    cache = FailureCache(path)

    assert cache.lookup("generate", "unit", "attempt") is None
    assert "failure cache ignored" in cache.report


def test_a_mismatched_key_is_corrupt(tmp_path: Path):
    path = tmp_path / "failures.json"
    cache = FailureCache(path)
    cache.record(_record())
    payload = json.loads(path.read_text(encoding="ascii"))
    payload["failures"]["generate\0elsewhere"] = payload["failures"].pop("generate\0unit")
    path.write_text(json.dumps(payload), encoding="ascii")

    assert cache.lookup("generate", "unit", "attempt") is None
    assert cache.report


def test_concurrent_records_keep_unrelated_entries(tmp_path: Path):
    cache = FailureCache(tmp_path / "failures.json")
    records = [_record(unit_id="unit-%d" % i, attempt_key=str(i)) for i in range(12)]

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(cache.record, records))

    assert [cache.lookup("generate", r.unit_id, r.attempt_key) for r in records] == records


def test_the_file_is_ascii_json_with_schema_and_seven_fields(tmp_path: Path):
    path = tmp_path / "failures.json"
    FailureCache(path).record(_record())

    raw = path.read_bytes()
    assert raw.decode("ascii")
    payload = json.loads(raw)
    assert payload["schema_version"] == 1
    assert set(payload["failures"]["generate\0unit"]) == {
        "stage", "unit_id", "attempt_key", "error_fingerprint",
        "fingerprint_version", "code", "summary",
    }


def test_record_refuses_a_malformed_record(tmp_path: Path):
    cache = FailureCache(tmp_path / "failures.json")
    with pytest.raises(TypeError):
        cache.record({"stage": "generate"})
    with pytest.raises(TypeError):
        cache.record(_record(version="1"))


def test_error_fingerprint_collapses_whitespace_and_sorts_atoms():
    details = [
        {"code": "z", "file": "b", "message": " two\n words "},
        {"code": "a", "file": None, "message": "one\tword"},
    ]
    expected = error_fingerprint("failure", details)

    assert expected == error_fingerprint(
        "failure", [details[1], {"code": "z", "file": "b", "message": "two words"}]
    )
    assert expected != error_fingerprint("other", details)
    assert len(expected) == 64
    assert FINGERPRINT_VERSION == 1
