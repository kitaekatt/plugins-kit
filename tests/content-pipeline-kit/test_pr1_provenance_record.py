"""PR1: provenance.record (run record, rotation, portable paths)."""

import hashlib
import inspect
import json
import sys
import types

import pytest

from content_pipeline.provenance import record as rec


def _load(path):
    return json.loads(path.read_text(encoding="ascii"))


def test_record_paths_are_root_relative_and_portable(tmp_path):
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    f = root / "data" / "in.txt"
    f.write_text("x")
    r = rec.start_run(root / "bundle", root=root, inputs={"in": f})
    assert r.record.inputs["in"]["path"] == "data/in.txt"
    text = r.path.read_text(encoding="ascii")
    assert str(root).replace("\\", "/") not in text.replace("\\\\", "/")
    assert rec.portable_path(rec.Path.home() / "a" / "b", root=root) == "~/a/b"


def test_env_values_only_for_allow_list(tmp_path, monkeypatch):
    monkeypatch.setenv("PR1_ALLOWED", "v")
    monkeypatch.setenv("PR1_SECRET", "hunter2")
    monkeypatch.delenv("PR1_ABSENT", raising=False)
    r = rec.start_run(tmp_path / "b", root=tmp_path, env_values=["PR1_ALLOWED"],
                      env_presence=["PR1_SECRET", "PR1_ABSENT"])
    assert r.record.env == {"PR1_ALLOWED": "v", "PR1_SECRET": "set", "PR1_ABSENT": "unset"}
    assert "hunter2" not in r.path.read_text(encoding="ascii")


def test_inputs_hashed_and_copied(tmp_path):
    f = tmp_path / "in.txt"
    f.write_bytes(b"hello")
    r = rec.start_run(tmp_path / "b", root=tmp_path, inputs={"in": f, "gone": None},
                      copy_inputs=True)
    assert r.record.inputs["in"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert r.record.inputs["in"]["bytes"] == 5
    assert r.record.inputs["gone"] is None
    assert (tmp_path / "b" / "inputs" / "in.txt").read_bytes() == b"hello"


def test_rotate_prior_run_moves_to_segment(tmp_path):
    b = tmp_path / "b"
    rec.start_run(b, root=tmp_path, inputs={"i": _mk(tmp_path)}, copy_inputs=True)
    (b / "run.log").write_text("log")
    moved = rec.rotate_prior_run(b, label="one", logs=["run.log"])
    assert not (b / "run.json").exists() and not (b / "inputs").exists()
    assert (b / "segment-one" / "run.json").is_file()
    assert (b / "segment-one" / "inputs").is_dir()
    assert (b / "segment-one" / "run.log").read_text() == "log"
    assert "segment-one/run.json" in moved
    r2 = rec.start_run(b, root=tmp_path)
    assert r2.record.segments == ["segment-one"]


def _mk(tmp_path):
    f = tmp_path / "seed.txt"
    f.write_text("s")
    return f


def test_rotate_uses_suffix_for_incompatible_segment(tmp_path):
    b = tmp_path / "b"
    rec.start_run(b, root=tmp_path)
    rec.rotate_prior_run(b, label="x")
    rec.start_run(b, root=tmp_path)
    rec.rotate_prior_run(b, label="x")
    assert (b / "segment-x" / "run.json").is_file()
    assert (b / "segment-x-b" / "run.json").is_file()


def test_rotate_without_prior_is_noop(tmp_path):
    assert rec.rotate_prior_run(tmp_path / "none", label="x") == []


def test_start_run_rotate_label_rotates_first(tmp_path):
    b = tmp_path / "b"
    first = rec.start_run(b, root=tmp_path)
    second = rec.start_run(b, root=tmp_path, rotate_label="r")
    assert second.record.segments == ["segment-r"]
    old = _load(b / "segment-r" / "run.json")
    assert old["run_id"] == first.record.run_id


def test_context_manager_marks_error_and_reraises(tmp_path):
    with pytest.raises(ValueError):
        with rec.start_run(tmp_path / "b", root=tmp_path) as r:
            raise ValueError("boom")
    data = _load(r.path)
    assert data["status"] == "error"
    assert data["error"] == "ValueError: boom"
    assert data["finished_at"]


def test_finish_marks_ok_with_result(tmp_path):
    with rec.start_run(tmp_path / "b", root=tmp_path) as r:
        assert _load(r.path)["status"] == "running"
        r.finish(result={"n": 3})
    data = _load(r.path)
    assert data["status"] == "ok" and data["result"] == {"n": 3}
    assert data["schema"] == rec.SCHEMA


def test_record_is_ascii(tmp_path):
    r = rec.start_run(tmp_path / "b", root=tmp_path, params={"name": "café"},
                      argv=["x", "ü"])
    raw = r.path.read_bytes()
    raw.decode("ascii")
    assert _load(r.path)["params"]["name"] == "café"


def test_modules_default_imports_nothing(tmp_path):
    name = "pr1_never_imported_mod"
    assert name not in sys.modules
    r = rec.start_run(tmp_path / "b", root=tmp_path, modules=[name])
    assert name not in sys.modules
    assert r.record.modules[name] == {"version": None, "location": None}


def test_modules_version_from_sys_modules(tmp_path, monkeypatch):
    mod = types.ModuleType("pr1_fake_mod")
    mod.__version__ = "9.9"
    monkeypatch.setitem(sys.modules, "pr1_fake_mod", mod)
    r = rec.start_run(tmp_path / "b", root=tmp_path, modules=["pr1_fake_mod"])
    assert r.record.modules["pr1_fake_mod"]["version"] == "9.9"


def test_modules_version_from_metadata_preferred(tmp_path):
    r = rec.start_run(tmp_path / "b", root=tmp_path, modules=["pytest"])
    assert r.record.modules["pytest"]["version"] == __import__("importlib.metadata").metadata.version("pytest")


def test_no_shared_mutable_defaults():
    for fn in (rec.start_run, rec.rotate_prior_run):
        for p in inspect.signature(fn).parameters.values():
            assert not isinstance(p.default, (dict, list, set)), p.name
    a = rec.ProvenanceRecord("a", "t", [], {}, {}, {}, {})
    b = rec.ProvenanceRecord("b", "t", [], {}, {}, {}, {})
    a.edits.append("e")
    a.segments.append("s")
    assert b.edits == [] and b.segments == []


def test_source_link_serialized(tmp_path):
    link = rec.SourceLink("rid", "run.json", 2, "fill", "cycle-2/s.json", "ab")
    r = rec.start_run(tmp_path / "b", root=tmp_path, source=link, edits=["cell A1"])
    data = _load(r.path)
    assert data["source"]["run_id"] == "rid" and data["source"]["cycle"] == 2
    assert data["edits"] == ["cell A1"]


def test_snapshot_file_and_sha(tmp_path):
    src = tmp_path / "s.txt"
    src.write_text("q")
    assert rec.snapshot_file(src, tmp_path / "d" / "o.txt") is True
    assert rec.snapshot_file(tmp_path / "nope", tmp_path / "x") is False
    assert rec.sha256_file(tmp_path / "d" / "o.txt") == hashlib.sha256(b"q").hexdigest()
