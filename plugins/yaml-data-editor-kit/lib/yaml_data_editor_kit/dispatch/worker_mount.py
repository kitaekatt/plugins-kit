"""Plan-authenticated protocol mount for background work-unit workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import sys
from pathlib import Path
from typing import Any

from content_pipeline.cli.run import build_commands
from content_pipeline.cli.scaffold import dispatch
from content_pipeline.execution.adapter import RunAdapter
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.execution.protocol import ProtocolHandler, build_handlers
from content_pipeline.execution.workerpack import WorkerCommand
from content_pipeline.freshness.hashing import content_hash

from yaml_data_editor_kit.comments import EvaluationError, SelectorError, parse_anchor, resolve_anchor, slice_hash
from yaml_data_editor_kit.comments.store import CommentStore
from yaml_data_editor_kit.schema import load_corpus, load_profile

from .adapter import adapter_for
from .protocol import build_dispatch_handlers
from .state import DispatchPlan, load_plan
from .units import unit_targets


def build_worker_command(plan_path: Path, worker_dir: Path) -> WorkerCommand:
    """Build the deterministic worker command template for one run."""
    plan = Path(plan_path).resolve()
    worker = Path(worker_dir).resolve()
    return WorkerCommand(
        argv=(
            sys.executable,
            "-m",
            "yaml_data_editor_kit.dispatch.worker_mount",
            str(plan),
        ),
        answer_dir=str(worker / "answers"),
        envelope_dir=str(worker / "envelopes"),
    )


def _profile_path(corpus_path: Path) -> Path:
    candidate = corpus_path / "profile"
    return candidate if candidate.exists() else corpus_path


def _require_payload(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise ValueError("protocol payload is missing {!r}".format(key))
    return payload[key]


def _fresh(
    unit_id: str,
    payload: Mapping[str, Any],
    profile: Any,
    corpus_path: Path,
) -> None:
    """Recheck every saved slice and guard before worker read or submit."""
    corpus = load_corpus(profile, corpus_path)
    for target in unit_targets(payload):
        expected = target.get("content_hash")
        anchor = target.get("anchor")
        if not isinstance(expected, str) or not isinstance(anchor, str):
            raise ValueError("planned unit {!r} has invalid freshness data".format(unit_id))
        try:
            actual = content_hash(resolve_anchor(parse_anchor(anchor), profile, corpus).slice_value)
        except (EvaluationError, SelectorError) as exc:
            raise ValueError("stale:{}".format(anchor)) from exc
        if actual != expected:
            raise ValueError("stale:{}".format(anchor))

        anchors = target.get("comment_anchors", ())
        guards = target.get("comment_guards", ())
        if len(anchors) != len(guards):
            raise ValueError("planned unit {!r} has mismatched comment guards".format(unit_id))
        for comment_anchor, guard in zip(anchors, guards):
            if not isinstance(comment_anchor, str) or not isinstance(guard, str):
                raise ValueError("planned unit {!r} has invalid comment guards".format(unit_id))
            try:
                actual_guard = slice_hash(
                    resolve_anchor(parse_anchor(comment_anchor), profile, corpus).slice_value
                )
            except (EvaluationError, SelectorError) as exc:
                raise ValueError("stale:{}".format(anchor)) from exc
            if actual_guard != guard:
                raise ValueError("stale:{}".format(anchor))

        for ruling in target.get("rulings", ()):
            ruling_anchor = ruling.get("anchor")
            guard = ruling.get("guard")
            if not isinstance(ruling_anchor, str) or not isinstance(guard, str):
                raise ValueError("planned unit {!r} has invalid ruling guards".format(unit_id))
            try:
                actual_guard = slice_hash(
                    resolve_anchor(parse_anchor(ruling_anchor), profile, corpus).slice_value
                )
            except (EvaluationError, SelectorError) as exc:
                raise ValueError("stale:{}".format(anchor)) from exc
            if actual_guard != guard:
                raise ValueError("stale:{}".format(anchor))


def _runtime(plan_path: Path) -> tuple[DispatchPlan, ExecutionStore, RunAdapter, dict[str, ProtocolHandler]]:
    preliminary = load_plan(Path(plan_path))
    execution = ExecutionStore(Path(plan_path).parent / preliminary.execution_store)
    plan = load_plan(Path(plan_path), execution_unit_ids=[unit.unit_id for unit in execution.list_units(preliminary.run_id)])
    adapter = adapter_for(plan)
    profile = load_profile(_profile_path(plan.corpus_path))
    comments = CommentStore(plan.comment_store_path)
    base = build_handlers(execution, adapter)
    handlers = build_dispatch_handlers(
        base,
        execution=execution,
        adapter=adapter,
        comment_store=comments,
        profile=profile,
        corpus_path=plan.corpus_path,
    )

    def authenticate(payload: Mapping[str, Any], *, fence: bool = False) -> tuple[str, Any]:
        run_id = _require_payload(payload, "run_id")
        unit_id = _require_payload(payload, "unit_id")
        if run_id != plan.run_id:
            raise ValueError("protocol run_id does not match dispatch plan")
        if not isinstance(unit_id, str):
            raise ValueError("protocol unit_id must be text")
        plan.unit_for(unit_id)
        unit = execution.get_unit(plan.run_id, unit_id)
        if unit is None:
            raise ValueError("execution store has no unit {!r}".format(unit_id))
        worker_id = _require_payload(payload, "worker_id")
        if worker_id != unit.claimed_by:
            raise ValueError("protocol worker_id is not the active claimant")
        if fence:
            token = _require_payload(payload, "fencing_token")
            if token != unit.fencing_token:
                raise ValueError("protocol fencing token is stale")
        return unit_id, unit

    raw_claim = handlers["claim"]

    def claim(payload: Mapping[str, Any]) -> Any:
        run_id = _require_payload(payload, "run_id")
        unit_id = _require_payload(payload, "unit_id")
        worker_id = _require_payload(payload, "worker_id")
        if run_id != plan.run_id:
            raise ValueError("protocol run_id does not match dispatch plan")
        if not isinstance(unit_id, str) or not isinstance(worker_id, str):
            raise ValueError("protocol claim identity must be text")
        plan.unit_for(unit_id)
        return raw_claim(payload)

    raw_read = handlers["read"]
    raw_submit = handlers["submit"]
    raw_fail = handlers["fail"]

    def stale_fail(unit_id: str, unit: Any, detail: str) -> None:
        """Terminally settle a claim whose saved freshness no longer holds."""
        raw_fail({
            "run_id": plan.run_id,
            "unit_id": unit_id,
            "worker_id": unit.claimed_by,
            "fencing_token": unit.fencing_token,
            "terminal": True,
            "error": detail,
        })

    def check_fresh(unit_id: str, unit: Any) -> None:
        try:
            _fresh(unit_id, adapter.unit_for(unit_id).payload, profile, plan.corpus_path)
        except ValueError as exc:
            if not str(exc).startswith("stale:"):
                raise
            stale_fail(unit_id, unit, str(exc))
            raise

    def read(payload: Mapping[str, Any]) -> Any:
        unit_id, unit = authenticate(payload)
        check_fresh(unit_id, unit)
        return raw_read(payload)

    def submit(payload: Mapping[str, Any]) -> Any:
        unit_id, unit = authenticate(payload, fence=True)
        check_fresh(unit_id, unit)
        return raw_submit(payload)

    def fail(payload: Mapping[str, Any]) -> Any:
        _unit_id, _unit = authenticate(payload, fence=True)
        if payload.get("terminal") is not True:
            raise ValueError("worker fail must be terminal")
        error = payload.get("error")
        if not isinstance(error, str) or not error.strip():
            raise ValueError("worker fail requires nonempty error detail")
        return raw_fail(payload)

    return plan, execution, adapter, {
        "claim": claim,
        "read": read,
        "submit": submit,
        "fail": fail,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the plan-first CPK protocol mount."""
    values = list(sys.argv[1:] if argv is None else argv)
    if not values:
        sys.stderr.write("usage: worker_mount PLAN_PATH protocol [args]\n")
        return 2
    try:
        plan, execution, adapter, handlers = _runtime(Path(values[0]))
        commands = build_commands(execution, adapter=adapter, protocol_handlers=handlers)
        return dispatch(values[1:], commands)
    except Exception as exc:  # noqa: BLE001 - process boundary returns a stable error
        sys.stderr.write("error: worker mount: {}\n".format(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_worker_command", "main"]
