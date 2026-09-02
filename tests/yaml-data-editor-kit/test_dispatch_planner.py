"""Tests for the comment-to-work-unit planner."""

from pathlib import Path
import json
import os
from typing import Callable

import pytest

from content_pipeline.freshness.hashing import content_hash
from content_pipeline.llm import BackendOptions, CostBudget
from content_pipeline.llm.backends import MockBackend
from content_pipeline.pipeline.workunit import WorkUnit

from yaml_data_editor_kit.comments import Comment, CommentStore
from yaml_data_editor_kit.dispatch.planner import (
    CommentPlanStore,
    CommentPlanner,
    PlannerPolicy,
)
from yaml_data_editor_kit.dispatch.request import DispatchSelection
from yaml_data_editor_kit.schema import Corpus, Profile, load_corpus, load_profile

Writer = Callable[[str, str], Path]


def _catalogue(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> tuple[Profile, Corpus]:
    write(
        "profile/catalogue.yaml",
        """
dialect: type/1
id: product
identified_by: id
fields:
  id: { type: id }
  name: { type: string }
  summary: { type: text }
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
---
dialect: type/1
id: settings
fields:
  theme: { type: string }
---
dialect: source/1
of: settings
layout: single
path: content/settings.yaml
""",
    )
    write("content/products.yaml", "- { id: bolt, name: Bolt, summary: fastener }\n")
    write("content/settings.yaml", "theme: plain\n")
    profile = load_profile(profile_dir)
    return profile, load_corpus(profile, tmp_path)


def test_planner_collapses_record_comments_and_keeps_document_comments_separate(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [
        Comment.create(
            profile,
            corpus,
            id="summary-note",
            anchor="product/bolt/summary",
            text="Make the summary precise.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="name-note",
            anchor="product/bolt/name",
            text="Use the displayed name.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="doc-one",
            anchor="settings/@doc",
            text="Use the canonical theme.",
            created="2026-08-30",
        ),
        Comment.create(
            profile,
            corpus,
            id="doc-two",
            anchor="settings/@doc",
            text="Keep this document small.",
            created="2026-08-30",
        ),
    ]

    units = CommentPlanner().units(CommentPlanStore(profile, corpus, comments))

    assert [unit.id for unit in units] == [
        "comment:settings/@doc:doc-one",
        "comment:settings/@doc:doc-two",
        "record:product/bolt",
    ]
    record_unit = units[-1]
    assert record_unit.payload["comments"] == [
        "Use the displayed name.",
        "Make the summary precise.",
    ]
    assert record_unit.payload["anchored_slice"] == {
        "id": "bolt",
        "name": "Bolt",
        "summary": "fastener",
    }


def test_planner_ids_and_content_hashes_are_stable(
    tmp_path: Path, profile_dir: Path, write: Writer
) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = Comment.create(
        profile,
        corpus,
        id="note",
        anchor="product/bolt/name",
        text="Use the name.",
        created="2026-08-30",
    )
    store = CommentPlanStore(profile, corpus, [comment])

    first = CommentPlanner().units(store)
    second = CommentPlanner().units(store)

    assert [unit.id for unit in first] == [unit.id for unit in second]
    payload = first[0].payload
    assert payload["content_hash"] == content_hash(payload["anchored_slice"])
    assert len(payload["content_hash"]) == 16


def test_planner_ignores_resolved_comments(tmp_path: Path, profile_dir: Path, write: Writer) -> None:
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = Comment.create(
        profile,
        corpus,
        id="done",
        anchor="product/bolt",
        text="Already handled.",
        created="2026-08-30",
    )
    resolved = comment.__class__(
        id=comment.id,
        anchor=comment.anchor,
        text=comment.text,
        state="resolved",
        created=comment.created,
        guard=comment.guard,
        annotations=comment.annotations,
    )

    assert CommentPlanner().units(CommentPlanStore(profile, corpus, [resolved])) == []


def _planner_fixture(tmp_path: Path, profile_dir: Path, write: Writer, response: str, *, comments=None, backend=None, policy=None):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    if comments is None:
        comments = [Comment.create(profile, corpus, id="note", anchor="product/bolt/name", text="Use the name.", created="2026-08-30")]
    planner = CommentPlanner(profile, corpus, comments, backend=backend, policy=policy)
    return planner, CommentPlanStore(profile, corpus, comments), response


def test_no_configured_backend_returns_mechanical_plan_without_routing(tmp_path, profile_dir, write, monkeypatch):
    monkeypatch.delenv("CONTENT_PIPELINE_LLM_BACKEND", raising=False)
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "")
    assert planner.units(store)[0].id == "record:product/bolt"


def test_planner_prompt_is_canonical_json_with_anchored_slices(tmp_path, profile_dir, write):
    backend = MockBackend(responses=['{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"Use the name."}]}'])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    planner.units(store)
    payload = json.loads(backend.calls[0]["user"])
    assert payload["comments"][0]["anchored_slice"]["id"] == "bolt"


def test_planner_prompt_declares_the_exact_output_schema(tmp_path, profile_dir, write):
    backend = MockBackend(responses=['{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"Use the name."}]}'])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    planner.units(store)
    assert '"work_units":[{"comment_ids":["comment-id"],"instruction":"direct instruction"}]' in backend.calls[0]["system"]


def test_agentic_response_groups_complete_mechanical_units(tmp_path, profile_dir, write):
    response = '{"schema_version":"1","work_units":[{"comment_ids":["name-note","summary-note"],"instruction":"Apply both."}]}'
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [Comment.create(profile, corpus, id=i, anchor="product/bolt/" + field, text=i, created="x") for i, field in (("name-note", "name"), ("summary-note", "summary"))]
    backend = MockBackend(responses=[response])
    units = CommentPlanner(profile, corpus, comments, backend=backend).units(CommentPlanStore(profile, corpus, comments))
    assert units[0].payload["comment_ids"] == ["name-note", "summary-note"]


def test_agentic_response_order_does_not_change_ids_or_target_order(tmp_path, profile_dir, write):
    response = '{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"Use it."}]}'
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, response, backend=MockBackend(responses=[response]))
    first = planner.units(store)[0]
    planner.backend = MockBackend(responses=[response])
    second = planner.units(store)[0]
    assert first.id == second.id and first.payload["targets"][0]["id"] == second.payload["targets"][0]["id"]


