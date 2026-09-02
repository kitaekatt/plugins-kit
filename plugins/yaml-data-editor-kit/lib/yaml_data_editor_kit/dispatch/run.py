"""Execute planned comment units through content-pipeline-kit.

The inline lane uses content-pipeline-kit for claims, fencing, leases, and
accepted-text recording. The attributed YAML file is the small file seam for
the editor-facing result: only its ``machine`` slice is written by this
module. Existing ``human`` slices are carried through untouched.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import yaml

from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.controller import finalize_run
from content_pipeline.execution.drivers.inline import (
    UnacceptedSubmissionError,
    run_wave,
)
from content_pipeline.execution.model import UnitState
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.freshness.hashing import content_hash
from content_pipeline.llm.backends import route
from content_pipeline.llm.platform import LLMBackend, submit_validated
from content_pipeline.store.attributed import effective_value

from yaml_data_editor_kit.comments import (
    EvaluationError,
    SelectorError,
    parse_anchor,
    resolve_anchor,
    slice_hash,
)
from yaml_data_editor_kit.comments.store import CommentStore
from yaml_data_editor_kit.schema import errors_only, load_corpus, load_profile

from .planner import CommentPlanStore, CommentPlanner, PlannerPolicy
from .request import DispatchRequest, DispatchRequestSet, load_request
from .units import (
    plain_value,
    prompt_for_payload,
    system_prompt_for_unit,
    unit_targets,
    validation_spec_for_unit,
)


@dataclass(frozen=True)
class RunSummary:
    """The result of one dispatch invocation."""

    run_id: str
    driver: str
    planned: int
    accepted: int
    applied: int
    rejected: int
    stale: int
    halted: bool
    attributed_store: Path
    execution_store: Path
    statuses: Mapping[str, str] = field(default_factory=dict)


class StaleSliceError(RuntimeError):
    """A unit's anchored slice changed before its result could be applied."""

    def __init__(
        self,
        unit_id: str,
        expected: str,
        actual: str | None,
        target_anchor: str | None = None,
    ) -> None:
        self.unit_id = unit_id
        self.expected = expected
        self.actual = actual
        self.target_anchor = target_anchor
        detail = "unresolvable" if actual is None else repr(actual)
        super().__init__(
            "unit {!r} target {!r} is stale: expected anchored-slice hash {!r}, got {}".format(
                unit_id, target_anchor, expected, detail
            )
        )


