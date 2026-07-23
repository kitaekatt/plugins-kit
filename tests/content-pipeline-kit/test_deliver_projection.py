"""Tests for content_pipeline.deliver.projection.

Translates the localization append-only projection-writer behaviors: never
overwrite in place (move to .bak first), rollback via a rename, reload-
validation that restores the .bak on a bad write, and the xliff aggregation
SHAPE (many (artifact, unit) pairs -> one artifact list).
"""

import json

import pytest

from content_pipeline.deliver.projection import (
    aggregate_projections,
    apply_projection,
    rollback_projection,
)


def _json_serialize(path, content):
    path.write_text(json.dumps(content), encoding="utf-8")


def _json_load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_write_creates_no_backup(tmp_path):
    art = tmp_path / "proj.json"
    result = apply_projection(art, {"v": 1}, serialize=_json_serialize)
    assert result.written is True
    assert result.backup is None
    assert _json_load(art) == {"v": 1}


def test_second_write_moves_previous_to_bak(tmp_path):
    art = tmp_path / "proj.json"
    apply_projection(art, {"v": 1}, serialize=_json_serialize)
    result = apply_projection(art, {"v": 2}, serialize=_json_serialize)
    assert result.backup == art.with_name("proj.json.bak")
    assert _json_load(art) == {"v": 2}
    assert _json_load(result.backup) == {"v": 1}  # previous preserved


def test_rollback_is_a_rename(tmp_path):
    art = tmp_path / "proj.json"
    apply_projection(art, {"v": 1}, serialize=_json_serialize)
    apply_projection(art, {"v": 2}, serialize=_json_serialize)
    assert rollback_projection(art) is True
    assert _json_load(art) == {"v": 1}  # rolled back to previous version


def test_rollback_without_backup_returns_false(tmp_path):
    art = tmp_path / "proj.json"
    apply_projection(art, {"v": 1}, serialize=_json_serialize)
    assert rollback_projection(art) is False


def test_reload_validation_failure_restores_bak(tmp_path):
    art = tmp_path / "proj.json"
    apply_projection(art, {"v": 1}, serialize=_json_serialize)
    # A serializer that writes a corrupt artifact; reload validation should
    # restore the previous version rather than leave the corruption in place.
    def bad_serialize(path, content):
        path.write_text("{not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        apply_projection(
            art, {"v": 2}, serialize=bad_serialize, load=_json_load
        )
    assert _json_load(art) == {"v": 1}  # restored from .bak


def test_validate_predicate_failure_restores_bak(tmp_path):
    art = tmp_path / "proj.json"
    apply_projection(art, {"ok": True}, serialize=_json_serialize)
    result_holder = {}
    with pytest.raises(ValueError):
        apply_projection(
            art,
            {"ok": False},
            serialize=_json_serialize,
            load=_json_load,
            validate=lambda data: data.get("ok") is True,
        )
    assert _json_load(art) == {"ok": True}  # bad write rolled back


# -- xliff aggregation shape --------------------------------------------------

def test_aggregate_folds_pairs_by_artifact():
    pairs = [
        ("file_a.xlf", {"unit": 1}),
        ("file_b.xlf", {"unit": 2}),
        ("file_a.xlf", {"unit": 3}),
    ]
    agg = aggregate_projections(pairs)
    assert agg == {
        "file_a.xlf": [{"unit": 1}, {"unit": 3}],
        "file_b.xlf": [{"unit": 2}],
    }


def test_aggregate_preserves_input_order():
    agg = aggregate_projections([("a", 1), ("a", 2), ("a", 3)])
    assert agg["a"] == [1, 2, 3]
