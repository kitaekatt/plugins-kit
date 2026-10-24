"""Differential parity: llm_kit.pipeline.artifacts vs the legacy content_pipeline modules.

Two layers. (1) Normalized-source identity: each final module's text equals the
legacy text after CRLF->LF and the one permitted import re-point, so nothing
else moved. (2) Behavioural drives: the same inputs go through legacy and final
modules and results (including exception type and message) must be equal.
The legacy tree is imported under a sys.path insert scoped to the fixture.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LEGACY_LIB = REPO / "plugins" / "content-pipeline-kit" / "lib"
FINAL_ROOT = Path(__import__("llm_kit").__file__).resolve().parent / "pipeline" / "artifacts"

# module path shared by legacy (under content_pipeline) and final (under
# llm_kit.pipeline.artifacts)
PAIRS = [
    "cli.unsupported",
    "deliver.inplace",
    "deliver.projection",
    "freshness.classify",
    "freshness.ensure",
    "freshness.hashing",
    "freshness.tier",
    "providers.assembly",
    "providers.registry",
    "roundtrip.questions",
    "roundtrip.returns",
    "store.attributed",
    "store.candidate",
    "store.intermediary",
    "store.projection",
    "vcs.git_vcs",
    "vcs.null_vcs",
]
PACKAGE_INITS = ["deliver", "freshness", "providers", "roundtrip", "store", "vcs"]


@pytest.fixture(scope="module")
def mods():
    """{key: (legacy_module, final_module)}; legacy import scoped here."""
    saved_path = list(sys.path)
    before = set(sys.modules)
    sys.path.insert(0, str(LEGACY_LIB))
    try:
        out = {}
        for key in PAIRS:
            out[key] = (
                importlib.import_module("content_pipeline." + key),
                importlib.import_module("llm_kit.pipeline.artifacts." + key),
            )
        yield out
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - before:
            if name == "content_pipeline" or name.startswith("content_pipeline."):
                del sys.modules[name]


def norm(x):
    """Reduce results to plain comparable structures (legacy/final classes differ)."""
    if isinstance(x, enum.Enum):
        return ("enum", type(x).__name__, x.value)
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return (
            "dc",
            type(x).__name__,
            {f.name: norm(getattr(x, f.name)) for f in dataclasses.fields(x)},
        )
    if isinstance(x, BaseException):
        return ("exc", type(x).__name__, str(x))
    if isinstance(x, dict):
        return {norm(k): norm(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(norm(v) for v in x)
    return x


def call(fn, *a, **kw):
    """Normalized result, or the exception's type name and message."""
    try:
        return norm(fn(*a, **kw))
    except Exception as exc:  # noqa: BLE001 - parity compares failures too
        return ("raised", type(exc).__name__, str(exc))


def same(mods, key, name, *a, **kw):
    legacy, final = mods[key]
    lo = call(getattr(legacy, name), *a, **kw)
    fo = call(getattr(final, name), *a, **kw)
    assert lo == fo, (key, name, lo, fo)
    return lo


def each(mods, key, script):
    """Run ``script(module)`` against legacy and final; assert equal results."""
    legacy, final = mods[key]
    lo, fo = norm(script(legacy)), norm(script(final))
    assert lo == fo
    return lo


# ---------------------------------------------------------------- layer 1


def _legacy_text(rel: str) -> str:
    raw = (LEGACY_LIB / "content_pipeline" / rel).read_bytes().decode("ascii")
    return raw.replace("\r\n", "\n")


def _final_text(rel: str) -> str:
    return (FINAL_ROOT / rel).read_bytes().decode("ascii")


@pytest.mark.parametrize("key", PAIRS)
def test_source_identity_after_repoint(key):
    rel = key.replace(".", "/") + ".py"
    expected = _legacy_text(rel).replace(
        "from content_pipeline.", "from llm_kit.pipeline.artifacts."
    )
    assert _final_text(rel) == expected


@pytest.mark.parametrize("pkg", PACKAGE_INITS)
def test_package_init_identity(pkg):
    rel = pkg + "/__init__.py"
    assert _final_text(rel) == _legacy_text(rel)


