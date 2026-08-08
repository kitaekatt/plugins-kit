"""Tests for content_pipeline.store.attributed.

These cases pin scalar and block attribution behavior: a human block wins
wholesale even when one sub-field is empty; preserved fields carry human
overrides, machine blocks, and hashes across regeneration; orphaned authored
answers survive; and scalar precedence is human > machine > sourced. A field
carries a ``sourced`` / ``machine`` / ``human`` slice; a record carries human-authored
fields, machine blocks with a driving text, and a per-record hash.
"""

from content_pipeline.store.attributed import (
    AttributedField,
    CollectionMerge,
    MergePolicy,
    effective_value,
    merge_preserved_fields,
)


# -- effective_value: scalar precedence (localization translation_human) ------

def test_human_wins_over_machine():
    assert effective_value(machine="mt", human="edited") == "edited"


def test_machine_wins_over_sourced_when_no_human():
    assert effective_value(sourced="orig", machine="mt") == "mt"


def test_sourced_is_the_base_fallback():
    assert effective_value(sourced="orig") == "orig"


def test_empty_human_does_not_win():
    assert effective_value(machine="mt", human="") == "mt"


def test_none_slices_fall_through():
    assert effective_value(sourced=None, machine=None, human=None) is None


# -- effective_value: block precedence ---------------------------------------

def _block_present(block):
    return bool(block) and any(block.values())


def test_human_block_wins_wholesale():
    machine = {"body": "Wave", "face": "Happy"}
    human = {"body": "Bow", "face": "Sad"}
    assert effective_value(machine=machine, human=human, present=_block_present) == human


def test_human_partial_block_takes_ownership_even_with_empty_subfield():
    # Designer ownership: a human block with an empty body still wins wholesale,
    # so the empty body becomes the visible pick -- NOT the machine body.
    machine = {"body": "Wave", "face": "Happy"}
    human = {"body": "", "face": "Sad"}
    resolved = effective_value(machine=machine, human=human, present=_block_present)
    assert resolved is human
    assert resolved["body"] == ""
    assert resolved["face"] == "Sad"


def test_all_empty_human_block_does_not_win():
    machine = {"body": "Wave", "face": "Happy"}
    human = {"body": "", "face": ""}
    assert effective_value(machine=machine, human=human, present=_block_present) == machine


def test_machine_only_block():
    machine = {"body": "Wave", "face": "Happy"}
    assert effective_value(machine=machine, human=None, present=_block_present) == machine


# -- AttributedField ----------------------------------------------------------

def test_attributed_field_resolve():
    assert AttributedField(sourced="s", machine="m", human="h").resolve() == "h"
    assert AttributedField(sourced="s", machine="m").resolve() == "m"
    assert AttributedField(sourced="s").resolve() == "s"


# -- merge_preserved_fields: top level ----------------------------------------

def _text_unchanged(old, new):
    return old.get("text") == new.get("text")


TOP_POLICY = MergePolicy(
    human_fields=("direction_human",),
    conditional_fields=("direction_machine",),
    carry_fields=("inputs_hash",),
    unchanged=_text_unchanged,
)


def test_merge_returns_incoming_when_no_existing():
    incoming = {"direction_machine": "fresh"}
    assert merge_preserved_fields(None, incoming, policy=TOP_POLICY) is incoming


def test_merge_carries_human_override_forward():
    existing = {"direction_human": "hand-authored"}
    incoming = {"text": "t", "direction_machine": "fresh"}
    merged = merge_preserved_fields(existing, incoming, policy=TOP_POLICY)
    assert merged["direction_human"] == "hand-authored"


def test_merge_carries_machine_block_when_text_unchanged():
    existing = {"text": "same", "direction_machine": "old-machine"}
    incoming = {"text": "same", "direction_machine": "[pending]"}
    merged = merge_preserved_fields(existing, incoming, policy=TOP_POLICY)
    assert merged["direction_machine"] == "old-machine"


