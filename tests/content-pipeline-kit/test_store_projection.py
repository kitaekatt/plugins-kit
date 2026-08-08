"""Tests for content_pipeline.store.projection.

These cases pin the canonical-store -> consumer-visible projection seam.
Projection computes the view (effective value / active candidate); it never
writes -- delivery lives in ``deliver``.
"""

from content_pipeline.store.candidate import Candidate, CandidateCell
from content_pipeline.store.projection import (
    GroupSlices,
    ProjectionSpec,
    project,
    project_cell,
    project_field,
)


# -- project_field ------------------------------------------------------------

def test_project_field_applies_precedence():
    assert project_field(sourced="s", machine="m", human="h") == "h"
    assert project_field(sourced="s", machine="m") == "m"


# -- project_cell -------------------------------------------------------------

def test_project_cell_returns_active_value():
    cell = CandidateCell(key=("u",), entries=(
        Candidate(id="c0", value="a", status="shadow"),
        Candidate(id="c1", value="b", status="active"),
    ))
    assert project_cell(cell) == "b"


def test_project_cell_default_when_none_or_no_active():
    assert project_cell(None, default="fallback") == "fallback"
    cell = CandidateCell(key=("u",), entries=(Candidate(id="c0", value="a", status="shadow"),))
    assert project_cell(cell, default="fallback") == "fallback"


# -- project (record reduction) -----------------------------------------------

def test_project_reduces_attributed_cells_and_passthrough():
    cell = CandidateCell(key=("u",), entries=(
        Candidate(id="c0", value="active-target", status="active"),
    ))
    record = {
        "direction_sourced": "orig",
        "direction_machine": "mt",
        "direction_human": "edited",
        "translation_cell": cell,
        "id": "unit-1",
    }
    spec = ProjectionSpec(
        attributed={
            "direction": GroupSlices(
                sourced="direction_sourced",
                machine="direction_machine",
                human="direction_human",
            )
        },
        cells={"translation": "translation_cell"},
        passthrough=("id",),
    )
    projected = project(record, spec)
    assert projected == {
        "direction": "edited",       # human wins
        "translation": "active-target",
        "id": "unit-1",
    }
    # None of the machinery leaks into the projected view.
    assert "direction_sourced" not in projected
    assert "translation_cell" not in projected


def test_project_machine_wins_when_no_human():
    record = {"d_machine": "mt", "d_human": ""}
    spec = ProjectionSpec(
        attributed={"d": GroupSlices(machine="d_machine", human="d_human")}
    )
    assert project(record, spec)["d"] == "mt"


def test_project_block_precedence_present_predicate():
    record = {
        "pick_machine": {"body": "Wave", "face": "Happy"},
        "pick_human": {"body": "", "face": "Sad"},
    }
    spec = ProjectionSpec(
        attributed={
            "pick": GroupSlices(
                machine="pick_machine",
                human="pick_human",
                present=lambda b: bool(b) and any(b.values()),
            )
        }
    )
    # Human block wins wholesale (designer ownership), empty body included.
    assert project(record, spec)["pick"] == {"body": "", "face": "Sad"}