def test_excluded_modules_absent():
    assert not (FINAL_ROOT / "freshness" / "seed.py").exists()
    assert not (FINAL_ROOT / "vcs" / "seam.py").exists()
    cli = sorted(p.name for p in (FINAL_ROOT / "cli").glob("*.py"))
    assert cli == ["__init__.py", "unsupported.py"]


@pytest.mark.parametrize("key", PAIRS)
def test_public_surface_equal(mods, key):
    legacy, final = mods[key]

    def surface(m):
        return sorted(
            n
            for n, v in vars(m).items()
            if not n.startswith("__")
            and getattr(v, "__module__", m.__name__) == m.__name__
        )

    assert surface(legacy) == surface(final)


# ---------------------------------------------------------------- layer 2


def test_hashing(mods):
    for v in [{"b": 1, "a": [1, 2]}, "x", 3, None, [1, {"z": 0}], {"a", "b"}]:
        same(mods, "freshness.hashing", "stable_json", v)
        same(mods, "freshness.hashing", "canonical_bytes", v)
        same(mods, "freshness.hashing", "content_hash", v, "tail")
    same(mods, "freshness.hashing", "digest", b"a", b"b", length=8)
    same(mods, "freshness.hashing", "digest", b"a", length=0)
    same(mods, "freshness.hashing", "content_hash", "a", length=64)
    same(mods, "freshness.hashing", "shared_snapshot", "a", 1)


def test_classify_and_tier(mods):
    legacy, final = mods["freshness.classify"]
    recs = [
        None,
        {},
        {"human": "h"},
        {"machine": "m", "generation_hash": "H"},
        {"machine": "m", "generation_hash": "OLD"},
        {"machine": "", "generation_hash": "H"},
    ]
    sl, sf = [], []
    for r in recs:
        for ex in (False, True):
            a = call(legacy.classify, r, "H", excluded=ex)
            b = call(final.classify, r, "H", excluded=ex)
            assert a == b
            sl.append(legacy.FreshnessState(a[2]))
            sf.append(final.FreshnessState(b[2]))
    for x, y in zip(sl, sf):
        for inc in (True, False):
            assert legacy.needs_generation(x, include_stale=inc) == final.needs_generation(
                y, include_stale=inc
            )
    assert norm(legacy.bucket_counts(sl)) == norm(final.bucket_counts(sf))
    same(
        mods, "freshness.classify", "classify", {"m": "x", "g": "H"}, "H",
        human_field="h", machine_field="m", hash_field="g",
    )
    same(mods, "freshness.tier", "is_cross_ref_stale", "a", "a")
    same(mods, "freshness.tier", "is_cross_ref_stale", "a", "b")


def test_ensure(mods, tmp_path):
    def script(m):
        disk = {"v": None}
        log = []
        spec = m.ArtifactSpec(
            regenerate=lambda: "R",
            content_hash=lambda r: r,
            load_existing=lambda: disk["v"],
            write=lambda r: disk.__setitem__("v", r) or log.append("w"),
            pre_write=lambda: log.append("pre"),
            prerequisites=(lambda: log.append("pq"),),
        )
        log.append(norm(m.ensure(spec)))
        log.append(norm(m.ensure(spec)))
        disk["v"] = ""
        log.append(norm(m.ensure(spec)))
        target = tmp_path / ("x" + str(id(m)))
        m.atomic_write(target, "h\u00e9llo")
        log.append(target.read_bytes())
        return log

    each(mods, "freshness.ensure", script)


def test_assembly(mods):
    def script(m):
        s = m.SlotSyntax()
        blocks = [m.Block("a", "A"), m.Block("b", "B", include=False), m.Block("c", "C")]
        return (
            m.assemble_blocks(blocks, separator="--"),
            m.assemble_blocks(blocks),
            s.slot("x"),
            s.parse_slots("a ${x} ${y} ${x}"),
            s.has_any_slot("no"),
            s.render("a ${x}", lambda n: n.upper()),
            s.render_map("a ${x}", {"x": "1"}),
            call(s.render_map, "a ${q}", {"x": "1"}),
            call(s.render_map, "a ${q}", {"x": "1"}, strict=False),
        )

    each(mods, "providers.assembly", script)
    same(mods, "providers.assembly", "assign_labels", ["k1", "k2"], prefix="p", start=3)
    same(mods, "providers.assembly", "invert_labels", {"a": "L", "b": "L"})
    same(mods, "providers.assembly", "invert_labels", {"a": "L", "b": "M"})
    same(mods, "providers.assembly", "relabel", {"item_1": 1, "zz": 2}, {"a": "item_1"})
    same(
        mods, "providers.assembly", "relabel", {"item_1": 1, "zz": 2},
        {"a": "item_1"}, strict=False,
    )