def dispatch(
    request: DispatchRequest | DispatchRequestSet | str | Path,
    *,
    backend: LLMBackend | None = None,
    planner_backend: LLMBackend | None = None,
    planner_policy: PlannerPolicy | None = None,
) -> RunSummary:
    """Load, plan, and execute one file-backed dispatch request.

    ``backend`` is an injection seam for tests and callers that already chose
    a backend. When omitted, content-pipeline-kit performs its normal routing.
    """
    resolved_request = _request_value(request)
    if resolved_request.driver == "claude_bg":
        from .background import BackgroundStagesRequiredError

        raise BackgroundStagesRequiredError(
            "background dispatch requires prepare_background_dispatch, "
            "run_background_wave, get_background_dispatch_status, and "
            "finalize_background_dispatch"
        )

    profile = load_profile(_profile_path(resolved_request.corpus_path))
    corpus = load_corpus(profile, resolved_request.corpus_path)
    corpus_errors = errors_only(corpus.diagnostics)
    if corpus_errors:
        raise ValueError("corpus diagnostics prevent dispatch: {}".format(corpus_errors))

    comments = CommentStore(resolved_request.comment_store_path).load()
    if comments.diagnostics:
        raise ValueError(
            "comment-store diagnostics prevent dispatch: {}".format(
                comments.diagnostics
            )
        )

    run_dir = resolved_request.run_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    policy = planner_policy
    if policy is not None and policy.cache_dir is None:
        from dataclasses import replace

        policy = replace(policy, cache_dir=run_dir / "planner-cache")
    elif policy is None and planner_backend is not None:
        policy = PlannerPolicy(cache_dir=run_dir / "planner-cache")
    planning_store = CommentPlanStore(
        profile,
        corpus,
        comments.comments,
        resolved_request.selection,
    )
    units = CommentPlanner(
        backend=planner_backend,
        policy=policy,
    ).units(planning_store)
    execution_path = run_dir / "execution.sqlite3"
    attributed_path = run_dir / "attributed.yaml"
    _load_attributed(attributed_path)
    execution = ExecutionStore(execution_path)
    active_backend = backend if backend is not None else route()
    run_id = "dispatch-{}".format(uuid4().hex)
    backend_name = str(getattr(active_backend, "name", type(active_backend).__name__))
    execution.create_run(
        run_id,
        driver="inline",
        backend=backend_name,
        model="",
        adapter_version="yaml-data-editor-dispatch-1",
    )
    execution.register_units(run_id, [unit.id for unit in units])

    statuses: dict[str, str] = {unit.id: "planned" for unit in units}
    applied_ids: set[str] = set()
    accepted_result_ids: set[str] = set()
    rejected = 0
    stale = 0

    unit_by_id = {unit.id: unit for unit in units}

    def unit_for(unit_id: str):
        return unit_by_id[unit_id]

    def generate(unit: Any) -> str:
        validation = adapter.resolve_validation_spec(unit)
        payload = unit.payload
        _assert_fresh(
            unit.id,
            payload,
            profile,
            resolved_request.corpus_path,
        )
        result = submit_validated(
            backend=active_backend,
            system=system_prompt_for_unit(unit),
            user=_prompt(payload),
            model="",
            parse_fn=validation.parse_fn,
            validators=validation.validators,
            context=validation.context,
            block_soft=validation.block_soft,
            max_attempts=1,
        )
        if not result.accepted or not result.responses:
            raise UnacceptedSubmissionError(unit.id, result.rejections)
        _assert_fresh(
            unit.id,
            payload,
            profile,
            resolved_request.corpus_path,
        )
        return result.responses[-1].text

    def apply(unit_id: str, result: Any) -> None:
        unit = unit_by_id[unit_id]
        _write_machine_result(unit, result, attributed_path, profile, resolved_request.corpus_path)
        applied_ids.add(unit_id)

    adapter = RunAdapter(
        unit_for=unit_for,
        validation_spec_for=validation_spec_for_unit,
        apply=apply,
        reconcile=lambda unit_id: False,
    )


    for unit in units:
        statuses[unit.id] = "running"
        unit_record = execution.get_unit(run_id, unit.id)
        if unit_record is None:
            raise RuntimeError("execution store lost planned unit {!r}".format(unit.id))
        try:
            accepted_ids = run_wave(
                execution,
                run_id,
                [unit_record],
                adapter,
                generate=generate,
            )
        except StaleSliceError as exc:
            _reject_claimed(execution, run_id, unit.id, "stale:{}".format(exc))
            statuses[unit.id] = "stale"
            rejected += 1
            stale += 1
            continue
        except UnacceptedSubmissionError as exc:
            _reject_claimed(execution, run_id, unit.id, "rejected:{}".format(exc))
            statuses[unit.id] = "rejected"
            rejected += 1
            continue

        if unit.id not in accepted_ids:
            if execution.get_run(run_id).halted:  # type: ignore[union-attr]
                statuses[unit.id] = "halted"
                break
            statuses[unit.id] = "pending"
            break

        accepted_result_ids.add(unit.id)
        apply_stale = False
        try:
            finalize_run(execution, run_id, adapter)
        except StaleSliceError:
            apply_stale = True
            statuses[unit.id] = "stale"
            rejected += 1
            stale += 1
        if not apply_stale:
            if unit.id in applied_ids:
                statuses[unit.id] = "applied"
            else:
                statuses[unit.id] = "accepted"

        if execution.get_run(run_id).halted:  # type: ignore[union-attr]
            break

    accepted = len(accepted_result_ids)
    halted_record = execution.get_run(run_id)
    return RunSummary(
        run_id=run_id,
        driver=resolved_request.driver,
        planned=len(units),
        accepted=accepted,
        applied=len(applied_ids),
        rejected=rejected,
        stale=stale,
        halted=bool(halted_record and halted_record.halted),
        attributed_store=attributed_path,
        execution_store=execution_path,
        statuses=dict(statuses),
    )


def effective_result(record: Mapping[str, Any]) -> Any:
    """Resolve one result record with CPK's human > machine > sourced order."""
    return effective_value(
        record.get("sourced"),
        record.get("machine"),
        record.get("human"),
    )


def _request_value(
    request: DispatchRequest | DispatchRequestSet | str | Path,
) -> DispatchRequest:
    if isinstance(request, DispatchRequest):
        return request
    loaded = request if isinstance(request, DispatchRequestSet) else load_request(Path(request))
    if loaded.request is None or loaded.diagnostics:
        raise ValueError("dispatch request diagnostics: {}".format(loaded.diagnostics))
    return loaded.request


def _profile_path(corpus_path: Path) -> Path:
    profile_path = corpus_path / "profile"
    return profile_path if profile_path.exists() else corpus_path


