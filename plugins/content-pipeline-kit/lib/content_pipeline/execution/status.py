"""Bounded RunStatus digest -- what a supervisor may see, and nothing more.

The digest exists so a process that did NOT run the work (a supervising
session, a batch-boundary check, an operator) can answer "how is this run
going" from durable state alone. Invariant 6 (the plan's, restated here as the
module's whole reason to exist): the digest never contains prompts, unit
payloads, or full outputs -- only counts, timestamps, and small operational
codes.

That last part is deliberately narrower than "capped in length": a short
string is still a raw string, and `fail_unit(error=...)` / `set_halt(...,
detail=...)` accept arbitrary caller text that the DURABLE attempt/unit rows
legitimately store in full (truncated only defensively, at the store layer).
The digest never re-surfaces that text -- it classifies it. ``error`` and
``halted_detail`` are reduced to a short, content-free, stable code (a hash of
the exact text) before they ever reach a :class:`RunStatus`, so two identical
errors still group together (grouping is preserved) without the digest ever
carrying what a caller actually wrote. A worker's raw error text and a halt's
raw detail live only in the store's durable rows, reachable by a caller that
explicitly asks for them (``list_attempts``, ``get_run``) -- never through
this digest.

:func:`compute_status` is read-only over an :class:`~content_pipeline.execution.
store.ExecutionStore` -- it issues plain queries, no writes -- so a supervisor
polling status never contends with a worker's claim/submit transactions. It
reads its run/units/attempts through :meth:`ExecutionStore.snapshot`, which
runs all three queries inside ONE read transaction: three separate
connections (the previous shape) could observe a write landing between them
and produce a torn digest (a count reflecting a state a failure-group tally
did not yet see, or vice versa).
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from content_pipeline.execution.model import AttemptKind, UnitState
from content_pipeline.execution.store import ExecutionStore

DEFAULT_THROUGHPUT_WINDOW_S = 300.0
DEFAULT_MAX_FAILURE_GROUPS = 5

_CODE_LENGTH = 12


def _classify(text: Optional[str]) -> str:
    """A short, stable, content-free code for an arbitrary operational string.

    Identical input always yields the identical code (so grouping by exact
    text is preserved -- deliberately simple, no fuzzy clustering, same as
    before), and the code carries no recoverable content: it is a truncated
    hex digest, not a redaction or a summary of ``text``. Empty/``None``
    input yields ``""``.
    """
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_CODE_LENGTH]


@dataclass(frozen=True)
class FailureGroup:
    """One capped group of recent failures sharing an error CODE.

    ``error_code`` is :func:`_classify` applied to whatever the caller passed
    to :meth:`~content_pipeline.execution.store.ExecutionStore.fail_unit` as
    ``error`` -- never the raw text (invariant 6 / the plan's digest-leak
    fix). Two failures with the identical raw error text still group under
    the identical code, so this loses grouping fidelity to nothing; it only
    loses the ability to read the text back out of the digest.
    """

    error_code: str
    count: int
    last_unit_id: str
    last_at: float


@dataclass(frozen=True)
class RunStatus:
    """A bounded, read-only snapshot of one run's state.

    Every field here is either a count, a timestamp, or a short, content-free
    code. Nothing here can hold a prompt, a unit payload, or a full model
    output: ``accept_unit`` / ``fail_unit`` never take those in the first
    place, and the one caller-supplied strings this phase does accept
    (``fail_unit(error=...)``, ``set_halt(..., detail=...)``) are classified
    to a code (see :func:`_classify`) before they ever reach this dataclass --
    the raw text is never copied into a ``RunStatus`` field, not even
    truncated.
    """

    run_id: str
    driver: str
    backend: str
    model: str
    adapter_version: str
    total_units: int
    counts_by_state: Dict[str, int]
    elapsed_s: float
    oldest_in_flight_age_s: Optional[float]
    expired_lease_count: int
    throughput_window_s: float
    accepted_in_window: int
    failed_in_window: int
    recent_failures: List[FailureGroup]
    truncated_failure_groups: bool
    halted_kind: Optional[str]
    halted_detail_code: str
    halted_at: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        """A plain-dict rendering suitable for YAML output (``cli.run``)."""
        return asdict(self)


def compute_status(
    store: ExecutionStore,
    run_id: str,
    *,
    throughput_window_s: float = DEFAULT_THROUGHPUT_WINDOW_S,
    max_failure_groups: int = DEFAULT_MAX_FAILURE_GROUPS,
    now: Optional[float] = None,
) -> RunStatus:
    """Compute a bounded :class:`RunStatus` digest for ``run_id``.

    Raises a plain ``KeyError`` when the run does not exist -- this module
    stays free of a dependency on the full execution error taxonomy; a caller
    that needs a typed error should call ``store.get_run`` first.
    """
    when = time.time() if now is None else now

    run, units, attempts = store.snapshot(run_id)
    if run is None:
        raise KeyError(f"no such run: {run_id!r}")

    counts: Counter = Counter(u.state.value for u in units)
    for state in UnitState:
        counts.setdefault(state.value, 0)

    # Age is measured from CLAIM, not from the unit's last update -- a renew
    # must not reset how long a unit has been in flight (it is the same
    # claim, just with a refreshed lease).
    in_flight_ages = [
        when - u.claimed_at
        for u in units
        if u.state is UnitState.CLAIMED and u.claimed_at is not None
    ]
    oldest_in_flight_age = max(in_flight_ages) if in_flight_ages else None

    expired_lease_count = sum(
        1
        for u in units
        if u.state is UnitState.CLAIMED
        and u.lease_expires_at is not None
        and u.lease_expires_at <= when
    )

    window_start = when - throughput_window_s
    accepted_in_window = sum(
        1
        for u in units
        if u.state is UnitState.ACCEPTED and u.accepted_at is not None and u.accepted_at >= window_start
    )

    fail_groups: Dict[str, List[Any]] = defaultdict(list)
    failed_in_window = 0
    for a in attempts:
        if a.kind is not AttemptKind.FAIL:
            continue
        if a.at < window_start:
            continue
        failed_in_window += 1
        fail_groups[_classify(a.error)].append(a)

    ordered_codes = sorted(
        fail_groups.items(),
        key=lambda kv: max(a.at for a in kv[1]),
        reverse=True,
    )
    recent_failures = [
        FailureGroup(
            error_code=code,
            count=len(group),
            last_unit_id=max(group, key=lambda a: a.at).unit_id,
            last_at=max(a.at for a in group),
        )
        for code, group in ordered_codes[:max_failure_groups]
    ]
    truncated = len(ordered_codes) > max_failure_groups

    return RunStatus(
        run_id=run.id,
        driver=run.driver,
        backend=run.backend,
        model=run.model,
        adapter_version=run.adapter_version,
        total_units=len(units),
        counts_by_state=dict(counts),
        elapsed_s=when - run.created_at,
        oldest_in_flight_age_s=oldest_in_flight_age,
        expired_lease_count=expired_lease_count,
        throughput_window_s=throughput_window_s,
        accepted_in_window=accepted_in_window,
        failed_in_window=failed_in_window,
        recent_failures=recent_failures,
        truncated_failure_groups=truncated,
        halted_kind=run.halted_kind,
        halted_detail_code=_classify(run.halted_detail),
        halted_at=run.halted_at,
    )


__all__ = [
    "DEFAULT_THROUGHPUT_WINDOW_S",
    "DEFAULT_MAX_FAILURE_GROUPS",
    "FailureGroup",
    "RunStatus",
    "compute_status",
]