def test_provider_registry(mods):
    def script(m):
        m.clear()
        log = [call(m.register, "a", lambda *x: {"v": x}, tier="source")]
        log.append(call(m.register, "a", lambda *x: {}, tier="source"))
        log.append(call(m.register, "b", lambda *x: {}, tier="bogus"))
        m.register("g", lambda *x: {"g": 1}, tier="generation")
        log += [
            m.registered_names(),
            m.registered_names(tier="source"),
            m.tiers(),
            m.get_tier("a"),
            call(m.get_tier, "nope"),
            call(m.resolve, "nope"),
            call(m.invoke, "a", 1, 2),
            call(m.invoke, "a", expect_tier="generation"),
            call(m.run_tier, "source", 5),
            call(m.unregister, "a"),
            call(m.unregister, "a"),
        ]
        m.clear()
        log.append(m.registered_names())
        return log

    each(mods, "providers.registry", script)


def test_unsupported(mods):
    def script(m):
        r = m.UnsupportedRegistry()
        r.mark("u1", "why")
        doc = r.to_doc()
        stamped = {k: bool(v.pop("marked_at", None)) for k, v in doc.items()}
        r2 = m.UnsupportedRegistry.from_doc({"u9": {"reason": "r", "marked_at": "t"}})
        stub = m.stub_record("id", "rs", base={"a": 1, "b": 0}, carry_fields=("a", "b"))
        stub_ts = bool(stub["unsupported"].pop("marked_at"))
        return (
            doc, stamped, r.is_unsupported("u1"), r.reason("u1"), r.reason("x"),
            r.filter(["u1", "u2"]), r.clear("u1"), r.clear("u1"), r2.to_doc(),
            type(m.UnsupportedRegistry.from_doc(None)).__name__,
            stub, stub_ts, m.is_unsupported_record(stub),
            m.is_unsupported_record(None), m.record_reason(stub), m.record_reason({}),
        )

    each(mods, "cli.unsupported", script)


def _cand_script(m):
    C, Cell = m.Candidate, m.CandidateCell
    log = []
    cell = Cell(key=("k", 1))
    cell = m.append_candidate(cell, C("a", "va", status="shadow"))
    cell = m.append_candidate(cell, C("b", "vb", status="active", riders={"r": 1}))
    log.append(call(m.append_candidate, cell, C("a", "dup")))
    log.append(call(m.append_candidate, cell, C("c", "x", status="active")))
    log.append(call(C, ""))
    log.append(call(C, "z", status="weird"))
    p = m.promote_candidate(cell, "a")
    log.append(p)
    log.append(m.promote_candidate(cell, "a", retire_previous=True))
    log.append(call(m.promote_candidate, cell, "nope"))
    log.append(m.retire_candidate(p, "a"))
    log.append(call(m.retire_candidate, p, "nope"))
    log.append(call(m.set_locked, cell, True))
    log.append((cell.active, cell.get("a"), cell.get("zz"), cell.produced_count))
    d = m.cell_to_dict(cell)
    log.append(d)
    log.append(m.cell_from_dict(d))
    two_active = {
        "key": ["k"],
        "entries": [{"id": "x", "status": "active"}, {"id": "y", "status": "active"}],
    }
    log.append(call(m.cell_from_dict, two_active))
    log.append(m.candidate_to_dict(C("q", {"v": 1}, grade_summary={"g": 1})))
    log.append(call(m.candidate_from_dict, {"id": "q", "status": "bad"}))
    log.append(m.rider_cache_key({"a": 1}, salt="s"))
    store = m.CandidateStore()
    store.put(cell)
    log.append(m.store_to_doc(store))
    log.append(m.store_from_doc(m.store_to_doc(store)).get(("k", 1)))
    log.append(call(m.dump_store, store, yaml_dump=repr))
    log.append(call(store.add, cell))
    return log


def test_candidate(mods):
    each(mods, "store.candidate", _cand_script)