def test_invalid_then_valid_response_uses_cpk_validation_retry(tmp_path, profile_dir, write):
    valid = '{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"Use it."}]}'
    backend = MockBackend(responses=['{}', valid])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, valid, backend=backend)
    assert planner.units(store)[0].id.startswith("group:") and len(backend.calls) == 2


def test_exhausted_invalid_responses_fall_back_to_complete_mechanical_plan(tmp_path, profile_dir, write):
    backend = MockBackend(responses=["{}", "{}", "{}"])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    assert planner.units(store)[0].id == "record:product/bolt"


def test_duplicate_json_keys_and_extra_keys_fall_back(tmp_path, profile_dir, write):
    response = '{"schema_version":"1","work_units":[],"x":1}'
    backend = MockBackend(responses=[response] * 3)
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    assert planner.units(store)[0].id == "record:product/bolt"


def test_wrong_schema_blank_instruction_and_empty_groups_fall_back(tmp_path, profile_dir, write):
    backend = MockBackend(responses=['{"schema_version":"2","work_units":[]}'] * 3)
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    assert len(planner.units(store)) == 1


def test_grouping_with_missing_duplicate_or_unknown_comment_id_falls_back(tmp_path, profile_dir, write):
    response = '{"schema_version":"1","work_units":[{"comment_ids":["unknown"],"instruction":"x"}]}'
    backend = MockBackend(responses=[response] * 3)
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    assert planner.units(store)[0].id == "record:product/bolt"


def test_grouping_that_splits_a_mechanical_unit_falls_back(tmp_path, profile_dir, write):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [Comment.create(profile, corpus, id=i, anchor="product/bolt/" + field, text=i, created="x") for i, field in (("a", "name"), ("b", "summary"))]
    response = '{"schema_version":"1","work_units":[{"comment_ids":["a"],"instruction":"a"},{"comment_ids":["b"],"instruction":"b"}]}'
    planner = CommentPlanner(profile, corpus, comments, backend=MockBackend(responses=[response] * 3))
    assert planner.units(CommentPlanStore(profile, corpus, comments))[0].id == "record:product/bolt"