def test_merge_drops_machine_block_when_text_changed():
    existing = {"text": "old", "direction_machine": "old-machine"}
    incoming = {"text": "new", "direction_machine": "[pending]"}
    merged = merge_preserved_fields(existing, incoming, policy=TOP_POLICY)
    assert merged["direction_machine"] == "[pending]"


def test_merge_carries_hash_forward():
    # Without carrying the hash, the next freshness check reports stale on the
    # very next run even though nothing drifted.
    existing = {"text": "t", "inputs_hash": "abc123"}
    incoming = {"text": "t"}
    merged = merge_preserved_fields(existing, incoming, policy=TOP_POLICY)
    assert merged["inputs_hash"] == "abc123"


# -- merge_preserved_fields: keyed sub-collections ----------------------------

ITEM_MERGE = CollectionMerge(
    id_key="key",
    human_fields=("assignment_human",),
    carry_fields=("assignment_machine",),
    conditional_fields=("direction_machine",),
    unchanged=_text_unchanged,
)

COLL_POLICY = MergePolicy(collections={"items": ITEM_MERGE})


def test_item_human_override_and_verbatim_machine_carry():
    existing = {
        "items": [
            {
                "key": "a",
                "text": "same",
                "assignment_human": {"body": "Bow"},
                "assignment_machine": {"body": "Wave"},
                "direction_machine": "old-dir",
            }
        ]
    }
    incoming = {
        "items": [
            {"key": "a", "text": "same", "direction_machine": "[pending]"}
        ]
    }
    merged = merge_preserved_fields(existing, incoming, policy=COLL_POLICY)
    item = merged["items"][0]
    # Human override carried; machine block carried verbatim; conditional
    # machine field carried because text is unchanged.
    assert item["assignment_human"] == {"body": "Bow"}
    assert item["assignment_machine"] == {"body": "Wave"}
    assert item["direction_machine"] == "old-dir"


def test_item_conditional_dropped_when_text_changed():
    existing = {
        "items": [{"key": "a", "text": "old", "direction_machine": "old-dir"}]
    }
    incoming = {
        "items": [{"key": "a", "text": "new", "direction_machine": "[pending]"}]
    }
    merged = merge_preserved_fields(existing, incoming, policy=COLL_POLICY)
    assert merged["items"][0]["direction_machine"] == "[pending]"


def test_item_with_no_match_is_left_alone():
    existing = {"items": [{"key": "a", "assignment_human": {"body": "Bow"}}]}
    incoming = {"items": [{"key": "b", "text": "t"}]}
    merged = merge_preserved_fields(existing, incoming, policy=COLL_POLICY)
    keys = {i["key"] for i in merged["items"]}
    assert keys == {"b"}  # no keep_orphans_when -> orphan 'a' not retained


# -- orphan retention (questions with answers) --------------------------------

ANSWER_MERGE = CollectionMerge(
    id_key="id",
    human_fields=("answer",),
    keep_orphans_when=("answer",),
)
ANSWER_POLICY = MergePolicy(collections={"questions": ANSWER_MERGE})


def test_orphan_answer_retained():
    existing = {
        "questions": [
            {"id": "q1", "answer": "kept"},
            {"id": "q2"},  # no answer -> dropped when orphaned
        ]
    }
    incoming = {"questions": [{"id": "q3"}]}
    merged = merge_preserved_fields(existing, incoming, policy=ANSWER_POLICY)
    ids = {q["id"] for q in merged["questions"]}
    assert ids == {"q3", "q1"}
    kept = next(q for q in merged["questions"] if q["id"] == "q1")
    assert kept["answer"] == "kept"


def test_matched_answer_carried_forward():
    existing = {"questions": [{"id": "q1", "answer": "prior"}]}
    incoming = {"questions": [{"id": "q1"}]}
    merged = merge_preserved_fields(existing, incoming, policy=ANSWER_POLICY)
    assert merged["questions"][0]["answer"] == "prior"