def _assert_fresh(
    unit_id: str,
    payload: Mapping[str, Any],
    profile: Any,
    corpus_path: Path,
) -> None:
    current_corpus = load_corpus(profile, corpus_path)
    for target in unit_targets(payload):
        expected = target.get("content_hash")
        anchor_text = target.get("anchor")
        if not isinstance(expected, str) or not isinstance(anchor_text, str):
            raise ValueError("planned unit {!r} has an invalid freshness payload".format(unit_id))
        try:
            current = resolve_anchor(parse_anchor(anchor_text), profile, current_corpus)
            actual = content_hash(current.slice_value)
        except (EvaluationError, SelectorError) as exc:
            raise StaleSliceError(unit_id, expected, None, anchor_text) from exc
        if actual != expected:
            raise StaleSliceError(unit_id, expected, actual, anchor_text)
        anchors = target.get("comment_anchors", ())
        guards = target.get("comment_guards", ())
        if len(anchors) != len(guards):
            raise ValueError("planned unit {!r} has mismatched comment freshness data".format(unit_id))
        for comment_anchor, expected_guard in zip(anchors, guards):
            if not isinstance(comment_anchor, str) or not isinstance(expected_guard, str):
                raise ValueError("planned unit {!r} has invalid comment freshness data".format(unit_id))
            try:
                comment_slice = resolve_anchor(
                    parse_anchor(comment_anchor), profile, current_corpus
                ).slice_value
                actual_guard = slice_hash(comment_slice)
            except (EvaluationError, SelectorError) as exc:
                raise StaleSliceError(unit_id, expected, None, anchor_text) from exc
            if actual_guard != expected_guard:
                raise StaleSliceError(unit_id, expected, actual_guard, anchor_text)
        for ruling in target.get("rulings", ()):
            ruling_anchor = ruling.get("anchor")
            expected_guard = ruling.get("guard")
            if isinstance(ruling_anchor, str) and isinstance(expected_guard, str):
                try:
                    actual_guard = slice_hash(
                        resolve_anchor(parse_anchor(ruling_anchor), profile, current_corpus).slice_value
                    )
                except (EvaluationError, SelectorError) as exc:
                    raise StaleSliceError(unit_id, expected, None, anchor_text) from exc
                if actual_guard != expected_guard:
                    raise StaleSliceError(unit_id, expected, actual_guard, anchor_text)


def _reject_claimed(
    execution: ExecutionStore,
    run_id: str,
    unit_id: str,
    error: str,
) -> None:
    unit = execution.get_unit(run_id, unit_id)
    if unit is not None and unit.state is UnitState.CLAIMED:
        execution.fail_unit(
            run_id,
            unit_id,
            unit.fencing_token,
            error=error,
            terminal=True,
        )


def _prompt(payload: Mapping[str, Any]) -> str:
    return prompt_for_payload(payload)


def _load_attributed(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"records": {}}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("cannot read attributed store {}: {}".format(path, exc)) from exc
    if not isinstance(raw, dict):
        raise ValueError("attributed store must be a mapping: {}".format(path))
    records = raw.get("records", {})
    if not isinstance(records, dict):
        raise ValueError("attributed store records must be a mapping: {}".format(path))
    raw["records"] = records
    return raw


def _write_machine_result(
    unit: Any,
    result: Any,
    path: Path,
    profile: Any,
    corpus_path: Path,
) -> None:
    with _attributed_write_lock(path):
        _assert_fresh(unit.id, unit.payload, profile, corpus_path)
        attributed = _load_attributed(path)
        records = attributed.setdefault("records", {})
        targets = unit_targets(unit)
        results = {unit.id: result} if "targets" not in unit.payload else result["results"]
        for target in targets:
            record_id = target.get("id", unit.id)
            existing = records.get(record_id, {})
            if not isinstance(existing, dict):
                raise ValueError("attributed record {!r} must be a mapping".format(record_id))
            record = dict(existing)
            record.setdefault("anchor", target["anchor"])
            record.setdefault("sourced", plain_value(target["anchored_slice"]))
            record["comments"] = list(target["comments"])
            record["comment_ids"] = list(target["comment_ids"])
            record["content_hash"] = target["content_hash"]
            record["machine"] = results[record_id] if isinstance(results, dict) else next(
                item["machine"] for item in results if item["anchor"] == target["anchor"]
            )
            records[record_id] = record
        _atomic_dump(path, attributed)


_ATTRIBUTED_WRITE_LOCK = threading.RLock()


@contextmanager
def _attributed_write_lock(path: Path):
    """Serialize attributed merges in this process and across POSIX workers."""
    with _ATTRIBUTED_WRITE_LOCK:
        lock_path = path.with_name("{}.lock".format(path.name))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                import fcntl
            except ImportError:
                fcntl = None
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _atomic_dump(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name(".{}.{}.tmp".format(path.name, uuid4().hex))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)




__all__ = [
    "RunSummary",
    "StaleSliceError",
    "dispatch",
    "effective_result",
]
