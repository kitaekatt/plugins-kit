"""Tests for content_pipeline.freshness.ensure and .tier.

These cases mirror the dependency-driven regeneration contract pinned by the
two-tier source system's ensure-chain suite (its ``ensure_brief`` ->
``ensure_lang_brief`` -> ``ensure_prepared_chunks`` cascade and best-effort
version-control edit hook) plus the two-tier cross-reference check, translated
to the plugin's neutral vocabulary as the port-equivalence baseline. No
game/domain concepts appear in the test bodies: an "artifact" is regenerated
in memory, content-hashed, and written only on a real change; a "pre-write
hook" stands in for the version-control open-for-edit seam.
"""

import os

import pytest

from content_pipeline.freshness.ensure import (
    ArtifactSpec,
    atomic_write,
    ensure,
)
from content_pipeline.freshness.hashing import content_hash
from content_pipeline.freshness.tier import (
    GenerationTier,
    SourceTier,
    TwoTierHashes,
    is_cross_ref_stale,
)


# -- an in-memory artifact harness --------------------------------------------

class FakeArtifact:
    """A regenerate/hash/load/write quartet backed by an in-memory store.

    ``inputs`` is the mutable upstream state; ``regenerate`` reads it, so a
    test can mutate inputs and observe whether ensure rewrites. ``store`` is
    the "disk" -- a dict holding the persisted representation. ``writes`` and
    ``pre_writes`` count side effects.
    """

    def __init__(self, inputs):
        self.inputs = dict(inputs)
        self.store = {}
        self.writes = 0
        self.pre_writes = 0

    def regenerate(self):
        return dict(self.inputs)

    def content_hash(self, repr_):
        return content_hash(repr_)

    def load_existing(self):
        return self.store.get("current")

    def write(self, repr_):
        self.store["current"] = dict(repr_)
        self.writes += 1

    def pre_write(self):
        self.pre_writes += 1

    def spec(self, **overrides):
        base = dict(
            regenerate=self.regenerate,
            content_hash=self.content_hash,
            load_existing=self.load_existing,
            write=self.write,
            pre_write=self.pre_write,
        )
        base.update(overrides)
        return ArtifactSpec(**base)


# -- ensure: create / no-op / rewrite -----------------------------------------

def test_ensure_creates_when_missing():
    fa = FakeArtifact({"a": 1})
    result = ensure(fa.spec())
    assert result.written is True
    assert fa.writes == 1
    assert fa.store["current"] == {"a": 1}


def test_ensure_no_op_when_content_unchanged():
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    result = ensure(fa.spec())
    assert result.written is False
    assert fa.writes == 1  # not incremented on the second call


def test_ensure_no_op_does_not_call_pre_write():
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    fa.pre_writes = 0
    ensure(fa.spec())
    assert fa.pre_writes == 0


def test_ensure_rewrites_when_inputs_change():
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    fa.inputs["a"] = 2
    result = ensure(fa.spec())
    assert result.written is True
    assert fa.writes == 2
    assert fa.store["current"] == {"a": 2}


def test_ensure_calls_pre_write_before_a_real_write():
    fa = FakeArtifact({"a": 1})
    result = ensure(fa.spec())
    assert result.written is True
    assert fa.pre_writes == 1


def test_ensure_recreates_after_deletion():
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    del fa.store["current"]  # simulate a deleted artifact
    result = ensure(fa.spec())
    assert result.written is True
    assert fa.store["current"] == {"a": 1}


def test_ensure_result_carries_fresh_representation_even_on_no_op():
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    result = ensure(fa.spec())
    assert result.written is False
    assert result.representation == {"a": 1}


# -- ensure: legacy empty hash forces one rewrite -----------------------------

def test_ensure_rewrites_when_existing_hash_is_empty():
    # A legacy artifact whose recorded content hash is empty must be
    # rewritten once so the field gets populated -- the
    # ``existing_hash and existing_hash == new_hash`` guard.
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    fa.writes = 0

    # content_hash that reports "" for the existing artifact but a real
    # hash for the freshly regenerated one.
    def hashing(repr_):
        if repr_ is fa.store.get("current"):
            return ""
        return content_hash(repr_)

    result = ensure(fa.spec(content_hash=hashing))
    assert result.written is True
    assert fa.writes == 1


