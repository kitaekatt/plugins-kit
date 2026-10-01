"""PR5: provenance.replay (replay_stage)."""

import hashlib
import json

import pytest

from content_pipeline.pipeline import convergence_loop as cl
from content_pipeline.provenance import record as rec
from content_pipeline.provenance import replay as rp
from content_pipeline.provenance import snapshot as snap


def _write(store, path):
    path.write_text(json.dumps(store, sort_keys=True), encoding="ascii")


def _materialize(snapshot, workdir):
    dst = workdir / "store.json"
    dst.write_bytes(snapshot.read_bytes())
    return json.loads(dst.read_text(encoding="utf-8"))


def _fill(store, cycle):
    # Output is a pure function of the cells and the cycle.
    store["out"] = {k: "%s#%d" % (v.upper(), cycle) for k, v in store["cells"].items()}


def _noop(store, cycle):
    store["seen"] = cycle


def _make_source(tmp_path, skip_apply=False):
    src = tmp_path / "src"
    root = tmp_path
    store = {"cells": {"a": "alpha", "b": "beta"}}
    with rec.start_run(src, root=root, params={"k": 1}, modules=()) as run_rec:
        result = cl.run(
            store,
            grade=_noop,
            select=_noop,
            apply=None if skip_apply else _noop,
            fill=_fill,
            measure=lambda s: (1, 5),
            max_cycles=2,
            observers=[snap.StageSnapshotter(src, write=_write)],
        )
        run_rec.finish(result={"cycles": result.cycles_run})
    return src, root, run_rec.record


def _tree_hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def _replay(tmp_path, src, root, edit=None, **kw):
    return rp.replay_stage(
        src,
        cycle=2,
        stage="fill",
        run=_fill,
        out_bundle=tmp_path / "out",
        root=root,
        materialize=_materialize,
        write=_write,
        edit=edit,
        **kw,
    )


def _edit_b(store):
    store["cells"]["b"] = "gamma"


def test_replay_fill_from_after_apply_snapshot_with_edited_input(tmp_path):
    src, root, _ = _make_source(tmp_path)
    original = json.loads((src / "cycle-2" / "store.after-fill.json").read_text())
    assert original["out"] == {"a": "ALPHA#2", "b": "BETA#2"}

    result = _replay(tmp_path, src, root, edit=_edit_b, edits=["cells.b -> gamma"])

    # Differs exactly where the edit predicts: only cell b.
    assert result.store["out"] == {"a": "ALPHA#2", "b": "GAMMA#2"}
    assert result.store["out"]["a"] == original["out"]["a"]
    after = json.loads((tmp_path / "out/cycle-2/store.after-fill.json").read_text())
    assert after == result.store
    before = json.loads((tmp_path / "out/cycle-2/store.before-fill.json").read_text())
    assert before["cells"]["b"] == "gamma"
    assert result.record.status == "ok"


def test_replay_record_links_source_run(tmp_path):
    src, root, source_record = _make_source(tmp_path)
    result = _replay(tmp_path, src, root, edit=_edit_b)
    link = result.record.source
    snapshot = src / "cycle-2" / "store.after-apply.json"
    assert link.run_id == source_record.run_id
    assert link.cycle == 2 and link.stage == "fill"
    assert link.snapshot == "src/cycle-2/store.after-apply.json"
    assert link.record == "src/run.json"
    assert link.snapshot_sha256 == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    on_disk = json.loads((tmp_path / "out" / "run.json").read_text())
    assert on_disk["source"]["run_id"] == source_record.run_id
    assert on_disk["run_id"] != source_record.run_id
    assert on_disk["edits"]


def test_replay_leaves_source_bundle_unchanged(tmp_path):
    src, root, _ = _make_source(tmp_path)
    before = _tree_hashes(src)
    _replay(tmp_path, src, root, edit=_edit_b)
    assert _tree_hashes(src) == before


def test_replay_refuses_missing_snapshot(tmp_path):
    src, root, _ = _make_source(tmp_path, skip_apply=True)
    with pytest.raises(FileNotFoundError):
        _replay(tmp_path, src, root)
    assert not (tmp_path / "out").exists()


def test_replay_refuses_unknown_stage_and_source_overlap(tmp_path):
    src, root, _ = _make_source(tmp_path)
    with pytest.raises(ValueError):
        rp.replay_stage(
            src, cycle=2, stage="nope", run=_fill, out_bundle=tmp_path / "out",
            root=root, materialize=_materialize, write=_write,
        )
    with pytest.raises(ValueError):
        rp.replay_stage(
            src, cycle=2, stage="fill", run=_fill, out_bundle=src / "replay",
            root=root, materialize=_materialize, write=_write,
        )


def test_replay_records_input_override_hash(tmp_path):
    src, root, _ = _make_source(tmp_path)
    glossary = tmp_path / "glossary.txt"
    glossary.write_text("term=edited\n", encoding="ascii")
    result = _replay(tmp_path, src, root, inputs={"glossary": glossary})
    entry = result.record.inputs["glossary"]
    assert entry["sha256"] == hashlib.sha256(glossary.read_bytes()).hexdigest()
    assert entry["path"] == "glossary.txt"


def test_replay_failure_marks_record_error(tmp_path):
    src, root, _ = _make_source(tmp_path)

    def boom(store, cycle):
        raise RuntimeError("stage broke")

    with pytest.raises(RuntimeError):
        rp.replay_stage(
            src, cycle=2, stage="fill", run=boom, out_bundle=tmp_path / "out",
            root=root, materialize=_materialize, write=_write,
        )
    on_disk = json.loads((tmp_path / "out" / "run.json").read_text())
    assert on_disk["status"] == "error"
    assert on_disk["source"] is not None
