from pathlib import Path

import pytest
import yaml

from yaml_data_editor_kit.dispatch.state import (
    PlanDigestError,
    PlanUnitSetError,
    load_plan,
    write_plan,
)


def _write(path: Path, **kwargs):
    return write_plan(path, corpus_path=path.parent / "corpus", comment_store_path=path.parent / "comments", **kwargs)


def test_plan_round_trips_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "run" / "dispatch-plan.yaml"
    written = _write(path, run_id="dispatch-1", units=[{"id": "unit-a", "payload": {"x": 1}}])
    loaded = load_plan(path)
    assert loaded == written
    assert loaded.adapter_version.endswith(loaded.digest)


def test_tampered_digest_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    _write(path, run_id="dispatch-1", units=[])
    raw = yaml.safe_load(path.read_text())
    raw["digest"] = "0" * 64
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(PlanDigestError, match="digest"):
        load_plan(path)


def test_tampered_unit_set_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plan.yaml"
    _write(path, run_id="dispatch-1", units=[{"id": "unit-a"}])
    with pytest.raises(PlanUnitSetError, match="unit set"):
        load_plan(path, execution_unit_ids=["unit-b"])


def test_atomic_write_never_exposes_partial_yaml(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "plan.yaml"
    _write(path, run_id="dispatch-1", units=[])
    original = path.read_bytes()

    def interrupted(source, target):
        raise RuntimeError("interrupted")

    monkeypatch.setattr("yaml_data_editor_kit.dispatch.run.os.replace", interrupted)
    with pytest.raises(RuntimeError):
        _write(path, run_id="dispatch-2", units=[])
    assert path.read_bytes() == original
