"""PR4: provenance.snapshot (StageSnapshotter, EventLog, snapshot_label)."""

import json

import pytest

from content_pipeline.pipeline import convergence_loop as cl
from content_pipeline.provenance import snapshot as snap


def _write(store, path):
    path.write_text(json.dumps(store, sort_keys=True), encoding="ascii")


def _loop(store, observers, max_cycles=2, fill=None):
    def grade(s, c):
        s["graded"] = c

    def select(s, c):
        s["selected"] = c

    def apply(s, c):
        s["applied"] = c

    def default_fill(s, c):
        s["filled"] = c
        s["n"] = s.get("n", 0) + 1

    return cl.run(
        store,
        grade=grade,
        select=select,
        apply=apply,
        fill=fill or default_fill,
        measure=lambda s: (1, 5),
        max_cycles=max_cycles,
        observers=observers,
    )


def test_snapshotter_writes_five_files_per_cycle(tmp_path):
    s = snap.StageSnapshotter(tmp_path, write=_write)
    _loop({}, [s])
    for cycle in (1, 2):
        names = sorted(p.name for p in (tmp_path / f"cycle-{cycle}").iterdir())
        assert names == [
            "store.after-apply.json",
            "store.after-fill.json",
            "store.after-grade.json",
            "store.after-select.json",
            "store.before-grade.json",
        ]
    d = tmp_path / "cycle-1"
    assert json.loads((d / "store.before-grade.json").read_text()) == {}
    assert json.loads((d / "store.after-grade.json").read_text()) == {"graded": 1}
    assert json.loads((d / "store.after-apply.json").read_text())["applied"] == 1
    assert "filled" not in json.loads((d / "store.after-apply.json").read_text())
    assert json.loads((d / "store.after-fill.json").read_text())["filled"] == 1
    # cycle 2 starts from cycle 1's end state
    assert (tmp_path / "cycle-2" / "store.before-grade.json").read_text() == (
        d / "store.after-fill.json"
    ).read_text()


def test_snapshot_label_maps_before_to_previous_after():
    assert snap.snapshot_label("grade", "before") == "before-grade"
    assert snap.snapshot_label("fill", "before") == "after-apply"
    assert snap.snapshot_label("select", "before") == "after-grade"
    assert snap.snapshot_label("fill", "after") == "after-fill"
    with pytest.raises(ValueError):
        snap.snapshot_label("nope", "after")
    with pytest.raises(ValueError):
        snap.snapshot_label("fill", "during")


def test_suffix_is_used(tmp_path):
    s = snap.StageSnapshotter(tmp_path, write=_write, suffix=".yaml")
    _loop({}, [s], max_cycles=1)
    assert (tmp_path / "cycle-1" / "store.after-fill.yaml").is_file()


def test_snapshot_failure_stops_loop(tmp_path):
    calls = []

    def bad_write(store, path):
        calls.append(path.name)
        if len(calls) == 3:
            path.write_text("partial")
            raise OSError("disk full")
        _write(store, path)

    s = snap.StageSnapshotter(tmp_path, write=bad_write)
    ran = []

    def fill(st, c):
        ran.append(c)

    with pytest.raises(OSError, match="disk full"):
        _loop({}, [s], fill=fill)
    assert ran == []  # fill (4th stage) never ran
    names = [p.name for p in (tmp_path / "cycle-1").iterdir()]
    assert not any(n.endswith(".partial") for n in names)
    assert "store.after-select.json" not in names


def test_event_log_one_line_per_event(tmp_path):
    log = tmp_path / "sub" / "events.jsonl"
    _loop({"secret_store_marker": 1}, [snap.EventLog(log)], max_cycles=1)
    text = log.read_text(encoding="ascii")
    lines = [json.loads(x) for x in text.splitlines()]
    kinds = [x["kind"] for x in lines]
    assert kinds[0] == "loop_started" and kinds[-1] == "loop_finished"
    assert kinds.count("stage_started") == 4 == kinds.count("stage_finished")
    assert kinds.count("cycle_finished") == 1
    assert "secret_store_marker" not in text
    cyc = [x for x in lines if x["kind"] == "cycle_finished"][0]
    assert cyc["round"]["outstanding"] == 5 and cyc["verdict"] == "continue"


def test_event_log_records_stage_failure(tmp_path):
    log = tmp_path / "events.jsonl"

    def fill(s, c):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _loop({}, [snap.EventLog(log)], fill=fill)
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    failed = [x for x in lines if x["kind"] == "stage_failed"]
    assert failed and failed[0]["error"] == "RuntimeError: boom"
    assert failed[0]["stage"] == "fill"
