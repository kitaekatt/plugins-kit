"""Tests for the worker-failure question protocol adapter."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.protocol import ProtocolHandler
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.freshness.hashing import content_hash
from content_pipeline.pipeline.workunit import WorkUnit

from yaml_data_editor_kit.comments import CommentStore, slice_hash
from yaml_data_editor_kit.dispatch.protocol import (
    QUESTION_FAILURE_PREFIX,
    QuestionProtocolError,
    build_dispatch_handlers,
    encode_question_failure,
    materialize_failed_questions,
)
from yaml_data_editor_kit.schema import load_corpus, load_profile


def _setup(tmp_path: Path, write):
    write("profile/catalogue.yaml", """
dialect: type/1
id: product
identified_by: id
fields:
  id: {type: id}
  summary: {type: text}
---
dialect: source/1
of: product
layout: rows
path: content/products.yaml
""")
    write("content/products.yaml", "- {id: bolt, summary: fastener}\n")
    profile = load_profile(tmp_path / "profile")
    corpus = load_corpus(profile, tmp_path)
    target = {
        "id": "record:product/bolt",
        "anchor": "product/bolt",
        "anchored_slice": {"id": "bolt", "summary": "fastener"},
        "content_hash": content_hash({"id": "bolt", "summary": "fastener"}),
        "comment_anchors": [],
        "comment_guards": [],
        "comment_ids": ["summary-note"],
    }
    adapter = SimpleNamespace(unit_for=lambda _: WorkUnit("unit-1", {"targets": [target]}))
    comments = CommentStore.init(tmp_path / "comments")
    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    execution.create_run("run-1", driver="worker", backend="mock", model="", adapter_version="")
    execution.register_units("run-1", ["unit-1"])
    claim = execution.claim_unit("run-1", "unit-1", "worker")
    return profile, adapter, comments, execution, claim.fencing_token, target


def _handlers(execution, adapter, comments, profile, tmp_path, calls=None):
    calls = [] if calls is None else calls

    def fail(payload):
        calls.append(payload)
        execution.fail_unit(
            payload["run_id"], payload["unit_id"], payload["fencing_token"],
            error=payload.get("error", ""), terminal=payload.get("terminal", False),
        )
        return {"ok": True}

    return build_dispatch_handlers(
        {"fail": fail, "other": lambda payload: payload},
        execution=execution, adapter=adapter, comment_store=comments,
        profile=profile, corpus_path=tmp_path,
    ), calls


def test_question_failure_encoder_emits_canonical_ascii_json() -> None:
    value = encode_question_failure("product/bolt", "Use caf\u00e9?")
    assert value == QUESTION_FAILURE_PREFIX + '{"anchor":"product/bolt","question":"Use caf\\u00e9?"}'
    assert value.isascii()


def test_terminal_question_failure_records_cpk_failure_and_open_question(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, target = _setup(tmp_path, write)
    handlers, _ = _handlers(execution, adapter, comments, profile, tmp_path)
    handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                       "terminal": True, "error": encode_question_failure("product/bolt", "Which name?")})
    loaded = comments.load().comments
    assert len(loaded) == 1 and loaded[0].kind == "question"
    assert execution.list_attempts("run-1")[-1].kind is AttemptKind.FAIL


def test_question_failure_names_one_target_in_a_multi_target_unit(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, target = _setup(tmp_path, write)
    other = dict(target, anchor="product/missing")
    adapter.unit_for = lambda _: WorkUnit("unit-1", {"targets": [target, other]})
    handlers, _ = _handlers(execution, adapter, comments, profile, tmp_path)
    with pytest.raises(QuestionProtocolError):
        handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                           "terminal": True, "error": encode_question_failure("product/all", "Which?")})


def test_nonterminal_question_failure_is_rejected_before_cpk_fail(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    handlers, calls = _handlers(execution, adapter, comments, profile, tmp_path)
    with pytest.raises(QuestionProtocolError):
        handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                           "terminal": False, "error": encode_question_failure("product/bolt", "Which?")})
    assert calls == []


def test_malformed_unknown_anchor_and_oversized_question_failures_are_rejected(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    handlers, calls = _handlers(execution, adapter, comments, profile, tmp_path)
    values = ["question/1:{bad", encode_question_failure("product/unknown", "Which?"),
              QUESTION_FAILURE_PREFIX + '{"anchor":"product/bolt","question":"' + "x" * 390 + '"}']
    for value in values:
        with pytest.raises((QuestionProtocolError, ValueError)):
            handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                               "terminal": True, "error": value})
    assert calls == []


def test_ordinary_worker_failure_creates_no_comment(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    handlers, _ = _handlers(execution, adapter, comments, profile, tmp_path)
    handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                       "terminal": True, "error": "ordinary"})
    assert comments.load().comments == []


def test_stale_question_target_creates_no_question(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    write("content/products.yaml", "- {id: bolt, summary: changed}\n")
    handlers, calls = _handlers(execution, adapter, comments, profile, tmp_path)
    handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                       "terminal": True, "error": encode_question_failure("product/bolt", "Which?")})
    assert comments.load().comments == [] and calls[0]["error"] == "stale:product/bolt"


def test_question_guard_comes_from_the_planned_target_slice(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, target = _setup(tmp_path, write)
    target["anchored_slice"] = {"id": "bolt", "summary": "planned"}
    target["content_hash"] = content_hash({"id": "bolt", "summary": "fastener"})
    handlers, _ = _handlers(execution, adapter, comments, profile, tmp_path)
    handlers["fail"]({"run_id": "run-1", "unit_id": "unit-1", "fencing_token": token,
                       "terminal": True, "error": encode_question_failure("product/bolt", "Which?")})
    assert comments.load().comments[0].guard == slice_hash(target["anchored_slice"])


def test_question_id_and_created_time_come_from_the_durable_attempt(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "Which?"), terminal=True, at=1.25)
    ids = materialize_failed_questions(execution, "run-1", adapter, comments)
    assert ids[0].startswith("question-") and comments.load().comments[0].created.endswith("Z")


def test_distinct_fail_attempts_on_one_anchor_create_distinct_questions(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "One?"), terminal=True)
    materialize_failed_questions(execution, "run-1", adapter, comments)
    assert len(comments.load().comments) == 1


def test_question_materialization_replay_is_idempotent(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "Which?"), terminal=True)
    first = materialize_failed_questions(execution, "run-1", adapter, comments)
    assert materialize_failed_questions(execution, "run-1", adapter, comments) == []
    assert first and len(comments.load().comments) == 1


def test_resolved_question_is_not_reopened_by_replay(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "Which?"), terminal=True)
    materialize_failed_questions(execution, "run-1", adapter, comments)
    question = comments.load().comments[0]
    comments.rule(question, "Use bolt.")
    assert materialize_failed_questions(execution, "run-1", adapter, comments) == []
    assert comments.load().comments[0].state == "resolved"


def test_question_id_collision_on_immutable_fields_is_rejected(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "Which?"), terminal=True)
    materialize_failed_questions(execution, "run-1", adapter, comments)
    question = comments.load().comments[0]
    comments.write(question.__class__(**{**question.__dict__, "text": "Different?"}))
    with pytest.raises(ValueError):
        materialize_failed_questions(execution, "run-1", adapter, comments)


def test_reconciliation_repairs_failure_committed_before_comment_write(tmp_path, write) -> None:
    profile, adapter, comments, execution, token, _ = _setup(tmp_path, write)
    execution.fail_unit("run-1", "unit-1", token, error=encode_question_failure("product/bolt", "Which?"), terminal=True)
    assert materialize_failed_questions(execution, "run-1", adapter, comments)


def test_cpk_preserves_the_400_character_question_failure_payload(tmp_path) -> None:
    execution = ExecutionStore(tmp_path / "execution.sqlite3")
    execution.create_run("run", driver="worker", backend="mock", model="", adapter_version="")
    execution.register_units("run", ["unit"])
    token = execution.claim_unit("run", "unit", "worker").fencing_token
    error = "x" * 400
    execution.fail_unit("run", "unit", token, error=error, terminal=True)
    assert execution.list_attempts("run")[1].error == error