def test_grouping_may_merge_whole_base_units_with_distinct_write_anchors(tmp_path, profile_dir, write):
    """Merging is the point of agentic grouping: two base units, one work unit, two targets."""
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [
        Comment.create(profile, corpus, id="bolt-note", anchor="product/bolt", text="x", created="x"),
        Comment.create(profile, corpus, id="theme-note", anchor="settings", text="y", created="x"),
    ]
    response = '{"schema_version":"1","work_units":[{"comment_ids":["bolt-note","theme-note"],"instruction":"Apply both."}]}'
    planner = CommentPlanner(profile, corpus, comments, backend=MockBackend(responses=[response]))
    units = planner.units(CommentPlanStore(profile, corpus, comments))

    assert len(units) == 1
    assert units[0].id.startswith("group:")
    assert units[0].payload["comment_ids"] == ["bolt-note", "theme-note"]
    anchors = {target["anchor"] for target in units[0].payload["targets"]}
    assert len(units[0].payload["targets"]) == 2 and len(anchors) == 2


def test_grouping_that_takes_only_part_of_a_base_unit_falls_back(tmp_path, profile_dir, write):
    """A base unit must be taken whole -- no worker sees part of a record's comments."""
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comments = [
        Comment.create(profile, corpus, id="name-note", anchor="product/bolt/name", text="x", created="x"),
        Comment.create(profile, corpus, id="summary-note", anchor="product/bolt/summary", text="y", created="x"),
        Comment.create(profile, corpus, id="theme-note", anchor="settings", text="z", created="x"),
    ]
    # name-note and summary-note share one base unit; this response tears them apart.
    response = (
        '{"schema_version":"1","work_units":['
        '{"comment_ids":["name-note","theme-note"],"instruction":"a"},'
        '{"comment_ids":["summary-note"],"instruction":"b"}]}'
    )
    planner = CommentPlanner(profile, corpus, comments, backend=MockBackend(responses=[response] * 3))
    units = planner.units(CommentPlanStore(profile, corpus, comments))

    assert {unit.id for unit in units} == {"record:product/bolt", "comment:settings:theme-note"}
    assert not any(unit.id.startswith("group:") for unit in units)


def test_parse_grouping_rejects_two_base_units_sharing_a_write_anchor() -> None:
    """Defensive guard: mechanical grouping keys by write anchor, so it cannot itself
    emit two base units sharing one -- but the applier's one-write-per-anchor rule
    depends on it, so parse_grouping enforces it directly."""
    from yaml_data_editor_kit.dispatch.planner import parse_grouping

    shared = {"anchor": "product/bolt", "anchored_slice": {}, "comment_anchors": [], "comments": []}
    mechanical = [
        WorkUnit(id="record:one", payload=dict(shared, comment_ids=["a"])),
        WorkUnit(id="record:two", payload=dict(shared, comment_ids=["b"])),
    ]
    response = '{"schema_version":"1","work_units":[{"comment_ids":["a","b"],"instruction":"both"}]}'

    with pytest.raises(ValueError, match="duplicate write anchors"):
        parse_grouping(response, mechanical)


def test_unserializable_anchored_slice_falls_back(tmp_path, profile_dir, write):
    from yaml_data_editor_kit.dispatch import planner as planner_module
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    comment = Comment.create(profile, corpus, id="note", anchor="product/bolt", text="x", created="x")
    original = planner_module.plain_value
    planner_module.plain_value = lambda value: (_ for _ in ()).throw(TypeError("bad key"))
    try:
        planner = CommentPlanner(profile, corpus, [comment], backend=MockBackend())
        assert planner.units(CommentPlanStore(profile, corpus, [comment]))[0].id == "record:product/bolt"
    finally:
        planner_module.plain_value = original