def test_ensure_rewrites_when_existing_is_unreadable():
    # A corrupt/unreadable artifact -> load_existing returns None -> rewrite.
    fa = FakeArtifact({"a": 1})
    ensure(fa.spec())
    fa.writes = 0
    result = ensure(fa.spec(load_existing=lambda: None))
    assert result.written is True
    assert fa.writes == 1


# -- ensure: the cascade (prerequisites) --------------------------------------

def test_prerequisites_run_before_regeneration():
    order = []

    upstream = FakeArtifact({"u": 1})

    def upstream_ensure():
        order.append("upstream")
        return ensure(upstream.spec())

    downstream = FakeArtifact({"d": 1})

    def down_regen():
        order.append("regen")
        return dict(downstream.inputs)

    spec = downstream.spec(
        regenerate=down_regen,
        prerequisites=(upstream_ensure,),
    )
    ensure(spec)
    assert order == ["upstream", "regen"]
    assert upstream.store["current"] == {"u": 1}


def test_upstream_drift_cascades_into_downstream_rewrite():
    # Deleting/altering an upstream artifact and re-running the downstream
    # ensure must regenerate both -- the freshness check walks the chain on
    # every call.
    upstream = FakeArtifact({"u": 1})
    downstream = FakeArtifact({"d": 1})

    def down_regen():
        # Downstream content folds in upstream's current persisted state,
        # so upstream drift changes the downstream content hash.
        return {"d": downstream.inputs["d"], "upstream": dict(upstream.store.get("current") or {})}

    spec = lambda: downstream.spec(  # noqa: E731 - local factory
        regenerate=down_regen,
        prerequisites=(lambda: ensure(upstream.spec()),),
    )
    ensure(spec())
    downstream.writes = 0
    # Mutate upstream inputs; the cascade must rewrite downstream.
    upstream.inputs["u"] = 2
    result = ensure(spec())
    assert result.written is True
    assert downstream.writes == 1
    assert downstream.store["current"]["upstream"] == {"u": 2}


# -- atomic_write default helper ----------------------------------------------

def test_atomic_write_creates_file_and_parent(tmp_path):
    target = tmp_path / "nested" / "out.txt"
    atomic_write(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write(target, "first")
    atomic_write(target, "second")
    assert target.read_text(encoding="utf-8") == "second"


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write(target, "content")
    leftovers = [p for p in os.listdir(tmp_path) if p != "out.txt"]
    assert leftovers == []


def test_ensure_end_to_end_with_atomic_write(tmp_path):
    # ensure driving the real atomic_write default: create, no-op, rewrite.
    path = tmp_path / "artifact.txt"
    inputs = {"v": "one"}

    spec_kwargs = dict(
        regenerate=lambda: f"value={inputs['v']}",
        content_hash=lambda text: content_hash(text),
        load_existing=lambda: path.read_text(encoding="utf-8") if path.is_file() else None,
        write=lambda text: atomic_write(path, text),
    )
    first = ensure(ArtifactSpec(**spec_kwargs))
    assert first.written is True
    second = ensure(ArtifactSpec(**spec_kwargs))
    assert second.written is False
    inputs["v"] = "two"
    third = ensure(ArtifactSpec(**spec_kwargs))
    assert third.written is True
    assert path.read_text(encoding="utf-8") == "value=two"


# -- tier: the two-tier model + cross-reference -------------------------------

def test_tier_dataclasses_carry_hashes():
    pair = TwoTierHashes(source=SourceTier("s"), generation=GenerationTier("g"))
    assert pair.source.hash == "s"
    assert pair.generation.hash == "g"


def test_cross_ref_fresh_when_recorded_matches_current():
    assert is_cross_ref_stale("srchash", "srchash") is False


def test_cross_ref_stale_when_recorded_differs():
    assert is_cross_ref_stale("old", "new") is True


def test_cross_ref_empty_recorded_forces_rebuild():
    # A legacy derived artifact with no recorded source hash is treated as
    # stale so the cross-ref field gets populated on first encounter.
    assert is_cross_ref_stale("", "anything") is True


def test_tier_hashes_are_frozen():
    with pytest.raises(Exception):
        SourceTier("s").hash = "mutated"  # type: ignore[misc]
