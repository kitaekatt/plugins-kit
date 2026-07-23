"""Tests for content_pipeline.deliver.inplace.

Translates the first-pass marker + apply + revert behaviors, generalized to a
neutral row shape:

- marker schema: add/remove idempotent + whitespace-normalizing; HUMAN vs
  MACHINE vs EMPTY ownership classification (test_markers_*).
- apply purely from the store: marked rows rebuilt from the store, human rows
  untouched, no-value rows skipped, policy gate honored (test_markers /
  _apply_assignments).
- revert: strip marker + clear value on exactly the marked rows, exact mutated
  set (test_revert.py).
- changeset choreography: placeholder up front, per-item inline moves,
  description rebuilt from the moved subset, delete-if-empty; driven against a
  MockVcs and the NullVcs (test_cl_creation.py, null equivalent).
"""

from dataclasses import dataclass, field, replace

from content_pipeline.deliver.inplace import (
    ApplyResult,
    InplaceSpec,
    Marker,
    Ownership,
    apply_inplace,
    classify_ownership,
    deliver_changeset,
    revert_marked,
)
from content_pipeline.vcs.null_vcs import NullVcs


# A neutral row: an id, a value, and a marker-text field. Frozen so the set_*
# callables must return new rows (mirroring the store's do-no-harm boundary).
@dataclass(frozen=True)
class Row:
    id: str
    value: str = ""
    marker: str = ""


MARK = Marker("[MACHINE]")

SPEC = InplaceSpec(
    marker=MARK,
    row_id=lambda r: r.id,
    value_present=lambda r: bool(r.value),
    marker_text=lambda r: r.marker,
    store_value=lambda store, rid: store.get(rid),
    set_value=lambda r, v: replace(r, value=v),
    clear_value=lambda r: replace(r, value=""),
    set_marker=lambda r: replace(r, marker=MARK.add(r.marker)),
    clear_marker=lambda r: replace(r, marker=MARK.remove(r.marker)),
)


# -- Marker schema ------------------------------------------------------------

def test_marker_add_is_idempotent():
    assert MARK.add("") == "[MACHINE]"
    once = MARK.add("note")
    assert once == "note [MACHINE]"
    assert MARK.add(once) == once  # idempotent


def test_marker_remove_normalizes_whitespace():
    assert MARK.remove("note [MACHINE]") == "note"
    assert MARK.remove("[MACHINE]") == ""
    assert MARK.remove("no marker here") == "no marker here"


def test_marker_rejects_empty_tag():
    import pytest

    with pytest.raises(ValueError):
        Marker("")


def test_classify_ownership():
    assert classify_ownership(False, "", MARK) is Ownership.EMPTY
    assert classify_ownership(True, "[MACHINE]", MARK) is Ownership.MACHINE
    assert classify_ownership(True, "", MARK) is Ownership.HUMAN  # value, no marker


# -- apply purely from the store ----------------------------------------------

def test_apply_rebuilds_marked_and_empty_rows_from_store():
    rows = [Row(id="a"), Row(id="b", value="old", marker="[MACHINE]")]
    store = {"a": "AA", "b": "BB"}
    result = apply_inplace(rows, store, SPEC)
    by_id = {r.id: r for r in result.rows}
    assert by_id["a"].value == "AA" and MARK.is_marked(by_id["a"].marker)
    assert by_id["b"].value == "BB"
    assert set(result.written) == {"a", "b"}


def test_apply_never_overwrites_human_rows():
    # Populated value WITHOUT the marker == a human authored it: untouched.
    rows = [Row(id="h", value="human-authored")]
    store = {"h": "machine-would-write"}
    result = apply_inplace(rows, store, SPEC)
    assert result.rows[0].value == "human-authored"
    assert result.skipped_human == ["h"]
    assert result.written == []


def test_apply_skips_rows_the_store_has_no_value_for():
    rows = [Row(id="x", value="v", marker="[MACHINE]")]
    result = apply_inplace(rows, {}, SPEC)  # store has nothing for x
    assert result.skipped_no_value == ["x"]
    assert result.rows[0].value == "v"  # left verbatim


def test_apply_is_idempotent():
    rows = [Row(id="a")]
    store = {"a": "AA"}
    first = apply_inplace(rows, store, SPEC)
    second = apply_inplace(first.rows, store, SPEC)
    # Re-applying the same store yields the same rows; the value already matches,
    # but the row is still classified MACHINE and rewritten identically.
    assert second.rows[0].value == "AA"


