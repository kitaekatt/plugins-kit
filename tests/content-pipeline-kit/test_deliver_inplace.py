"""Tests for content_pipeline.deliver.inplace.

Pins marker, apply, and revert behavior against a neutral row shape:

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


def test_marker_matches_whole_token_not_substring():
    # A tag that is a substring of authored text ("gen" inside "regen") must
    # NOT classify as machine-owned -- the tag is matched as a whole token.
    gen_mark = Marker("gen")
    assert gen_mark.is_marked("regen pending") is False
    assert classify_ownership(True, "regen pending", gen_mark) is Ownership.HUMAN
    # It survives a revert pass unchanged (never touched, since unmarked).
    row = Row(id="h", value="human value", marker="regen pending")
    spec = replace(SPEC, marker=gen_mark)
    result = revert_marked([row], spec)
    assert result.reverted == []
    assert result.rows[0].marker == "regen pending"
    assert result.rows[0].value == "human value"


def test_marker_write_then_revert_round_trips_double_spaces_byte_for_byte():
    # remove() must delete exactly the tag token and one adjacent separator,
    # leaving the rest of the text byte-for-byte untouched -- including an
    # authored double space nowhere near the tag.
    original = "a  b"  # double space between "a" and "b"
    marked = MARK.add(original)
    assert marked == "a  b [MACHINE]"
    restored = MARK.remove(marked)
    assert restored == original


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


def test_apply_reapply_over_unchanged_marked_rows_writes_nothing():
    # ApplyResult.mutated_ids is documented as "the exact write set" -- a row
    # already marked and holding the store's current value must not be
    # rewritten (and so must not appear in written / mutated_ids) on a
    # no-op re-apply. This is the property a git-backed deliver relies on so
    # a second run over unchanged content stages nothing.
    rows = [Row(id="a")]
    store = {"a": "AA"}
    first = apply_inplace(rows, store, SPEC)
    assert first.written == ["a"]

    second = apply_inplace(first.rows, store, SPEC)
    assert second.written == []
    assert second.mutated_ids == []
    assert second.rows == first.rows  # byte-for-byte unchanged


def test_apply_reapply_with_in_place_setters_still_detects_the_diff():
    # A caller whose set_value / set_marker mutate the row in place and return
    # the same object must still get an exact write set: the first apply
    # writes, the no-op second apply writes nothing.
    @dataclass
    class MutableRow:
        id: str
        value: str = ""
        marker: str = ""

    def set_value_in_place(row, value):
        row.value = value
        return row

    def set_marker_in_place(row):
        row.marker = MARK.add(row.marker)
        return row

    spec = replace(SPEC, set_value=set_value_in_place, set_marker=set_marker_in_place)
    rows = [MutableRow(id="a")]
    store = {"a": "AA"}
    first = apply_inplace(rows, store, spec)
    assert first.written == ["a"]
    second = apply_inplace(first.rows, store, spec)
    assert second.written == []


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


def test_changeset_collects_move_failure_and_continues():
    # A vcs.move_into failure on one item is recorded on failed_moves and the
    # batch continues; the description is rebuilt from the moved subset only.
    class FlakyMoveVcs(MockVcs):
        def move_into(self, changeset, paths):
            if "b.txt" in paths:
                raise RuntimeError("reopen no-op: not open for edit")
            super().move_into(changeset, paths)

    vcs = FlakyMoveVcs()
    written = []
    items = [
        {"id": "a", "path": "a.txt"},
        {"id": "b", "path": "b.txt"},  # move fails
        {"id": "c", "path": "c.txt"},
    ]
    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: ",".join(i for i, _p in moved),
    )

    # All three applied (writes happened) but only a + c moved.
    assert written == ["a.txt", "b.txt", "c.txt"]
    assert [i for i, _p in result.moved] == ["a", "c"]
    assert result.failed_moves == [("b", "b.txt", "reopen no-op: not open for edit")]
    # Description rebuilt from the moved subset only -- "b" never claimed.
    assert result.description == "a,c"
    # Batch was NOT aborted: finalize + delete_if_empty still ran.
    kinds = [c[0] for c in vcs.calls]
    assert kinds[-2:] == ["finalize", "delete_if_empty"]


def test_changeset_collects_open_for_edit_failure():
    # An open_for_edit failure is also a per-item vcs failure: recorded, skipped,
    # and apply_item is NOT called for that item.
    class FlakyOpenVcs(MockVcs):
        def open_for_edit(self, path):
            if path == "b.txt":
                raise RuntimeError("cannot open for edit")
            super().open_for_edit(path)

    vcs = FlakyOpenVcs()
    written = []
    items = [{"id": "a", "path": "a.txt"}, {"id": "b", "path": "b.txt"}]
    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: ",".join(i for i, _p in moved),
    )
    assert written == ["a.txt"]  # b's apply skipped (open failed first)
    assert [i for i, _p in result.moved] == ["a"]
    assert result.failed_moves == [("b", "b.txt", "cannot open for edit")]


def test_changeset_all_moves_fail_deletes_empty_cl():
    # Every move fails -> nothing moved -> empty description -> delete_if_empty.
    class AllFailVcs(MockVcs):
        def move_into(self, changeset, paths):
            raise RuntimeError("move failed")

    vcs = AllFailVcs()
    result = deliver_changeset(
        [{"id": "a", "path": "a.txt"}],
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: None,
        describe=lambda moved: "unused",
    )
    assert result.moved == []
    assert result.failed_moves == [("a", "a.txt", "move failed")]
    assert result.description == ""
    assert ("delete_if_empty", 0) in vcs.calls


def test_changeset_apply_failure_reverts_the_open_item():
    # An apply failure after open_for_edit succeeded must revert the item so
    # a p4-backed run does not leave the file checked out. A revert failure
    # must not mask the original apply failure.
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
    assert result.failed == [("bad", "write failed")]
    assert ("revert", "b.txt") in vcs.calls
    # The good item was never reverted.
    assert ("revert", "g.txt") not in vcs.calls
    # GitVcs / NullVcs (real revert, no failure) is unaffected: the batch
    # still proceeds and finalizes normally.
    assert [i for i, _p in result.moved] == ["good"]


def test_changeset_apply_failure_revert_failure_does_not_mask_apply_failure():
    class RevertBoomVcs(MockVcs):
        def revert(self, path):
            super().revert(path)
            raise RuntimeError("revert boom")

    vcs = RevertBoomVcs()

    def apply_item(it):
        raise RuntimeError("write failed")

    items = [{"id": "bad", "path": "b.txt"}]
    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=apply_item,
        describe=lambda moved: "",
    )
    # The apply failure is still recorded, not replaced by the revert error.
    assert result.failed == [("bad", "write failed")]
    assert ("revert", "b.txt") in vcs.calls


def test_changeset_apply_and_move_failures_are_separate_buckets():
    # apply failures -> failed; vcs failures -> failed_moves; the two do not mix.
    class FlakyMoveVcs(MockVcs):
        def move_into(self, changeset, paths):
            if "m.txt" in paths:
                raise RuntimeError("move boom")
            super().move_into(changeset, paths)

    def apply_item(it):
        if it["id"] == "apply_bad":
            raise RuntimeError("write boom")

    vcs = FlakyMoveVcs()
    items = [
        {"id": "ok", "path": "ok.txt"},
        {"id": "apply_bad", "path": "a.txt"},
        {"id": "move_bad", "path": "m.txt"},
    ]
    result = deliver_changeset(
        items,
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=apply_item,
        describe=lambda moved: ",".join(i for i, _p in moved),
    )
    assert [i for i, _p in result.moved] == ["ok"]
    assert result.failed == [("apply_bad", "write boom")]
    assert result.failed_moves == [("move_bad", "m.txt", "move boom")]
    assert result.description == "ok"


def test_adopts_an_existing_changeset_instead_of_minting():
    """``changeset=`` supplies a pending changeset; nothing new is minted.

    The VcsBackend protocol is modeled on Perforce's pending changelist, so a
    long-lived adoptable changeset is the central object in that model -- a
    caller running several delivery passes into ONE reviewable CL could not
    express that while ``deliver_changeset`` always minted its own.
    """
    vcs = MockVcs()
    existing = vcs.make_changeset("my long-lived CL")
    vcs.calls.clear()
    written = []

    result = deliver_changeset(
        [{"id": "i1", "path": "a.txt"}, {"id": "i2", "path": "b.txt"}],
        vcs=vcs,
        changeset=existing,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda it: written.append(it["path"]),
        describe=lambda moved: f"delivered {len(moved)}",
    )

    assert "make" not in [c[0] for c in vcs.calls]
    assert result.changeset is existing
    assert written == ["a.txt", "b.txt"]
    assert [p for _i, p in result.moved] == ["a.txt", "b.txt"]
    assert existing["paths"] == ["a.txt", "b.txt"]
    # Everything else about the choreography is unchanged.
    assert result.description == "delivered 2"
    assert existing["description"] == "delivered 2"


def test_adopted_changeset_that_receives_nothing_is_left_untouched():
    """An adopted changeset the caller owns is never finalized or deleted empty.

    Both would be destructive on a real pending changelist: the empty check is
    scoped to THIS run's moves (not the changeset's actual contents), and
    finalizing with the empty description of a no-op run would blank a
    description the caller wrote.
    """
    vcs = MockVcs()
    existing = vcs.make_changeset("caller's description")
    vcs.calls.clear()

    result = deliver_changeset(
        [],
        vcs=vcs,
        changeset=existing,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda _it: None,
        describe=lambda moved: "should not be called",
    )

    assert vcs.calls == []
    assert result.moved == []
    assert result.description == ""
    # finalize_description never ran, so the changeset carries no description
    # written by this pass -- the caller's own stays authoritative.
    assert "description" not in existing


def test_minting_path_still_finalizes_and_deletes_when_empty():
    """The default (no ``changeset=``) path is unchanged."""
    vcs = MockVcs()
    result = deliver_changeset(
        [],
        vcs=vcs,
        item_id=lambda it: it["id"],
        path_of=lambda it: it["path"],
        apply_item=lambda _it: None,
        describe=lambda moved: "unused",
    )
    assert [c[0] for c in vcs.calls] == ["make", "finalize", "delete_if_empty"]
    assert result.description == ""


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