def test_attributed_and_projection(mods):
    for args in [(None, None, None), ("s", "m", None), ("s", "m", "h"), ("s", "", "h"), (0, False, "")]:
        same(mods, "store.attributed", "effective_value", *args)

    def attr(m):
        return (
            m.AttributedField(sourced="s", machine="m", human="h").resolve(),
            m.AttributedField(sourced="s", machine="m").resolve(),
            m.AttributedField(sourced="s", machine="m", human="h").resolve(
                present=lambda v: v == "m"
            ),
            m._present_or_falsy_scalar(0),
            m._present_or_falsy_scalar([]),
            m._present_or_falsy_scalar(None),
        )

    each(mods, "store.attributed", attr)

    outs = []
    for idx in (0, 1):
        cand = mods["store.candidate"][idx]
        proj = mods["store.projection"][idx]
        cell = cand.append_candidate(
            cand.CandidateCell(key=("k",)), cand.Candidate("a", "VA", status="active")
        )
        outs.append(
            norm(
                (
                    proj.project_cell(cell),
                    proj.project_cell(None, default="d"),
                    proj.project_cell(cand.CandidateCell(key=("k",)), default=7),
                )
            )
        )
    assert outs[0] == outs[1]


def test_merge_preserved_fields(mods):
    def script(m):
        policy = m.MergePolicy(
            human_fields=("h",),
            carry_fields=("c",),
            conditional_fields=("k",),
            unchanged=lambda old, new: old.get("src") == new.get("src"),
            collections={
                "qs": m.CollectionMerge(
                    id_key="id", human_fields=("answer",), keep_orphans_when=("answer",)
                )
            },
        )
        old = {
            "h": "H", "c": 0, "k": "K", "src": 1,
            "qs": [{"id": "1", "answer": "a"}, {"id": "9", "answer": "orph"}, {"id": "8"}],
        }
        outs = []
        for src in (1, 2):
            new = {"h": "", "c": None, "k": "", "src": src, "qs": [{"id": "1", "answer": ""}]}
            outs.append(m.merge_preserved_fields(old, new, policy=policy))
        outs.append(m.merge_preserved_fields(None, {"x": 1}, policy=policy))
        outs.append(call(m.merge_preserved_fields, old, {}, policy=m.MergePolicy()))
        return outs

    each(mods, "store.attributed", script)


def test_questions_and_returns(mods):
    qs = [
        {"id": "q1", "prompt": "p", "answer": ""},
        {"id": "q2", "prompt": "p2", "answer": "yes"},
    ]
    same(mods, "roundtrip.questions", "ask", qs, "q1", "new")
    same(mods, "roundtrip.questions", "ask", qs, "q3", "new")
    same(mods, "roundtrip.questions", "answer", qs, "q1", "a")
    same(mods, "roundtrip.questions", "answer", qs, "zz", "a")
    same(mods, "roundtrip.questions", "unanswered", qs)
    same(mods, "roundtrip.questions", "answered_context", qs)
    incoming = [{"id": "q1", "prompt": "n"}, {"id": "q9", "prompt": "z"}]
    same(mods, "roundtrip.questions", "merge_questions", qs, incoming)
    same(mods, "roundtrip.questions", "merge_questions", None, incoming)

    outs = []
    for idx in (0, 1):
        q = mods["roundtrip.questions"][idx]
        r = mods["roundtrip.returns"][idx]
        written = []
        rows = r.export_for_review(
            [1, 2], "dest",
            to_row=lambda e: r.ReviewRow(str(e), {"v": e}),
            serialize=lambda d, rr: written.append((d, len(rr))),
        )
        corr = r.intake_corrections(
            "src",
            parse=lambda s: [{"v": 1}, {"v": None}],
            to_correction=lambda row: None if row["v"] is None else r.Correction("e", row["v"]),
        )
        n = r.apply_corrections(corr, written.append)
        outs.append(
            norm((q.Question.from_dict({"id": 1, "prompt": None}), rows, corr, n, written))
        )
    assert outs[0] == outs[1]