def test_backend_or_budget_error_propagates_instead_of_falling_back(tmp_path, profile_dir, write):
    backend = MockBackend(responses=[RuntimeError("transport")])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    import pytest
    with pytest.raises(RuntimeError, match="transport"):
        planner.units(store)


def test_live_backend_without_a_resolved_model_is_rejected_before_call(tmp_path, profile_dir, write, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_BACKEND", "custom")
    class Backend:
        name = "custom"
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=Backend())
    import pytest
    with pytest.raises(ValueError, match="requires PlannerPolicy.model"):
        planner.units(store)


def test_planner_policy_forwards_cpk_cache_budget_retry_and_options(tmp_path, profile_dir, write):
    backend = MockBackend(responses=['{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"x"}]}'])
    budget = CostBudget()
    options = BackendOptions(max_tokens=12)
    policy = PlannerPolicy(options=options, cache_dir=tmp_path / "cache", cost_budget=budget, retries=2, retry_sleep=0.1)
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend, policy=policy)
    planner.units(store)
    assert backend.calls[0]["options"] == options


def test_open_question_blocks_only_its_write_anchor(tmp_path, profile_dir, write):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(profile, corpus, id="q", anchor="product/bolt", text="Which?", created="x")
    other = Comment.create(profile, corpus, id="other", anchor="settings/@doc", text="Set it.", created="x")
    assert [u.id for u in CommentPlanner(profile, corpus, [question, other]).units(CommentPlanStore(profile, corpus, [question, other]))] == ["comment:settings/@doc:other"]


def test_explicit_instruction_selection_cannot_bypass_open_question(tmp_path, profile_dir, write):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(profile, corpus, id="q", anchor="product/bolt", text="Which?", created="x")
    instruction = Comment.create(profile, corpus, id="note", anchor="product/bolt/name", text="Use it.", created="x")
    selection = DispatchSelection(comment_ids=("note",))
    assert CommentPlanner(profile, corpus, [question, instruction], selection).units(CommentPlanStore(profile, corpus, [question, instruction], selection)) == []


def test_resolved_question_unblocks_and_supplies_ruling_context(tmp_path, profile_dir, write):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    question = Comment.create_question(profile, corpus, id="q", anchor="product/bolt", text="Which?", created="x")
    resolved = CommentStore(tmp_path / "comments")
    resolved.root.mkdir()
    question = CommentStore.rule(resolved, question, "The displayed value.")
    instruction = Comment.create(profile, corpus, id="note", anchor="product/bolt/name", text="Use it.", created="x")
    unit = CommentPlanner(profile, corpus, [question, instruction]).units(CommentPlanStore(profile, corpus, [question, instruction]))[0]
    assert unit.payload["rulings"][0]["ruling"] == "The displayed value."


def test_resolved_rulings_are_sorted_and_guarded(tmp_path, profile_dir, write):
    profile, corpus = _catalogue(tmp_path, profile_dir, write)
    questions = [Comment.create_question(profile, corpus, id=i, anchor="product/bolt", text=i, created="x") for i in ("z", "a")]
    questions = [Comment(**{**q.__dict__, "state": "resolved", "ruling": "r"}) for q in questions]
    instruction = Comment.create(profile, corpus, id="note", anchor="product/bolt/name", text="x", created="x")
    payload = CommentPlanner(profile, corpus, [*questions, instruction]).units(CommentPlanStore(profile, corpus, [*questions, instruction]))[0].payload
    assert [item["question_id"] for item in payload["rulings"]] == ["a", "z"] and all(item["guard"] for item in payload["rulings"])


def test_injected_mock_wins_when_live_backend_environment_is_set(tmp_path, profile_dir, write, monkeypatch):
    monkeypatch.setenv("CONTENT_PIPELINE_LLM_BACKEND", "openrouter")
    backend = MockBackend(responses=['{"schema_version":"1","work_units":[{"comment_ids":["note"],"instruction":"x"}]}'])
    planner, store, _ = _planner_fixture(tmp_path, profile_dir, write, "", backend=backend)
    assert planner.units(store)[0].id.startswith("group:") and len(backend.calls) == 1
