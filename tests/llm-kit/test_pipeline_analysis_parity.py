"""Differential parity: llm_kit.pipeline.analysis vs legacy content_pipeline audit/provenance.

Two layers. (1) Source identity: each final module equals the legacy text after
CRLF->LF and the named re-points, so nothing else moved. (2) Behavioural drives:
the same inputs go through legacy and final modules and results, written file
bytes, and exceptions (type and message) must be equal. The legacy tree is
imported under a sys.path insert scoped to the fixture.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LEGACY_LIB = REPO / "plugins" / "content-pipeline-kit" / "lib"
LEGACY_ROOT = LEGACY_LIB / "content_pipeline"
FINAL_ROOT = REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "pipeline" / "analysis"

FILES = [
    "audit/__init__.py",
    "audit/auditor.py",
    "audit/reasoning_chain.py",
    "audit/report.py",
    "provenance/__init__.py",
    "provenance/call_audit.py",
    "provenance/record.py",
    "provenance/replay.py",
    "provenance/snapshot.py",
]

# Ordered legacy -> final text re-points. The first pair is the one deliberate
# behaviour change: the default recorded module names must be importable names
# in the final world (the legacy names no longer exist there).
REPOINTS = [
    ('("content_pipeline", "llm_scripting_kit")', '("llm_kit",)'),
    ("content_pipeline.freshness.classify", "llm_kit.pipeline.artifacts.freshness.classify"),
    ("content_pipeline.audit.auditor", "llm_kit.pipeline.analysis.audit.auditor"),
    ("content_pipeline.provenance.", "llm_kit.pipeline.analysis.provenance."),
    ("content_pipeline.pipeline.convergence_loop", "llm_kit.pipeline.core.convergence_loop"),
    ("``content_pipeline.llm.platform.CallAttempt``", "a call-attempt object"),
    ("imports nothing from ``content_pipeline``", "imports nothing from llm_kit"),
]


@pytest.fixture(scope="module")
def ns():
    """(legacy, final) namespaces of the modules a drive needs."""
    saved_path = list(sys.path)
    before = set(sys.modules)
    sys.path.insert(0, str(LEGACY_LIB))
    try:

        def load(audit_pkg, prov_pkg, loop, fresh):
            m = importlib.import_module
            return types.SimpleNamespace(
                auditor=m(audit_pkg + ".auditor"),
                chain=m(audit_pkg + ".reasoning_chain"),
                report=m(audit_pkg + ".report"),
                call_audit=m(prov_pkg + ".call_audit"),
                record=m(prov_pkg + ".record"),
                replay=m(prov_pkg + ".replay"),
                snapshot=m(prov_pkg + ".snapshot"),
                loop=m(loop),
                fresh=m(fresh),
            )

        legacy = load(
            "content_pipeline.audit",
            "content_pipeline.provenance",
            "content_pipeline.pipeline.convergence_loop",
            "content_pipeline.freshness.classify",
        )
        final = load(
            "llm_kit.pipeline.analysis.audit",
            "llm_kit.pipeline.analysis.provenance",
            "llm_kit.pipeline.core.convergence_loop",
            "llm_kit.pipeline.artifacts.freshness.classify",
        )
        yield legacy, final
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - before:
            if name == "content_pipeline" or name.startswith("content_pipeline."):
                del sys.modules[name]


# ---------------------------------------------------------------- layer 1


def _legacy_text(rel: str) -> str:
    raw = (LEGACY_ROOT / rel).read_bytes().decode("ascii")
    text = raw.replace("\r\n", "\n")
    for old, new in REPOINTS:
        text = text.replace(old, new)
    return text


@pytest.mark.parametrize("rel", FILES)
def test_source_identity_after_repoint(rel):
    assert (FINAL_ROOT / rel).read_bytes().decode("ascii") == _legacy_text(rel)


def test_no_residual_legacy_names():
    for rel in FILES:
        text = (FINAL_ROOT / rel).read_text(encoding="ascii")
        assert "content_pipeline" not in text and "_copy" not in text, rel


# ---------------------------------------------------------------- helpers


def norm(x):
    if isinstance(x, enum.Enum):
        return ("enum", type(x).__name__, x.value)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return ("dc", type(x).__name__, {f.name: norm(getattr(x, f.name)) for f in dataclasses.fields(x)})
    if isinstance(x, BaseException):
        return ("exc", type(x).__name__, str(x))
    if isinstance(x, dict):
        return {norm(k): norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(norm(v) for v in x)
    if isinstance(x, Path):
        return ("path", str(x))
    return x


def run(fn, *a, **kw):
    try:
        return norm(fn(*a, **kw))
    except Exception as exc:  # noqa: BLE001 - parity compares failures too
        return ("raised", type(exc).__name__, str(exc))


VOLATILE = {"run_id", "started_at", "finished_at", "at", "elapsed_s"}


def scrub(obj):
    if isinstance(obj, dict):
        return {k: ("<v>" if k in VOLATILE else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def tree(root: Path) -> dict:
    """{relpath: bytes} with volatile JSON values scrubbed (re-dumped sorted)."""
    out = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        data = p.read_bytes()
        rel = p.relative_to(root).as_posix()
        if p.suffix == ".json" and not data.startswith(b"S:"):
            data = json.dumps(scrub(json.loads(data)), sort_keys=True).encode()
        elif p.suffix == ".jsonl":
            data = b"\n".join(
                json.dumps(scrub(json.loads(line)), sort_keys=True).encode() for line in data.splitlines()
            )
        out[rel] = data
    return out


def _strip(x, work):
    """Replace the per-side work directory (any slash spelling) with a token."""
    if isinstance(x, str):
        for spelling in (work, work.replace("\\", "/"), work.replace("\\", "\\\\")):
            x = x.replace(spelling, "<W>")
        return x
    if isinstance(x, bytes):
        return _strip(x.decode("utf-8", "surrogateescape"), work).encode("utf-8", "surrogateescape")
    if isinstance(x, dict):
        return {_strip(k, work): _strip(v, work) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(_strip(v, work) for v in x)
    return x


def both(ns, drive, tmp_path):
    """Run drive(module_ns, workdir) for legacy and final; assert equal."""
    results = []
    for i, side in enumerate(ns):
        work = tmp_path / ("side%d" % i)
        work.mkdir()
        results.append(_strip(norm(drive(side, work)), str(work)))
    assert results[0] == results[1]
    return results[0]


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---------------------------------------------------------------- audit


def _spec(m):
    ents = {
        "apply_missing_val": dict(pol="apply", mark=False, rec=True, val="v", out=None),
        "apply_missing_none": dict(pol="apply", mark=False, rec=False, val=None, out=None),
        "apply_ok": dict(pol="apply", mark=True, rec=True, val="v", out="v"),
        "apply_mismatch": dict(pol="apply", mark=True, rec=True, val="v", out="w"),
        "apply_orphan": dict(pol="apply", mark=True, rec=False, val=None, out="x"),
        "excl_marked": dict(pol="exclude", mark=True, rec=True, val="v", out="v"),
        "excl_clean": dict(pol="exclude", mark=False, rec=False, val=None, out=None),
        "unk": dict(pol="unknown", mark=True, rec=True, val="v", out="v"),
        "blank": dict(pol="apply", mark=False, rec=True, val="  ", out=None),
    }
    spec = m.auditor.AuditSpec(
        policy=lambda e: e["pol"],
        output_marked=lambda e: e["mark"],
        store_has_record=lambda e: e["rec"],
        store_value=lambda e: e["val"],
        output_value=lambda e: e["out"],
    )
    return ents, spec


def test_audit_auditor_and_report(ns, tmp_path):
    def drive(m, work):
        ents, spec = _spec(m)
        out = {}
        for name, e in ents.items():
            out["entity:" + name] = run(m.auditor.audit_entity, e, name, spec)
        idof = lambda e: "%s-%s" % (e["pol"], e["val"])  # noqa: E731
        out["corpus"] = run(m.auditor.audit_corpus, list(ents.values()), spec, entity_id=idof)
        fs = m.auditor.audit_corpus(list(ents.values()), spec, entity_id=idof)
        out["refs"] = run(m.auditor.audit_references, [1, 2, 3], ref_id=str, resolves=lambda r: r == 2)
        out["refs_reason"] = run(m.auditor.audit_references, [1], ref_id=str, resolves=lambda r: False, reason="gone")
        out["refs_err"] = run(m.auditor.audit_references, [1], ref_id=str, resolves=lambda r: 1 / 0)
        out["counts"] = run(m.auditor.counts_by_kind, fs)
        out["group"] = run(m.auditor.group_by_kind, fs)
        st = m.fresh.FreshnessState
        states = [st.FRESH, st.STALE, st.MISSING, st.HUMAN, st.EXCLUDED, st.FRESH]
        out["cov"] = run(m.report.coverage_report, states)
        out["cov_f"] = run(m.report.coverage_report, states, fs)
        out["cov_empty"] = run(m.report.coverage_report, [])
        out["cov_bad"] = run(m.report.coverage_report, ["nope"])
        out["rollup"] = run(m.report.finding_rollup, fs)
        out["rank"] = run(m.report.impact_ranking, fs)
        out["rank_w"] = run(
            m.report.impact_ranking,
            fs,
            weights={m.auditor.FindingKind.FALSE_POSITIVE: 5.0, m.auditor.FindingKind.MISSING_VALUE: 0.5},
        )
        out["cost"] = run(m.report.cost_effectiveness_report, fs, {"total": 2.5}, resolved=4)
        out["cost0"] = run(m.report.cost_effectiveness_report, fs, {}, resolved=0)
        out["cost_none"] = run(m.report.cost_effectiveness_report, fs, {"total": None})
        out["cost_bad"] = run(m.report.cost_effectiveness_report, fs, {"total": "x"})
        out["kinds"] = [k.value for k in m.auditor.FindingKind]
        return out

    res = both(ns, drive, tmp_path)
    assert res["corpus"] and res["rank"]  # non-vacuous


def test_reasoning_chain(ns, tmp_path):
    def drive(m, work):
        c = m.chain
        out = {}
        ev = c.build_event(stage="s", inputs={"a": 1}, attempt=2, rejections=["r"], final="f", extra={"k": 1})
        out["event_full"] = scrub(ev)
        out["event_min"] = scrub(c.build_event())
        out["event_map_rej"] = scrub(c.build_event(rejections={"x": 1}))
        out["event_tuple_rej"] = scrub(c.build_event(rejections=("a", "b")))
        mem = c.InMemoryRecorder()
        c.record_chain(mem, "e1", [{"x": 1}, {"y": 2}])
        out["mem"] = mem.chains
        out["mem_chain"] = mem.chain("e1")
        out["mem_missing"] = mem.chain("zz")
        out["null"] = (c.NullRecorder().record("a", {}), c.NullRecorder().chain("a"))
        disk = {}
        side = c.SidecarRecorder(load=lambda e: disk.get(e), store=lambda e, v: disk.__setitem__(e, v))
        side.record("e", {"a": 1})
        side.record("e", {"b": 2})
        out["sidecar"] = (disk, side.chain("e"), side.chain("none"))

        resp1 = Obj(
            text="t1",
            output_contract={
                "contract_id": "c",
                "schema_version": 2,
                "schema_digest": "d",
                "policy": "p",
                "delivery": "dl",
                "disposition": "bad",
                "schema": "BODY",
            },
        )
        resp2 = Obj(text="t2", output_contract=Obj(to_json=lambda: {"contract_id": "c", "disposition": "ok"}))
        resp3 = Obj(output_contract=5)
        sub = Obj(
            responses=[resp1, resp2, resp3],
            rejections=[Obj(kind="k1"), "str"],
            payload={"p": 1},
            attempts=3,
        )
        rec = c.InMemoryRecorder()
        c.record_submission(rec, "e", sub, inputs={"in": 1})
        out["submission"] = scrub(rec.chains)
        rec2 = c.InMemoryRecorder()
        c.record_submission(rec2, "e", Obj(), stage="gen")
        out["submission_bare"] = scrub(rec2.chains)
        out["submission_err"] = run(c.record_submission, c.InMemoryRecorder(), "e", Obj(responses=5))
        return out

    res = both(ns, drive, tmp_path)
    assert res["submission"]["e"][0]["contract"]["schema_digest"] == "d"


# ---------------------------------------------------------------- provenance


def test_record_helpers_and_files(ns, tmp_path):
    def drive(m, work):
        r = m.record
        out = {}
        f = work / "a.txt"
        f.write_bytes(b"hello\r\nworld")
        out["sha"] = run(r.sha256_file, f)
        out["sha_missing"] = run(r.sha256_file, work / "nope")
        out["portable_in"] = run(r.portable_path, f, root=work)
        out["portable_out"] = run(r.portable_path, Path("/zz/q.txt"), root=work)
        out["snap"] = run(r.snapshot_file, f, work / "sub" / "b.txt")
        out["snap_missing"] = run(r.snapshot_file, work / "nope", work / "sub" / "c.txt")
        out["snap_bytes"] = (work / "sub" / "b.txt").read_bytes()
        r.json_dump({"b": 1, "a": [1, 2], "u": "x"}, work / "d.json")
        out["dump_bytes"] = (work / "d.json").read_bytes()
        out["load"] = r.json_load(work / "d.json")
        out["load_missing"] = run(r.json_load, work / "nope.json")
        return out

    res = both(ns, drive, tmp_path)
    assert res["sha"] and res["dump_bytes"]


def test_start_run_finish_fail_and_rotate(ns, tmp_path, monkeypatch):
    monkeypatch.setenv("LLMK_PARITY_V", "val")
    monkeypatch.delenv("LLMK_PARITY_P", raising=False)

    def drive(m, work):
        r = m.record
        out = {}
        src = work / "in.txt"
        src.write_text("data")
        bundle = work / "b1"
        with r.start_run(
            bundle,
            root=work,
            params={"p": 1},
            inputs={"i": src, "none": None, "gone": work / "x"},
            copy_inputs=True,
            env_values=["LLMK_PARITY_V"],
            env_presence=["LLMK_PARITY_P"],
            modules=("json", "no_such_module_xyz"),
            argv=["a", "b"],
            edits=["e1"],
        ) as rec:
            rec.finish(result={"ok": True})
        out["b1"] = tree(bundle)
        out["rec_dict"] = scrub(rec.record.to_dict())
        b2 = work / "b2"
        try:
            with r.start_run(b2, root=work, modules=(), argv=[]):
                raise RuntimeError("boom")
        except RuntimeError as exc:
            out["raised"] = str(exc)
        out["b2"] = tree(b2)
        b3 = work / "b3"
        r.start_run(b3, root=work, modules=(), argv=[]).finish()
        r.start_run(b3, root=work, modules=(), argv=[], rotate_label="old").finish()
        out["b3"] = sorted(tree(b3))
        out["rotate_none"] = run(r.rotate_prior_run, work / "empty", label="x")
        link = r.SourceLink(run_id="r", record="rec", cycle=1, stage="grade", snapshot="s", snapshot_sha256="h")
        b4 = work / "b4"
        r.start_run(b4, root=work, modules=(), argv=[], source=link).finish()
        out["b4"] = tree(b4)
        return out

    res = both(ns, drive, tmp_path)
    assert res["raised"] == "boom" and "segment-old/run.json" in res["b3"]


def test_start_run_default_modules(ns, tmp_path):
    """Deliberate divergence: legacy default names the legacy packages, final names llm_kit."""
    legacy, final = ns
    d1 = legacy.record.start_run(tmp_path / "l", root=tmp_path, argv=[])
    d2 = final.record.start_run(tmp_path / "f", root=tmp_path, argv=[])
    assert set(d1.record.modules) == {"content_pipeline", "llm_scripting_kit"}
    assert set(d2.record.modules) == {"llm_kit"}


def test_call_audit(ns, tmp_path):
    def drive(m, work):
        ca = m.call_audit
        out = {}
        aud = ca.CallAuditor(work / "audit")
        obs = aud.observer("chunk/one")
        resp = Obj(
            text="R1",
            from_cache=True,
            wall_ms=5,
            input_tokens=3,
            output_tokens=4,
            reported_cost_usd=0.5,
            reported_cost_source="prov",
            model="m",
        )
        obs(
            Obj(
                system="S",
                user="U",
                response=resp,
                validation_attempt=1,
                transport_attempt=1,
                model="m",
                temperature=0.1,
                max_tokens=9,
                cache_salt="x",
                effort="low",
                rejections=["bad"],
            )
        )
        obs(Obj(system="S", user="U2", response=None, error="E", rejections=()))
        obs(Obj(user="U2", response=resp))
        aud.observer("chunk/one")(Obj(user="Z", response=resp))
        aud.observer("")(Obj(user="Y", response=resp))
        aud.finalize()
        out["tree"] = tree(work / "audit")
        none = ca.CallAuditor(None)
        none.observer("c")(Obj())
        none.finalize()
        empty = ca.CallAuditor(work / "empty")
        empty.finalize()
        out["empty_exists"] = (work / "empty").exists()
        out["bad_attr"] = run(ca.CallAuditor(work / "bad").observer("c"), None)
        return out

    res = both(ns, drive, tmp_path)
    assert res["tree"]


def _events(m, store):
    L = m.loop
    K = L.LoopEventKind
    return [
        L.LoopEvent(K.CYCLE_STARTED, cycle=1, store=store, at=1.0),
        L.LoopEvent(K.STAGE_FINISHED, cycle=1, stage="grade", store=store, at=2.0, elapsed_s=0.1),
        L.LoopEvent(K.STAGE_FINISHED, cycle=1, stage="apply", store=store, at=2.5, elapsed_s=0.1),
        L.LoopEvent(K.STAGE_FINISHED, cycle=1, stage="fill", store=store, at=3.0, elapsed_s=0.2),
        L.LoopEvent(K.CYCLE_STARTED, cycle=None, store=store),
    ]


def _write_text_store(store, path):
    Path(path).write_text("S:" + str(store), encoding="ascii")


def test_snapshot_and_eventlog(ns, tmp_path):
    def drive(m, work):
        s = m.snapshot
        out = {}
        out["labels"] = [
            run(s.snapshot_label, st, ph)
            for st in ("grade", "select", "apply", "fill", "zz")
            for ph in ("before", "after", "mid")
        ]
        snap = s.StageSnapshotter(work / "bundle", write=_write_text_store)
        for ev in _events(m, "store1"):
            snap(ev)
        out["snaps"] = tree(work / "bundle")
        out["path_for"] = str(snap.path_for(2, "x").relative_to(work))
        lazy = s.StageSnapshotter(work / "lazy", write=lambda st, p: None)
        out["lazy_str"] = run(lazy._snap, 1, "lbl", "some/file")
        out["lazy_obj"] = run(lazy._snap, 1, "lbl", Obj())

        def boom(st, p):
            Path(p).write_text("x")
            raise OSError("disk")

        bad = s.StageSnapshotter(work / "bad", write=boom)
        out["boom"] = run(bad._snap, 1, "lbl", "s")
        out["boom_tree"] = sorted(tree(work / "bad"))
        log = s.EventLog(work / "log" / "ev.jsonl")
        K = m.loop.LoopEventKind
        for ev in _events(m, "st"):
            log(ev)
        log(m.loop.LoopEvent(K.CYCLE_STARTED, cycle=1, error=ValueError("bad")))
        out["log"] = tree(work / "log")
        out["jsonable"] = [
            s._jsonable(v) for v in (None, 1, "a", {1: (2, 3)}, [K.CYCLE_STARTED], K.CYCLE_STARTED)
        ]
        return out

    res = both(ns, drive, tmp_path)
    assert res["snaps"] and res["labels"][0] == "before-grade"


def _materialize(snapshot, workdir):
    return json.loads(Path(snapshot).read_text())


def _write_json_store(store, path):
    Path(path).write_text(json.dumps(store), encoding="ascii")


def test_replay(ns, tmp_path, monkeypatch):
    # replay uses start_run's default modules, the one deliberate divergence
    # (pinned in test_start_run_default_modules); everything else must match.
    monkeypatch.setitem(globals(), "VOLATILE", VOLATILE | {"modules"})

    def drive(m, work):
        out = {}
        src = work / "src"
        snap = m.snapshot.StageSnapshotter(src, write=_write_json_store)
        for ev in _events(m, {"n": 1}):
            snap(ev)
        common = dict(root=work, materialize=_materialize, write=_write_json_store)
        out["no_record"] = run(
            m.replay.replay_stage, src, cycle=1, stage="grade", run=lambda s, c: s, out_bundle=work / "o0", **common
        )
        m.record.start_run(src, root=work, modules=(), argv=[]).finish()
        out["bad_stage"] = run(
            m.replay.replay_stage, src, cycle=1, stage="zz", run=lambda s, c: s, out_bundle=work / "o1", **common
        )
        out["inside"] = run(
            m.replay.replay_stage, src, cycle=1, stage="grade", run=lambda s, c: s, out_bundle=src / "in", **common
        )
        out["no_snap"] = run(
            m.replay.replay_stage, src, cycle=2, stage="apply", run=lambda s, c: s, out_bundle=work / "o2", **common
        )
        res = m.replay.replay_stage(
            src,
            cycle=1,
            stage="grade",
            run=lambda s, c: {"n": s["n"] + 1},
            out_bundle=work / "o3",
            edit=lambda s: {"n": 10},
            params={"k": 1},
            **common,
        )
        out["result"] = (res.store, scrub(res.record.to_dict()))
        out["o3"] = tree(work / "o3")
        res2 = m.replay.replay_stage(
            src, cycle=1, stage="fill", run=lambda s, c: None, out_bundle=work / "o4", edits=["manual"], **common
        )
        out["result2"] = (res2.store, scrub(res2.record.to_dict()))
        out["run_error"] = run(
            m.replay.replay_stage, src, cycle=1, stage="grade", run=lambda s, c: 1 / 0, out_bundle=work / "o5", **common
        )
        out["o5"] = tree(work / "o5")
        return out

    res = both(ns, drive, tmp_path)
    assert res["result"][0] == {"n": 11}
