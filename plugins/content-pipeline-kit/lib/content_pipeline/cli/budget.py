"""Budget guard / hard-stop on 429/401, auth-expiry preflight.

A bulk CLI run checks its credentials before starting (auth-expiry preflight,
so a run does not burn partial progress before discovering an expired key) and
halts cleanly mid-sweep on a hard-stop (a 429 rate-limit or 401 auth-failure
that persists across calls -- retrying the next unit would only burn budget
against a dead credential). The whole point is a CLEAN stop with PARTIAL
progress reported, so a resume loop picks up where it left off.

This module (per the dependency contract) may import ``llm`` for the
:class:`~content_pipeline.llm.platform.HaltError` taxonomy and stdlib -- nothing
else from ``content_pipeline``. It consumes the halt signal the ``llm`` layer
already raises; it does not re-implement provider-error classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence, Tuple

from content_pipeline.llm.platform import (
    HaltError,
    classify_halt_text,
)


class BudgetStop(Exception):
    """A bulk sweep hit a hard-stop and halted with partial progress.

    - ``reason`` -- the halt kind (``HaltError.kind``: auth / rate_limit /
      insufficient_credit).
    - ``unit_id`` -- the unit whose call tripped the stop (``""`` for a
      preflight stop before any unit ran).
    - ``done`` / ``remaining`` -- units completed before the stop and units not
      yet attempted, so the driver emits an accurate partial summary and a
      resume loop knows what is left.
    """

    def __init__(
        self,
        reason: str,
        *,
        unit_id: str = "",
        done: Optional[Sequence[Any]] = None,
        remaining: Optional[Sequence[Any]] = None,
    ) -> None:
        self.reason = reason
        self.unit_id = unit_id
        self.done: List[Any] = list(done or [])
        self.remaining: List[Any] = list(remaining or [])
        super().__init__(
            f"budget stop ({reason})"
            + (f" at {unit_id!r}" if unit_id else "")
            + f": {len(self.done)} done, {len(self.remaining)} remaining"
        )


def preflight_check(probe: Callable[[], Any]) -> None:
    """Run ``probe`` before a sweep; re-raise a halt as :class:`BudgetStop`.

    ``probe`` is a cheap credential/budget check the caller supplies (e.g. a
    zero-cost auth ping). A :class:`~content_pipeline.llm.platform.HaltError`
    from the probe means the run would burn against a dead credential, so it is
    re-raised as a :class:`BudgetStop` with no units done -- the auth-expiry
    preflight. A probe that returns normally lets the run proceed; any non-halt
    exception propagates unchanged (it is not a persistent-credential problem).
    """
    try:
        probe()
    except HaltError as exc:
        raise BudgetStop(exc.kind) from exc


def check_response(response: Any) -> None:
    """Raise :class:`~content_pipeline.llm.platform.HaltError` on a hard-stop response.

    Inspects a response's text channel (``response.text`` or ``str(response)``)
    for a persistent-failure marker via ``llm.classify_halt_text`` -- the
    text-channel hard-stop the CLI backend surfaces even on a 200 envelope. A
    marker raises ``HaltError`` (so a surrounding :func:`guarded_sweep` catches
    it); a clean response returns ``None``.
    """
    text = getattr(response, "text", None)
    if text is None:
        text = str(response)
    kind = classify_halt_text(text)
    if kind is not None:
        raise HaltError(kind, text[:200])


@dataclass
class SweepResult:
    """Outcome of a :func:`guarded_sweep`.

    - ``done`` -- ``(unit, result)`` for units the worker completed.
    - ``errors`` -- ``(unit, message)`` for units whose worker raised a
      non-halt error (isolated, the sweep continued).
    - ``halted`` -- the :class:`BudgetStop` that stopped the sweep, or ``None``
      when the sweep ran to completion.
    - ``remaining`` -- units not attempted (non-empty only after a halt).
    """

    done: List[Tuple[Any, Any]] = field(default_factory=list)
    errors: List[Tuple[Any, str]] = field(default_factory=list)
    halted: Optional[BudgetStop] = None
    remaining: List[Any] = field(default_factory=list)

    @property
    def stopped(self) -> bool:
        return self.halted is not None


def guarded_sweep(
    units: Sequence[Any],
    worker: Callable[[Any], Any],
    *,
    isolate_errors: bool = True,
) -> SweepResult:
    """Run ``worker`` over ``units``, halting cleanly on the first hard-stop.

    For each unit the worker runs; a
    :class:`~content_pipeline.llm.platform.HaltError` halts the whole sweep
    (records a :class:`BudgetStop` carrying done/remaining and stops -- the
    remaining units are NOT attempted, since the credential is dead). A non-halt
    exception is isolated per unit when ``isolate_errors`` (recorded on
    ``errors``, the sweep continues) or propagated otherwise. Returns a
    :class:`SweepResult`; the caller reports partial progress and can resume
    from ``remaining``.
    """
    units = list(units)
    result = SweepResult()
    for index, unit in enumerate(units):
        try:
            outcome = worker(unit)
        except HaltError as exc:
            remaining = units[index + 1 :]
            done_units = [u for u, _ in result.done]
            result.halted = BudgetStop(
                exc.kind,
                unit_id=str(unit),
                done=done_units,
                remaining=remaining,
            )
            result.remaining = remaining
            break
        except Exception as exc:  # noqa: BLE001 -- isolate one unit's failure
            if not isolate_errors:
                raise
            result.errors.append((unit, str(exc)))
            continue
        result.done.append((unit, outcome))
    return result


__all__ = [
    "BudgetStop",
    "preflight_check",
    "check_response",
    "SweepResult",
    "guarded_sweep",
]
