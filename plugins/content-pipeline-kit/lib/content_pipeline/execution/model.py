"""Execution-store record shapes, states, and the error taxonomy.

Pure data: no SQLite here (that is ``execution.store``'s job) and no status
math (``execution.status``). Every dataclass round-trips through
``dataclasses.asdict`` cleanly, which is what ``status.py`` relies on to
serialize a bounded digest.

Nullable usage, never zero-for-unknown
---------------------------------------

:class:`UsageRecord` fields are ``Optional[int]``. A driver that cannot see
token counts (a background session, a workflow agent) must record ``None``,
not ``0`` -- a ``0`` would silently understate cost in any aggregate that
sums it. Only a transport that genuinely reports zero usage writes ``0``.

Unit state machine
-------------------

::

    PENDING --claim--> CLAIMED --accept--> ACCEPTED (terminal)
                           |  \\
                           |   --fail(terminal=True)--> FAILED (terminal)
                           |
                           +--fail(terminal=False)--> PENDING (retry)
                           |
                           +--lease expiry, reclaimed by a new claim--> CLAIMED

``ACCEPTED`` and ``FAILED`` are terminal: no further claim, renew, accept, or
fail is legal against them (``execution.store`` raises
:class:`TerminalStateError`). This is the store-level primitive only --
submit-time adjudication (parsing, validation, the accepted-verdict-is-final
rule) belongs to the adapter/protocol layer added in a later phase.

Accepted text and the apply tri-state (A-min.2)
-------------------------------------------------

``UnitRecord.accepted_text`` is the durable text recorded by
:meth:`~content_pipeline.execution.store.ExecutionStore.accept_unit` at
submit time (plan D1: "the verdict is recorded durably with the accepted
text"). ``execution.controller.finalize_run`` re-parses it mechanically via
the adapter's ``parse_fn`` -- it never re-validates and never flips a verdict.

Whether a unit's apply has run is NOT a ``units`` column -- it is derived from
the append-only attempts log via the :data:`AttemptKind.APPLY_STARTED` /
:data:`AttemptKind.APPLY_SUCCEEDED` pair a finalize records around each
adapter ``apply`` call (plan D6). A unit whose last apply-related attempt is
``APPLY_STARTED`` with no following ``APPLY_SUCCEEDED`` is ``apply_unknown``;
resuming finalize with any unit in that state refuses to proceed unless the
adapter supplies a reconciliation hook (fail closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional


class UnitState(str, Enum):
    """A unit's position in the state machine documented above.

    ``SKIPPED`` (A-min.2) is a second terminal state, added alongside
    ``FAILED`` for a unit a gate or freshness check decided will never be
    generated -- see ``execution.controller``'s "Terminal skips" section and
    ``execution.wave``'s graph-predecessor semantics. ``UnitState`` is a
    ``str`` Enum and the backing ``units.state`` column is ``TEXT``, so this
    addition needs no schema migration -- a fresh string value round-trips
    through the existing column exactly like any other member.
    """

    PENDING = "pending"
    CLAIMED = "claimed"
    ACCEPTED = "accepted"
    FAILED = "failed"
    SKIPPED = "skipped"


TERMINAL_STATES = (UnitState.ACCEPTED, UnitState.FAILED, UnitState.SKIPPED)

# The error-string convention ``execution.controller`` uses for a terminal
# skip's ``fail_unit(error=...)`` text (e.g. ``"skip:up_to_date"``,
# ``"skip:gate:<name>:<reason>"``). Shared here, rather than only documented
# in ``controller``'s module docstring, so ``execution.status`` can filter on
# it without importing ``controller`` (which itself imports ``pipeline.
# single_pass`` -- a dependency ``status`` deliberately does not carry).
SKIP_ERROR_PREFIX = "skip:"


class AttemptKind(str, Enum):
    """What one recorded attempt/event row represents."""

    CLAIM = "claim"
    EXPIRE = "expire"  # a lease-expiry reclaim, recorded before the new claim
    RENEW = "renew"
    ACCEPT = "accept"
    FAIL = "fail"
    SUPERSEDED = "superseded"  # a fenced-out accept/fail: recorded, never applied (invariant 4)
    APPLY_STARTED = "apply_started"  # finalize is about to call the adapter's apply (D6)
    APPLY_SUCCEEDED = "apply_succeeded"  # the adapter's apply returned without raising (D6)


@dataclass(frozen=True)
class UsageRecord:
    """Nullable per-attempt token usage. ``None`` means unknown, never 0."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_hit_tokens: Optional[int] = None