def test_dataclass_field_shapes(mods):
    table = {
        "store.intermediary": ("IntermediarySpec", "IntermediaryResult"),
        "store.projection": ("GroupSlices", "ProjectionSpec"),
        "freshness.ensure": ("ArtifactSpec", "EnsureResult"),
        "deliver.inplace": ("InplaceSpec", "ApplyResult", "RevertResult", "ChangesetResult"),
        "deliver.projection": ("ProjectionResult",),
        "freshness.tier": ("SourceTier", "GenerationTier", "TwoTierHashes"),
    }
    for key, names in table.items():
        legacy, final = mods[key]
        for n in names:
            def shape(m):
                return [
                    (f.name, str(f.type), f.default is dataclasses.MISSING)
                    for f in dataclasses.fields(getattr(m, n))
                ]

            assert shape(legacy) == shape(final), (key, n)


def test_apply_projection_files(mods, tmp_path):
    def script(m):
        d = tmp_path / ("p" + str(id(m)))
        d.mkdir()
        target = d / "a.txt"

        def ser(p, c):
            p.write_text(str(c), encoding="utf-8")

        log = [
            norm(m.apply_projection(target, "one", serialize=ser)),
            norm(m.apply_projection(target, "two", serialize=ser)),
            call(
                m.apply_projection, target, "bad", serialize=ser,
                load=lambda p: p.read_text(), validate=lambda c: False,
            ),
            (target.read_text(), (d / "a.txt.bak").exists()),
            m.rollback_projection(target),
            target.read_text(),
            m.rollback_projection(d / "none.txt"),
        ]
        return repr(log).replace(d.as_posix(), "D")

    each(mods, "deliver.projection", script)


def test_marker_ownership_and_inplace(mods):
    def script(m):
        mk = m.Marker("gen")
        log = [
            call(m.Marker, ""),
            mk.is_marked("a gen b"), mk.is_marked("regen"), mk.is_marked(None),
            mk.add("x"), mk.add("x gen"), mk.add("  "), mk.add(None),
            mk.remove("a  gen  b"), mk.remove("gen"), mk.remove("a gen"), mk.remove("zz"),
            [e.value for e in m.Ownership],
            m.classify_ownership(False, None, mk),
            m.classify_ownership(True, "gen", mk),
            m.classify_ownership(True, "", mk),
        ]
        spec = m.InplaceSpec(
            marker=mk,
            row_id=lambda r: r["id"],
            value_present=lambda r: bool(r.get("v")),
            marker_text=lambda r: r.get("m", ""),
            store_value=lambda s, i: s.get(i),
            set_value=lambda r, v: {**r, "v": v},
            clear_value=lambda r: {**r, "v": ""},
            set_marker=lambda r: {**r, "m": mk.add(r.get("m", ""))},
            clear_marker=lambda r: {**r, "m": mk.remove(r.get("m", ""))},
            eligible=lambda s, r: "skip" if r["id"] == "p" else None,
        )
        rows = [
            {"id": "a", "v": "", "m": ""},
            {"id": "b", "v": "human", "m": ""},
            {"id": "c", "v": "old", "m": "gen"},
            {"id": "d", "v": "same", "m": "gen"},
            {"id": "p", "v": "", "m": ""},
            {"id": "n", "v": "", "m": ""},
        ]
        store = {"a": "A", "b": "B", "c": "C", "d": "same", "p": "P"}
        res = m.apply_inplace(rows, store, spec)
        log.append(norm(res))
        log.append(call(m.apply_inplace, res.rows, store, spec).__class__.__name__)
        log.append(norm(m.revert_marked(res.rows, spec)) if hasattr(res, "rows") else None)
        return log

    each(mods, "deliver.inplace", script)


def test_null_vcs(mods):
    def script(m):
        v = m.NullVcs()
        cs = v.make_changeset("d")
        return (
            cs, v.open_for_edit("p"), v.add("p"), v.move_into(cs, ["a"]),
            v.finalize_description(cs, "x"), v.revert("p"), v.delete_if_empty(cs),
        )

    each(mods, "vcs.null_vcs", script)


def test_git_vcs_surface(mods):
    legacy, final = mods["vcs.git_vcs"]
    assert issubclass(final.GitVcsError, RuntimeError)
    assert issubclass(legacy.GitVcsError, RuntimeError)
    fields = lambda m: [f.name for f in dataclasses.fields(m.GitChangeset)]
    assert fields(legacy) == fields(final)
