"""Tests for content_pipeline.roundtrip.returns.

Translates the workbook export / intake behaviors with pluggable serialize /
parse callables (the xlsx specifics stay project-side): export a review
snapshot, intake only the rows a human corrected, surface corrections as
human-attributed values, and apply them.
"""

from content_pipeline.roundtrip.returns import (
    Correction,
    ReviewRow,
    apply_corrections,
    export_for_review,
    intake_corrections,
)


def test_export_projects_entities_and_serializes():
    written = {}
    entities = [
        {"id": "e1", "src": "hello", "value": "machine1"},
        {"id": "e2", "src": "world", "value": "machine2"},
    ]
    rows = export_for_review(
        entities,
        "dest",
        to_row=lambda e: ReviewRow(
            entity_id=e["id"], fields={"src": e["src"], "value": e["value"]}
        ),
        serialize=lambda dest, rows: written.__setitem__(dest, rows),
    )
    assert [r.entity_id for r in rows] == ["e1", "e2"]
    assert written["dest"][0].fields["src"] == "hello"


def test_intake_keeps_only_corrected_rows():
    # A "returned" workbook: only e2 carries a filled correction column.
    returned = [
        {"id": "e1", "value": "machine1", "corrected": ""},
        {"id": "e2", "value": "machine2", "corrected": "human-fix"},
    ]

    def to_correction(row):
        if row["corrected"].strip():
            return Correction(entity_id=row["id"], value=row["corrected"])
        return None  # untouched export value -- not a correction

    corrections = intake_corrections(
        "return.xlsx",
        parse=lambda src: returned,
        to_correction=to_correction,
    )
    assert len(corrections) == 1
    assert corrections[0].entity_id == "e2"
    assert corrections[0].value == "human-fix"
    assert corrections[0].attribution == "human"  # lands on the human slice


def test_apply_corrections_writes_each():
    written = []
    corrections = [
        Correction(entity_id="e1", value="a"),
        Correction(entity_id="e2", value="b"),
    ]
    count = apply_corrections(
        corrections, write=lambda c: written.append((c.entity_id, c.value))
    )
    assert count == 2
    assert written == [("e1", "a"), ("e2", "b")]


def test_correction_carries_meta():
    c = Correction(
        entity_id="e", value="v", meta={"category": "tone", "comment": "softer"}
    )
    assert c.meta["category"] == "tone"
