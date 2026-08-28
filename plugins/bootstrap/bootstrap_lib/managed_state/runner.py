"""Operation planning and execution over declared resources."""

from __future__ import annotations

from .model import (
    Inspection,
    Declaration,
    Operation,
    Plan,
    PlanItem,
    Report,
    ResourceResult,
    ResourceApplyError,
    Resources,
    State,
    Status,
)


def _operation(value: Operation | str) -> Operation:
    return value if isinstance(value, Operation) else Operation(value)


def _resources(value: Declaration | Resources) -> Resources:
    return value.resources if isinstance(value, Declaration) else value


def plan(resources: Declaration | Resources, operation: Operation | str) -> Plan:
    """Inspect resources and describe what the requested operation may change."""
    op = _operation(operation)
    items = []
    for resource in _resources(resources):
        inspection = resource.inspect()
        will_change = (
            op is Operation.UPDATE
            and inspection.state in {State.MISSING, State.DRIFTED}
        ) or (op is Operation.INSTALL and inspection.state is State.MISSING)
        blocked = inspection.state is State.ERROR or (
            op is Operation.INSTALL and inspection.state is State.DRIFTED
        )
        items.append(PlanItem(resource, inspection, will_change, blocked))
    return Plan(op, tuple(items))


def _unchanged(item: PlanItem, operation: Operation) -> ResourceResult:
    state = item.inspection.state
    if item.blocked or (
        operation is Operation.CHECK and state is not State.CURRENT
    ):
        detail = item.inspection.detail
        if state is State.DRIFTED:
            detail = f"drift requires update: {detail}"
        return ResourceResult(
            item.resource.name, Status.BLOCKED, state, state, detail)
    return ResourceResult(
        item.resource.name, Status.UNCHANGED, state, state,
        item.inspection.detail)


def run(resources: Declaration | Resources, operation: Operation | str) -> Report:
    """Run check, install-missing, or update-to-convergence semantics."""
    execution_plan = plan(resources, operation)
    results = []
    for item in execution_plan.items:
        if not item.will_change:
            results.append(_unchanged(item, execution_plan.operation))
            continue
        # A plan is evidence, not a lock. Re-inspect at the mutation boundary so
        # install cannot overwrite drift that appeared after planning and update
        # cannot needlessly rewrite a resource another process converged.
        fresh = item.resource.inspect()
        refreshed = PlanItem(
            item.resource,
            fresh,
            fresh.state in {State.MISSING, State.DRIFTED},
            fresh.state is State.ERROR or (
                execution_plan.operation is Operation.INSTALL
                and fresh.state is State.DRIFTED
            ),
        )
        if fresh.state is State.CURRENT or refreshed.blocked:
            results.append(_unchanged(refreshed, execution_plan.operation))
            continue
        try:
            results.append(item.resource.converge(
                fresh, execution_plan.operation))
        except ResourceApplyError as exc:
            results.append(ResourceResult(
                item.resource.name,
                Status.FAILED,
                fresh.state,
                exc.after.state,
                str(exc),
                exc.backup,
                exc.rollback,
            ))
        except Exception as exc:  # resource failures are report data, not a crash
            try:
                after = item.resource.inspect()
            except Exception as inspect_exc:
                after = Inspection(
                    State.ERROR, f"post-failure inspection failed: {inspect_exc}")
            results.append(ResourceResult(
                item.resource.name,
                Status.FAILED,
                fresh.state,
                after.state,
                f"{exc}; observed afterward: {after.detail}",
            ))
    return Report(execution_plan.operation, tuple(results))
