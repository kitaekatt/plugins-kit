"""Durable staged background dispatch for editor work units."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import yaml

from content_pipeline.execution.controller import finalize_run
from content_pipeline.execution.drivers.claude_bg import ClaudeCli, dispatch_wave
from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.store import ExecutionStore

from .adapter import adapter_for
from .planner import CommentPlanStore, CommentPlanner, PlannerPolicy
from .request import DispatchRequest, DispatchRequestSet, load_request
from .run import RunSummary, StaleSliceError, _assert_fresh, _load_attributed, _request_value, _write_machine_result
from .state import DispatchPlan, load_plan, write_plan
from .units import unit_targets, validation_spec_for_unit
from .worker_mount import build_worker_command
from yaml_data_editor_kit.schema import errors_only, load_corpus, load_profile
from content_pipeline.pipeline.workunit import WorkUnit


DispatchInput = DispatchRequest | DispatchRequestSet | str | Path


class BackgroundCommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> tuple[str, str, int]: ...


@dataclass(frozen=True)
class BackgroundDispatchOptions:
    max_agents: int = 4
    batch_size: int = 25
    poll_interval_s: float = 15.0
    launch_confirm_seconds: float = 60.0
    max_reclaims_per_unit: int = 2
    extra_launch_args: tuple[str, ...] = ()
    terminal_exit_grace_seconds: float = 300.0
    stall_timeout_seconds: float = 900.0


@dataclass(frozen=True)
class PreparedBackgroundDispatch:
    run_id: str
    run_dir: Path
    plan_path: Path
    execution_store: Path
    attributed_store: Path
    planned: int


BackgroundRef = PreparedBackgroundDispatch | str | Path


@dataclass(frozen=True)
class BackgroundWaveSummary:
    run_id: str
    dispatcher_acquired: bool
    dispatched: tuple[str, ...]
    recovered: tuple[str, ...]
    accepted: tuple[str, ...]
    settled: Mapping[str, str]
    failed_exhausted: tuple[str, ...]
    halted: str | None
    status_digests: tuple[Mapping[str, Any], ...]
    aborted_reason: str | None


@dataclass(frozen=True)
class BackgroundDispatchStatus:
    run_id: str
    planned: int
    states: Mapping[str, str]
    applied: int
    stale: tuple[str, ...]
    failed: Mapping[str, str]
    halted: bool
    halt_kind: str | None
    unfinished: tuple[str, ...]


class BackgroundStagesRequiredError(NotImplementedError):
    """The background driver requires the staged dispatch API."""


def _prepared(value: BackgroundRef) -> PreparedBackgroundDispatch:
    if isinstance(value, PreparedBackgroundDispatch):
        return value
    return load_background_dispatch(value)


def _plan_adapter(plan: DispatchPlan) -> Any:
    base = adapter_for(plan)

    def unit_for(unit_id: str) -> WorkUnit:
        return WorkUnit(id=unit_id, payload=plan.unit_for(unit_id).get("payload", {}))

    validation = validation_spec_for_unit

    return base, unit_for, validation


def _make_adapter(plan: DispatchPlan, run_dir: Path) -> Any:
    profile = load_profile(plan.corpus_path / "profile" if (plan.corpus_path / "profile").exists() else plan.corpus_path)
    base, unit_for, validation = _plan_adapter(plan)
    attributed = run_dir / plan.attributed_store

    def apply(unit_id: str, result: Any) -> None:
        unit = unit_for(unit_id)
        _write_machine_result(unit, result, attributed, profile, plan.corpus_path)

    base.unit_for = unit_for
    base.validation_spec_for = validation
    base.apply = apply
    base.reconcile = lambda unit_id: False
    return base


def prepare_background_dispatch(request: DispatchInput) -> PreparedBackgroundDispatch:
    resolved = _request_value(request)
    if resolved.driver != "claude_bg":
        raise ValueError("prepare_background_dispatch requires driver 'claude_bg'")
    profile = load_profile(resolved.corpus_path / "profile" if (resolved.corpus_path / "profile").exists() else resolved.corpus_path)
    corpus = load_corpus(profile, resolved.corpus_path)
    if errors_only(corpus.diagnostics):
        raise ValueError("corpus diagnostics prevent dispatch: {}".format(errors_only(corpus.diagnostics)))
    comments = CommentStore(resolved.comment_store_path).load()
    if comments.diagnostics:
        raise ValueError("comment-store diagnostics prevent dispatch: {}".format(comments.diagnostics))
    resolved.run_dir.mkdir(parents=True, exist_ok=True)
    policy = PlannerPolicy(cache_dir=resolved.run_dir / "planner-cache")
    units = CommentPlanner(policy=policy).units(CommentPlanStore(profile, corpus, comments.comments, resolved.selection))
    run_id = "dispatch-{}".format(uuid4().hex)
    plan_path = resolved.run_dir / "dispatch-plan.yaml"
    plan = write_plan(plan_path, run_id=run_id, corpus_path=resolved.corpus_path, comment_store_path=resolved.comment_store_path, units=[{"id": unit.id, "payload": unit.payload} for unit in units])
    execution_path = resolved.run_dir / plan.execution_store
    attributed_path = resolved.run_dir / plan.attributed_store
    if not attributed_path.exists():
        from .run import _atomic_dump

        _atomic_dump(attributed_path, {"records": {}})
    execution = ExecutionStore(execution_path)
    execution.create_run(run_id, driver="claude_bg", backend="claude-bg", model="", adapter_version=plan.adapter_version)
    execution.register_units(run_id, list(plan.unit_ids))
    return PreparedBackgroundDispatch(run_id, resolved.run_dir, plan_path, execution_path, attributed_path, len(units))


def load_background_dispatch(run_dir: str | Path) -> PreparedBackgroundDispatch:
    directory = Path(run_dir).resolve()
    plan_path = directory / "dispatch-plan.yaml"
    plan = load_plan(plan_path)
    execution_path = directory / plan.execution_store
    execution = ExecutionStore(execution_path)
    run = execution.get_run(plan.run_id)
    if run is None or run.driver != "claude_bg" or run.adapter_version != plan.adapter_version:
        raise ValueError("background run identity does not match its plan")
    load_plan(plan_path, execution_unit_ids=[unit.unit_id for unit in execution.list_units(plan.run_id)])
    return PreparedBackgroundDispatch(plan.run_id, directory, plan_path, execution_path, directory / plan.attributed_store, len(plan.units))


def run_background_wave(run: BackgroundRef, *, options: BackgroundDispatchOptions | None = None, executable: str | None = None, runner: BackgroundCommandRunner | None = None, env: Mapping[str, str] | None = None, sleep_fn: Callable[[float], None] = time.sleep, clock_fn: Callable[[], float] = time.time, at: float | None = None) -> BackgroundWaveSummary:
    prepared = _prepared(run)
    opts = options or BackgroundDispatchOptions()
    plan = load_plan(prepared.plan_path)
    execution = ExecutionStore(prepared.execution_store)
    adapter = _make_adapter(plan, prepared.run_dir)
    cli = ClaudeCli(executable=executable, runner=runner) if runner is not None or executable is not None else ClaudeCli()
    report = dispatch_wave(execution, plan.run_id, execution.list_units(plan.run_id), adapter, cli=cli, worker_command=build_worker_command(prepared.plan_path, prepared.run_dir / "workers"), max_agents=opts.max_agents, batch_size=opts.batch_size, poll_interval_s=opts.poll_interval_s, launch_confirm_seconds=opts.launch_confirm_seconds, max_reclaims_per_unit=opts.max_reclaims_per_unit, extra_launch_args=opts.extra_launch_args, terminal_exit_grace_seconds=opts.terminal_exit_grace_seconds, stall_timeout_seconds=opts.stall_timeout_seconds, env=env, sleep_fn=sleep_fn, clock_fn=clock_fn, at=at)
    return BackgroundWaveSummary(report.run_id, report.dispatcher_acquired, tuple(report.dispatched), tuple(report.recovered), tuple(report.accepted), dict(report.settled), tuple(report.failed_exhausted), report.halted, tuple(report.status_digests), report.aborted_reason)


def get_background_dispatch_status(run: BackgroundRef) -> BackgroundDispatchStatus:
    prepared = _prepared(run)
    plan = load_plan(prepared.plan_path)
    execution = ExecutionStore(prepared.execution_store)
    states: dict[str, str] = {}
    failed: dict[str, str] = {}
    stale: list[str] = []
    applied = 0
    for unit in execution.list_units(plan.run_id):
        state = unit.state.value
        if unit.state is UnitState.ACCEPTED and unit.accepted_text is not None:
            applied_kind = [a.kind for a in execution.list_attempts(plan.run_id, unit.unit_id) if a.kind is AttemptKind.APPLY_SUCCEEDED]
            if applied_kind:
                state = "applied"
                applied += 1
            else:
                state = "accepted"
        elif unit.state is UnitState.FAILED:
            attempts = execution.list_attempts(plan.run_id, unit.unit_id)
            detail = next((a.error or "" for a in reversed(attempts) if a.kind is AttemptKind.FAIL), "")
            failed[unit.unit_id] = detail
            if detail.startswith("stale:"):
                stale.append(unit.unit_id)
        states[unit.unit_id] = state
    record = execution.get_run(plan.run_id)
    unfinished = tuple(uid for uid, state in states.items() if state in {"planned", "pending", "claimed", "accepted"})
    return BackgroundDispatchStatus(plan.run_id, len(plan.units), states, applied, tuple(stale), failed, bool(record and record.halted), record.halted_kind if record else None, unfinished)


class StaleAtFinalizeError(StaleSliceError):
    """One or more accepted units went stale before finalize could apply them.

    Distinct from ``StaleSliceError`` so a caller can tell "this unit's slice moved
    mid-run" from "the run cannot be finalized until these anchors are re-anchored",
    and so the message names every affected unit rather than only the first.
    """

    def __init__(self, stale: list[tuple[str, str]]) -> None:
        self.stale = tuple(stale)
        detail = "; ".join("{}: {}".format(unit_id, reason) for unit_id, reason in stale)
        super().__init__(
            ", ".join(unit_id for unit_id, _ in stale),
            "fresh",
            "stale",
        )
        self._message = "cannot finalize -- {} accepted unit(s) went stale: {}".format(len(stale), detail)

    def __str__(self) -> str:
        return self._message

    def __reduce__(self):
        # Exception.__reduce__ rebuilds from self.args, which does not match this
        # constructor's single argument; rebuild from the stale list instead so the
        # error survives pickling and copying.
        return (self.__class__, (list(self.stale),))


def finalize_background_dispatch(run: BackgroundRef, *, at: float | None = None) -> RunSummary:
    prepared = _prepared(run)
    plan = load_plan(prepared.plan_path)
    execution = ExecutionStore(prepared.execution_store)
    profile = load_profile(plan.corpus_path / "profile" if (plan.corpus_path / "profile").exists() else plan.corpus_path)
    # A corpus edit between acceptance and finalize makes ONE unit stale, not the run.
    # Rejecting it the way the inline lane does keeps the rest applicable and leaves the
    # run finalizable; letting StaleSliceError escape here stranded every accepted unit
    # and made each later finalize raise again.
    stale_at_finalize: list[tuple[str, str]] = []
    for unit in execution.list_units(plan.run_id):
        if unit.state is UnitState.ACCEPTED and unit.accepted_text is not None:
            try:
                _assert_fresh(
                    unit.unit_id,
                    plan.unit_for(unit.unit_id).get("payload", {}),
                    profile,
                    plan.corpus_path,
                )
            except StaleSliceError as exc:
                # KNOWN GAP, deliberately surfaced rather than papered over. A unit that
                # goes stale between acceptance and finalize cannot be settled: ACCEPTED
                # is terminal in the execution store's state machine, so fail_unit refuses
                # the transition, and an adapter that silently skipped the unit would have
                # finalize record an apply that never happened. Raising names every stale
                # unit so an operator can re-anchor and retry; the healthy units in the
                # batch do not apply, which is the cost of not lying about the stale one.
                stale_at_finalize.append((unit.unit_id, str(exc)))
    if stale_at_finalize:
        raise StaleAtFinalizeError(stale_at_finalize)
    applied_ids = finalize_run(execution, plan.run_id, _make_adapter(plan, prepared.run_dir), at=at)
    status = get_background_dispatch_status(prepared)
    # `failed` already contains every stale unit -- `stale` is a labelled subset of
    # it, matching the inline lane, where one stale unit reports rejected=1 stale=1.
    rejected = len(status.failed)
    accepted = sum(state in {"accepted", "applied"} for state in status.states.values())
    return RunSummary(plan.run_id, "claude_bg", status.planned, accepted, status.applied, rejected, len(status.stale), status.halted, prepared.attributed_store, prepared.execution_store, status.states)


from yaml_data_editor_kit.comments import CommentStore

__all__ = ["BackgroundCommandRunner", "BackgroundDispatchOptions", "BackgroundDispatchStatus", "BackgroundRef", "BackgroundStagesRequiredError", "BackgroundWaveSummary", "DispatchInput", "PreparedBackgroundDispatch", "finalize_background_dispatch", "get_background_dispatch_status", "load_background_dispatch", "prepare_background_dispatch", "run_background_wave"]