@dataclass(frozen=True)
class RunRecord:
    """One tracked run's identity and halt state.

    ``driver`` / ``backend`` / ``model`` / ``adapter_version`` are recorded
    explicitly rather than inferred, so an incompatible resume can refuse
    instead of guessing (the plan's D1 adapter-identity requirement, recorded
    here even though adapter refusal itself is a later-phase concern).

    There is deliberately no ``paused`` field: operator pause/resume is D4's
    A-min.2 concern (the plan assigns pause/halt run-control semantics
    together in phase A-min.2). A-min.1 ships only halt, and halt alone.

    ``environment`` (item 5's anchor) is the environment snapshot taken at
    create-run time (``execution.adapter.WorkerEnvironment.snapshot()``), in
    the orchestrator's own shell where it is known correct. ``None`` means
    no snapshot was recorded (an adapter-less create, or a mount whose
    adapter declared nothing) -- treated as an empty recorded snapshot by
    ``execution.adapter.require_compatible_environment``. Never enters the
    status digest (invariant 6).

    ``dispatcher_id`` / ``dispatcher_lease_expires_at`` / ``dispatcher_fence``
    (B1) are the run-level LAUNCHER-ELECTION lease -- a distinct lease from a
    per-unit claim lease. At most one background-lane dispatcher process may
    hold it at a time, so an accidental second dispatcher exits without
    launching duplicate work. ``dispatcher_fence`` is a monotonically
    increasing counter bumped on every successful
    :meth:`~content_pipeline.execution.store.ExecutionStore.acquire_dispatcher_lease`
    call (fresh acquire OR the same dispatcher re-acquiring), so a stale
    former holder's renew/release is rejected the same way a stale unit
    fencing token is (see :class:`StaleDispatcherLeaseError`).
    """

    id: str
    driver: str
    backend: str
    model: str
    adapter_version: str
    created_at: float
    halted_kind: Optional[str] = None
    halted_detail: Optional[str] = None
    halted_at: Optional[float] = None
    environment: Optional[Mapping[str, str]] = None
    dispatcher_id: Optional[str] = None
    dispatcher_lease_expires_at: Optional[float] = None
    dispatcher_fence: int = 0

    @property
    def halted(self) -> bool:
        return self.halted_kind is not None


@dataclass(frozen=True)
class UnitRecord:
    """One unit's row: identity, state, and current claim/lease if any."""

    run_id: str
    unit_id: str
    ordinal: int
    state: UnitState
    created_at: float
    updated_at: float
    claimed_by: Optional[str] = None
    claimed_at: Optional[float] = None
    fencing_token: int = 0
    lease_expires_at: Optional[float] = None
    accepted_at: Optional[float] = None
    failed_at: Optional[float] = None
    accepted_text: Optional[str] = None


@dataclass(frozen=True)
class AttemptRecord:
    """One append-only attempt/event row."""

    id: int
    run_id: str
    unit_id: str
    kind: AttemptKind
    at: float
    worker_id: Optional[str] = None
    fencing_token: Optional[int] = None
    error: Optional[str] = None
    usage: Optional[UsageRecord] = None


@dataclass(frozen=True)
class ClaimResult:
    """What a successful claim hands the caller."""

    fencing_token: int
    lease_expires_at: float


@dataclass(frozen=True)
class DispatchRecord:
    """One row of the ``dispatches`` table (B1): a background-lane launch of
    ``unit_id``, distinct from -- and layered on top of -- the unit's own
    store-level claim (``UnitRecord.claimed_by`` / ``fencing_token``).

    Per the author ruling this ships against: the DRIVER mints ``worker_id``
    before launching the Claude session, and the worker claims the unit with
    that id (so "one agent claims one unit" is enforced at the existing
    per-unit fencing layer, unchanged). ``session_id`` -- the Claude session
    id -- is recorded ALONGSIDE ``worker_id`` once known (it is not known at
    launch time, since ``claude --bg`` prints it only after the process
    spawns), never in place of it, and every later status decision about
    this dispatch keys on ``session_id``, never on a PID.

    ``settled_at`` / ``outcome`` are ``None`` while the dispatch is open (the
    worker has not yet reached a terminal outcome from the dispatcher's point
    of view). A guarded uniqueness constraint on
    ``(run_id, unit_id) WHERE settled_at IS NULL`` (see the store module's
    migration) enforces at most one OPEN dispatch per unit at the database
    level -- a second ``record_dispatch`` call for a unit that already has an
    open dispatch fails with a ``sqlite3.IntegrityError`` rather than
    silently launching a duplicate worker.
    """

    id: int
    run_id: str
    unit_id: str
    worker_id: str
    session_id: Optional[str]
    launched_at: float
    settled_at: Optional[float]
    outcome: Optional[str]
    cli_version: Optional[str]


