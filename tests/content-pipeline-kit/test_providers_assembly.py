"""Behavioral tests for content_pipeline.providers.assembly.

Covers ordered-block assembly with conditional inclusion, a configurable
slot-syntax tokenizer, and label indirection that hides real keys from the
model.
"""

import pytest

from content_pipeline.providers.assembly import (
    Block,
    SlotSyntax,
    assemble_blocks,
    assign_labels,
    invert_labels,
    relabel,
)


# --- ordered named blocks ----------------------------------------------------


def test_assemble_blocks_joins_in_order():
    blocks = [Block("a", "AAA"), Block("b", "BBB")]
    assert assemble_blocks(blocks) == "AAA\n\nBBB"


def test_assemble_blocks_drops_excluded():
    blocks = [Block("a", "AAA"), Block("b", "BBB", include=False), Block("c", "CCC")]
    assert assemble_blocks(blocks) == "AAA\n\nCCC"


def test_assemble_blocks_drops_blank_bodies():
    blocks = [Block("a", "AAA"), Block("b", "   \n  "), Block("c", "CCC")]
    assert assemble_blocks(blocks) == "AAA\n\nCCC"


def test_assemble_blocks_custom_separator():
    blocks = [Block("a", "1"), Block("b", "2")]
    assert assemble_blocks(blocks, separator="\n---\n") == "1\n---\n2"


def test_assemble_blocks_all_empty_is_empty_string():
    assert assemble_blocks([Block("a", ""), Block("b", "", include=False)]) == ""


# --- slot syntax -------------------------------------------------------------


def test_slot_default_dialect():
    s = SlotSyntax()
    assert s.slot("glossary") == "${glossary}"


def test_parse_slots_left_to_right_with_duplicates():
    s = SlotSyntax()
    assert s.parse_slots("${a} and ${b} and ${a}") == ("a", "b", "a")


def test_parse_slots_multiword_name():
    s = SlotSyntax()
    assert s.parse_slots("${Baby Blue}") == ("Baby Blue",)


def test_has_any_slot():
    s = SlotSyntax()
    assert s.has_any_slot("hi ${x}")
    assert not s.has_any_slot("no slots here")


def test_render_via_lookup():
    s = SlotSyntax()
    out = s.render("${greeting}, ${name}!", lambda n: {"greeting": "Hi", "name": "Ana"}[n])
    assert out == "Hi, Ana!"


def test_render_map_strict_raises_on_unknown():
    s = SlotSyntax()
    with pytest.raises(KeyError):
        s.render_map("${missing}", {"present": "x"})


def test_render_map_non_strict_leaves_unknown():
    s = SlotSyntax()
    assert s.render_map("${missing} ${here}", {"here": "H"}, strict=False) == "${missing} H"


def test_custom_delimiters():
    s = SlotSyntax("<<", ">>")
    assert s.slot("x") == "<<x>>"
    assert s.parse_slots("a <<foo>> b <<bar>>") == ("foo", "bar")
    assert s.render_map("<<a>>-<<b>>", {"a": "1", "b": "2"}) == "1-2"


def test_empty_delimiter_rejected():
    with pytest.raises(ValueError):
        SlotSyntax("", "}")


# --- label indirection -------------------------------------------------------


def test_assign_labels_sequential_in_order():
    labels = assign_labels(["k-06a", "k-06b", "k-06c"])
    assert labels == {"k-06a": "item_1", "k-06b": "item_2", "k-06c": "item_3"}


def test_assign_labels_custom_prefix_and_start():
    labels = assign_labels(["x", "y"], prefix="line_", start=0)
    assert labels == {"x": "line_0", "y": "line_1"}


def test_invert_labels():
    lbl = {"k1": "item_1", "k2": "item_2"}
    assert invert_labels(lbl) == {"item_1": "k1", "item_2": "k2"}


def test_invert_labels_duplicate_raises():
    with pytest.raises(ValueError):
        invert_labels({"k1": "item_1", "k2": "item_1"})


def test_relabel_response_back_to_keys():
    label_by_key = assign_labels(["real-a", "real-b"])
    agent_response = {"item_2": "picked B", "item_1": "picked A"}
    assert relabel(agent_response, label_by_key) == {"real-a": "picked A", "real-b": "picked B"}


def test_relabel_strict_rejects_invented_label():
    label_by_key = assign_labels(["real-a"])
    with pytest.raises(KeyError):
        relabel({"item_99": "hallucinated"}, label_by_key)


def test_relabel_non_strict_drops_invented_label():
    label_by_key = assign_labels(["real-a"])
    out = relabel({"item_1": "ok", "item_99": "drop"}, label_by_key, strict=False)
    assert out == {"real-a": "ok"}


def test_label_roundtrip_full_cycle():
    keys = ["conv-1", "conv-2", "conv-3"]
    label_by_key = assign_labels(keys)
    # Agent replies keyed by opaque label; we translate back.
    reply = {label_by_key[k]: f"pick-{k}" for k in keys}
    restored = relabel(reply, label_by_key)
    assert restored == {k: f"pick-{k}" for k in keys}
