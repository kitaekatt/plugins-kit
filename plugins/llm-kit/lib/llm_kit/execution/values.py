"""Interrupt values shared by both execution products. Standard library only.

Importing this module never needs ``bootstrap_lib``: a product can read an
interrupt, test whether it lapsed, or compare canonical JSON on a machine
that links no contract. The rules here are single copies of rules the
contract (``bootstrap_lib.interrupt_contract``) also executes; a parity test
pins each to the contract's, so the copy cannot drift.

- :func:`interrupt_lapsed`: the lapse rule. Inclusive: an interrupt lapses AT
  its ``expires_at``; one with no expiry never lapses. Equal to
  ``interrupt_contract.lapsed``. The adapter's ``lapsed`` delegates to the
  contract; this function is the read-side form products use inside a record.
- :func:`record_lapsed`: whether an interrupt is unresolved and lapsed.
- :func:`canonical_json`: sorted keys, compact, ASCII, no NaN. Equal to
  ``interrupt_contract.canonical_json``.
- :class:`InterruptResolution`: the one immutable resolution of one interrupt.

The interrupt REQUEST and RECORD dataclasses stay with the products: their
field order and defaults differ (jobs: ``envelope`` first and required;
pipeline execution: ``kind`` first and ``envelope`` defaulted), and a record's
identity differs (job, attempt, continuation versus unit, fencing token,
policies).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional


def interrupt_lapsed(expires_at: Optional[float], now: float) -> bool:
    """Whether an interrupt expiring at ``expires_at`` has lapsed at ``now``.

    Inclusive: an interrupt lapses AT its ``expires_at``. An interrupt with
    no expiry never lapses.
    """
    return expires_at is not None and now >= expires_at


def record_lapsed(
    resolution: object, expires_at: Optional[float], now: float
) -> bool:
    """Whether an interrupt is unresolved and past its expiry at ``now``."""
    return resolution is None and interrupt_lapsed(expires_at, now)


def canonical_json(value: object) -> str:
    """The canonical text of a JSON value: sorted keys, compact, ASCII.

    Identical values give byte-identical text, whatever their key order.
    Raises ``ValueError`` for NaN or infinity.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


@dataclass(frozen=True)
class InterruptResolution:
    """The one immutable resolution of one interrupt.

    ``outcome`` is ``answered``, ``rejected`` or ``expired``, or -- in a job
    ledger of schema 13 or later only -- ``cancelled``. ``input`` is
    the operator's answer (answered only) and ``reason`` the operator's free
    text (rejected only). ``replayed`` is not stored: it is true when a
    resolve call matched the existing resolution and wrote nothing.
    """

    interrupt_id: str
    outcome: str
    resolved_at: float
    input: object = None
    reason: Optional[str] = None
    replayed: bool = field(default=False, compare=False)

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible resolution mapping."""
        return {
            "interrupt_id": self.interrupt_id,
            "outcome": self.outcome,
            "input": self.input,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
        }