class ExecutionError(Exception):
    """Base class for every execution-store error."""


class UnknownRunError(ExecutionError):
    """No run with this id exists."""


class UnknownUnitError(ExecutionError):
    """No unit with this (run_id, unit_id) exists."""


class DuplicateUnitError(ExecutionError):
    """A unit id was registered twice within one run."""


class RunHaltedError(ExecutionError):
    """A claim was attempted against a halted run (D4: halt blocks claims)."""

    def __init__(self, run_id: str, kind: str) -> None:
        self.run_id = run_id
        self.kind = kind
        super().__init__(f"run {run_id!r} is halted ({kind}); new claims are blocked")


class AlreadyClaimedError(ExecutionError):
    """The unit is CLAIMED with an unexpired lease held by someone else."""


class TerminalStateError(ExecutionError):
    """The unit is already ACCEPTED or FAILED; no further transition is legal."""


class NotClaimedError(ExecutionError):
    """A renew/accept/fail was attempted against a unit that is not CLAIMED."""


class NotAcceptedError(ExecutionError):
    """An apply-started/apply-succeeded record was attempted against a unit
    that is not ACCEPTED (finalize only ever applies accepted units, D1/D6)."""


class StaleFenceError(ExecutionError):
    """The fencing token presented does not match the unit's current token."""

    def __init__(self, run_id: str, unit_id: str, presented: int, current: int) -> None:
        self.run_id = run_id
        self.unit_id = unit_id
        self.presented = presented
        self.current = current
        super().__init__(
            f"stale fencing token for {run_id!r}/{unit_id!r}: "
            f"presented {presented}, current {current}"
        )


class StaleDispatcherLeaseError(ExecutionError):
    """A dispatcher-lease renew/release was attempted with a
    ``(dispatcher_id, fence)`` pair that does not match the run's current
    holder -- the run-level analogue of :class:`StaleFenceError`, checked
    first (before any other validation) the same way."""

    def __init__(
        self,
        run_id: str,
        dispatcher_id: str,
        fence: int,
        current_dispatcher_id: Optional[str],
        current_fence: int,
    ) -> None:
        self.run_id = run_id
        self.dispatcher_id = dispatcher_id
        self.fence = fence
        self.current_dispatcher_id = current_dispatcher_id
        self.current_fence = current_fence
        super().__init__(
            f"stale dispatcher lease for run {run_id!r}: presented "
            f"({dispatcher_id!r}, {fence}), current "
            f"({current_dispatcher_id!r}, {current_fence})"
        )


class NoOpenDispatchError(ExecutionError):
    """``settle_dispatch`` was called for a ``(run_id, unit_id)`` with no
    open (``settled_at IS NULL``) dispatch row to settle."""

    def __init__(self, run_id: str, unit_id: str) -> None:
        self.run_id = run_id
        self.unit_id = unit_id
        super().__init__(
            f"no open dispatch for {run_id!r}/{unit_id!r} to settle"
        )


__all__ = [
    "UnitState",
    "TERMINAL_STATES",
    "SKIP_ERROR_PREFIX",
    "AttemptKind",
    "UsageRecord",
    "RunRecord",
    "UnitRecord",
    "AttemptRecord",
    "ClaimResult",
    "DispatchRecord",
    "ExecutionError",
    "UnknownRunError",
    "UnknownUnitError",
    "DuplicateUnitError",
    "RunHaltedError",
    "AlreadyClaimedError",
    "TerminalStateError",
    "NotClaimedError",
    "NotAcceptedError",
    "StaleFenceError",
    "StaleDispatcherLeaseError",
    "NoOpenDispatchError",
]