def test_apply_policy_gate_holds_row():
    spec = replace(
        SPEC,
        eligible=lambda store, r: "excluded" if r.id == "no" else None,
    )
    rows = [Row(id="no"), Row(id="yes")]
    store = {"no": "N", "yes": "Y"}
    result = apply_inplace(rows, store, spec)
    assert result.skipped_policy == [("no", "excluded")]
    assert {r.id: r.value for r in result.rows} == {"no": "", "yes": "Y"}


# -- revert -------------------------------------------------------------------

def test_revert_strips_marked_rows_only():
    rows = [
        Row(id="m", value="v", marker="note [MACHINE]"),
        Row(id="h", value="human", marker=""),
    ]
    result = revert_marked(rows, SPEC)
    by_id = {r.id: r for r in result.rows}
    assert by_id["m"].value == "" and by_id["m"].marker == "note"
    assert by_id["h"].value == "human"  # human row untouched
    assert result.reverted == ["m"]  # exact mutated set


def test_revert_scoped_to_ids():
    rows = [
        Row(id="a", value="v", marker="[MACHINE]"),
        Row(id="b", value="v", marker="[MACHINE]"),
    ]
    result = revert_marked(rows, SPEC, only_ids=["a"])
    assert result.reverted == ["a"]  # b left alone despite its marker


# -- changeset choreography ---------------------------------------------------

@dataclass
class MockVcs:
    """Records the changeset choreography call sequence."""

    calls: list = field(default_factory=list)
    _cs_counter: int = 0

    def open_for_edit(self, path):
        self.calls.append(("open", path))

    def add(self, path):
        self.calls.append(("add", path))

    def make_changeset(self, description):
        self._cs_counter += 1
        self.calls.append(("make", description))
        return {"id": self._cs_counter, "paths": []}

    def move_into(self, changeset, paths):
        changeset["paths"].extend(paths)
        self.calls.append(("move", list(paths)))

    def finalize_description(self, changeset, description):
        changeset["description"] = description
        self.calls.append(("finalize", description))

    def revert(self, path):
        self.calls.append(("revert", path))

    def delete_if_empty(self, changeset):
        self.calls.append(("delete_if_empty", len(changeset["paths"])))


def test_changeset_choreography_sequence():
    vcs = MockVcs()
    written = []
    items = [{"id": "i1", "path": "a.txt"}, {"id": "i2", "path": "b.txt"}]

    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: f"delivered {len(moved)} files: "
        + ", ".join(p for _i, p in moved),
    )

    # Placeholder up front, then per-item open+move, then finalize+delete.
    kinds = [c[0] for c in vcs.calls]
    assert kinds[0] == "make"
    assert kinds[-2:] == ["finalize", "delete_if_empty"]
    assert written == ["a.txt", "b.txt"]
    assert [p for _i, p in result.moved] == ["a.txt", "b.txt"]
    # Description rebuilt from the moved subset.
    assert "a.txt" in result.description and "b.txt" in result.description


def test_changeset_description_rebuilt_from_moved_subset_only():
    # A failing apply keeps its item out of the moved subset AND out of the
    # description (the source description-vs-contents drift bug).
    vcs = MockVcs()

    def apply_item(it):
        if it["id"] == "bad":
            raise RuntimeError("write failed")

    items = [{"id": "good", "path": "g.txt"}, {"id": "bad", "path": "b.txt"}]
    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=apply_item,
        describe=lambda moved: ",".join(i for i, _p in moved),
    )
    assert [i for i, _p in result.moved] == ["good"]
    assert result.failed == [("bad", "write failed")]
    assert result.description == "good"  # "bad" never claimed


def test_changeset_empty_batch_finalizes_empty_and_delete_if_empty():
    vcs = MockVcs()
    result = deliver_changeset(
        [],
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: None,
        describe=lambda moved: "should not be called with content",
    )
    assert result.moved == []
    assert result.description == ""
    assert ("delete_if_empty", 0) in vcs.calls


def test_changeset_works_with_null_backend():
    # NullVcs makes deliver exercisable end-to-end without a real repo.
    written = []
    result = deliver_changeset(
        [{"id": "i1", "path": "a.txt"}],
        vcs=NullVcs(),
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: "desc",
    )
    assert written == ["a.txt"]
    assert result.changeset is None  # null backend returns no handle
    assert [p for _i, p in result.moved] == ["a.txt"]
