"""Worker protocol adapter for turning terminal ambiguity failures into questions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from content_pipeline.execution.model import AttemptKind
from content_pipeline.execution.protocol import ProtocolHandler, build_handlers
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.freshness.hashing import content_hash

from yaml_data_editor_kit.comments import parse_anchor, resolve_anchor, slice_hash
from yaml_data_editor_kit.comments.store import Comment, CommentStore, QUESTION
from yaml_data_editor_kit.schema import load_corpus

from .units import unit_targets

QUESTION_FAILURE_PREFIX = "question/1:"
_MAX_FAILURE_LENGTH = 400


class QuestionProtocolError(ValueError):
    """A worker failure does not satisfy the question protocol."""


def encode_question_failure(anchor: str, question: str) -> str:
    """Encode one canonical, bounded question failure value."""
    if not isinstance(anchor, str) or not anchor.strip():
        raise ValueError("question failure anchor must be non-empty text")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question failure question must be non-empty text")
    suffix = json.dumps(
        {"anchor": anchor, "question": question},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded = QUESTION_FAILURE_PREFIX + suffix
    if len(encoded) > _MAX_FAILURE_LENGTH:
        raise ValueError("question failure exceeds 400 characters")
    return encoded


def build_dispatch_handlers(
    base_handlers: Mapping[str, ProtocolHandler],
    *,
    execution: ExecutionStore,
    adapter: Any,
    comment_store: CommentStore,
    profile: Any,
    corpus_path: Path,
) -> dict[str, ProtocolHandler]:
    """Copy CPK handlers and replace only ``fail`` with the question adapter."""
    handlers = dict(base_handlers)
    base_fail = handlers["fail"]

    def fail(payload: Mapping[str, Any]) -> Any:
        parsed = _question_payload(payload)
        if parsed is None:
            result = base_fail(payload)
            materialize_failed_questions(execution, str(payload["run_id"]), adapter, comment_store)
            return result
        anchor, question = parsed
        target = _target_for_failure(adapter, payload, anchor)
        if _target_is_stale(target, profile, corpus_path):
            stale_payload = dict(payload)
            stale_payload["terminal"] = True
            stale_payload["error"] = "stale:" + anchor
            return base_fail(stale_payload)
        result = base_fail(payload)
        materialize_failed_questions(execution, str(payload["run_id"]), adapter, comment_store)
        return result

    handlers["fail"] = fail
    return handlers


def materialize_failed_questions(
    execution: ExecutionStore,
    run_id: str,
    adapter: Any,
    comment_store: CommentStore,
) -> list[str]:
    """Materialize every durable question failure missing from the comment store."""
    existing = {comment.id: comment for comment in comment_store.load().comments}
    created: list[str] = []
    for attempt in execution.list_attempts(run_id):
        if attempt.kind is not AttemptKind.FAIL or not attempt.error:
            continue
        if not attempt.error.startswith(QUESTION_FAILURE_PREFIX):
            continue
        anchor, question = _decode_question_failure(attempt.error)
        unit = adapter.unit_for(attempt.unit_id)
        target = _target_for_anchor(unit_targets(unit), anchor)
        question_id = "question-" + content_hash(
            [attempt.run_id, attempt.id, attempt.unit_id]
        )
        target_slice = target.get("anchored_slice")
        comment = Comment(
            id=question_id,
            anchor=parse_anchor(anchor),
            text=question,
            state="open",
            created=datetime.fromtimestamp(
                attempt.at, timezone.utc
            ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            guard=slice_hash(target_slice),
            annotations={
                "yaml_data_editor_kit": {
                    "source_run_id": attempt.run_id,
                    "source_attempt_id": attempt.id,
                    "source_unit_id": attempt.unit_id,
                    "source_comment_ids": sorted(target.get("comment_ids", [])),
                    "target_anchor": anchor,
                }
            },
            kind=QUESTION,
        )
        prior = existing.get(question_id)
        if prior is not None:
            _check_question_identity(prior, comment)
            continue
        comment_store.write(comment)
        existing[question_id] = comment
        created.append(question_id)
    return created


def _question_payload(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    error = payload.get("error")
    if not isinstance(error, str) or not error.startswith(QUESTION_FAILURE_PREFIX):
        return None
    anchor, question = _decode_question_failure(error)
    if payload.get("terminal") is not True:
        raise QuestionProtocolError("question failure must be terminal")
    return anchor, question


def _decode_question_failure(error: str) -> tuple[str, str]:
    if len(error) > _MAX_FAILURE_LENGTH or not error.startswith(QUESTION_FAILURE_PREFIX):
        raise QuestionProtocolError("question failure exceeds 400 characters")
    suffix = error[len(QUESTION_FAILURE_PREFIX):]
    try:
        value = json.loads(suffix, object_pairs_hook=_unique_object)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QuestionProtocolError("question failure is not canonical JSON") from exc
    if not isinstance(value, dict) or set(value) != {"anchor", "question"}:
        raise QuestionProtocolError("question failure must contain exactly anchor and question")
    anchor, question = value["anchor"], value["question"]
    if not isinstance(anchor, str) or not anchor.strip():
        raise QuestionProtocolError("question failure anchor must be non-empty text")
    if not isinstance(question, str) or not question.strip():
        raise QuestionProtocolError("question failure question must be non-empty text")
    if encode_question_failure(anchor, question) != error:
        raise QuestionProtocolError("question failure is not canonical")
    return anchor, question


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key {!r}".format(key))
        result[key] = value
    return result


def _target_for_failure(adapter: Any, payload: Mapping[str, Any], anchor: str) -> Mapping[str, Any]:
    unit = adapter.unit_for(str(payload.get("unit_id", "")))
    return _target_for_anchor(unit_targets(unit), anchor)


def _target_for_anchor(targets: list[Mapping[str, Any]], anchor: str) -> Mapping[str, Any]:
    matches = [target for target in targets if target.get("anchor") == anchor]
    if len(matches) != 1:
        raise QuestionProtocolError("question failure anchor must name exactly one target")
    return matches[0]


def _target_is_stale(target: Mapping[str, Any], profile: Any, corpus_path: Path) -> bool:
    try:
        corpus = load_corpus(profile, corpus_path)
        current = resolve_anchor(parse_anchor(str(target["anchor"])), profile, corpus)
        if content_hash(current.slice_value) != target.get("content_hash"):
            return True
        anchors = target.get("comment_anchors", ())
        guards = target.get("comment_guards", ())
        if len(anchors) != len(guards):
            return True
        return any(
            slice_hash(resolve_anchor(parse_anchor(anchor), profile, corpus).slice_value) != guard
            for anchor, guard in zip(anchors, guards)
        )
    except Exception:
        return True


def _check_question_identity(existing: Comment, expected: Comment) -> None:
    fields = ("kind", "anchor", "text", "guard", "annotations")
    for field in fields:
        left = getattr(existing, field)
        right = getattr(expected, field)
        if field == "anchor":
            left, right = left.canonical(), right.canonical()
        if left != right:
            raise ValueError("question id collision on immutable field {!r}".format(field))


__all__ = [
    "QUESTION_FAILURE_PREFIX",
    "QuestionProtocolError",
    "encode_question_failure",
    "build_dispatch_handlers",
    "materialize_failed_questions",
]
