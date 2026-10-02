"""Budget guard / hard-stop on 429/401, auth-expiry preflight.

A CLI run checks its credentials before starting (auth-expiry preflight,
so a run does not burn partial progress before discovering an expired key) and
halts cleanly mid-run on a hard-stop (a 429 rate-limit or 401 auth-failure
that persists across calls -- retrying the next unit would only burn budget
against a dead credential). The whole point is a CLEAN stop with PARTIAL
progress reported, so a resume loop picks up where it left off.

This module (per the dependency contract) may import ``llm`` for the
:class:`~content_pipeline.llm.platform.PipelineHaltError` taxonomy and stdlib -- nothing
else from ``content_pipeline``. It consumes the halt signal the ``llm`` layer
already raises; it does not re-implement provider-error classification.

That one-way rule is also WHY :func:`spend_stop` lives here rather than in
``llm``: the spend ledger's verdicts have to become a :class:`BudgetStop`
somewhere, and ``llm`` importing ``BudgetStop`` would invert the layer. So the
ledger raises its own
:class:`~content_pipeline.llm.spend_ledger.SpendCapExceeded` /
:class:`~content_pipeline.llm.spend_ledger.SpendLedgerHalted` (both
:class:`~content_pipeline.llm.platform.BudgetExceededError` subclasses) and this
module translates them, exactly as :func:`preflight_check` translates
``PipelineHaltError``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Sequence

from content_pipeline.llm.platform import (
    PipelineHaltError,
    classify_halt_text,
)
from content_pipeline.llm.spend_ledger import (
    SpendCapExceeded,
    SpendLedgerHalted,
)

#: :attr:`BudgetStop.reason` when the cross-process spend ledger refused a
#: reservation because granting it would push spend past the cap. Spelled like
#: the ``PipelineHaltError.kind`` values (``HALT_AUTH`` and friends) that supply
#: every other ``reason``: a lowercase machine-readable token.
SPEND_CAP = "spend_cap"

#: :attr:`BudgetStop.reason` when the ledger refused a reservation because its
#: halt row is set -- an operator (or an overbilled settle) stopped the run.
SPEND_HALT = "spend_halt"


class BudgetStop(Exception):
    """A bulk sweep hit a hard-stop and halted with partial progress.

    - ``reason`` -- the halt kind (``PipelineHaltError.kind``: auth / rate_limit /
      insufficient_credit), or a spend-ledger verdict (:data:`SPEND_CAP` /
      :data:`SPEND_HALT`) when :func:`spend_stop` raised it.
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
    zero-cost auth ping). A :class:`~content_pipeline.llm.platform.PipelineHaltError`
    from the probe means the run would burn against a dead credential, so it is
    re-raised as a :class:`BudgetStop` with no units done -- the auth-expiry
    preflight. A probe that returns normally lets the run proceed; any non-halt
    exception propagates unchanged (it is not a persistent-credential problem).
    """
    try:
        probe()
    except PipelineHaltError as exc:
        raise BudgetStop(exc.kind) from exc


@contextmanager
def spend_stop(
    done: Sequence[Any],
    remaining: Sequence[Any],
    *,
    unit_id: str = "",
) -> Iterator[None]:
    """Translate a spend-ledger verdict raised in the block into :class:`BudgetStop`.

    Wrap the work that may hit the cross-process ledger (in practice a
    ``call_llm(spend=...)`` for one unit). Two ledger exceptions are budget
    verdicts and become a clean partial stop, mirroring
    :func:`preflight_check`'s ``PipelineHaltError`` translation:

    - :class:`~content_pipeline.llm.spend_ledger.SpendCapExceeded` ->
      ``BudgetStop(SPEND_CAP, ...)``
    - :class:`~content_pipeline.llm.spend_ledger.SpendLedgerHalted` ->
      ``BudgetStop(SPEND_HALT, ...)``

    ``done`` / ``remaining`` / ``unit_id`` are copied onto the
    :class:`BudgetStop` so the driver reports accurate partial progress and a
    resume loop knows what is left.

    EVERYTHING ELSE PROPAGATES UNCHANGED, deliberately -- as a budget verdict
    each would be a lie, and a consumer that only catches :class:`BudgetStop`
    would then report hitting its cap when it did not:

    - ``sqlite3.OperationalError`` -- the busy timeout was exhausted. An
      unreachable ledger is infrastructure; the ledger admits nothing and
      fails closed.
    - ``ValueError`` -- a misconfiguration (a refused ``resume`` of an
      overbilled halt, or a reservation whose amount cannot be determined).
    - ``StaleReservationError`` / ``LedgerStateInvalid`` /
      ``LedgerIdentityChanged`` -- ledger-state faults, not verdicts.
    - ``KeyError`` -- an unknown model in the pricing table.

    None of those are caught here, which is why they are named rather than
    re-raised: adding a branch for any of them is the change this function
    exists to refuse.
    """
    try:
        yield
    except SpendCapExceeded as exc:
        raise BudgetStop(
            SPEND_CAP, unit_id=unit_id, done=done, remaining=remaining
        ) from exc
    except SpendLedgerHalted as exc:
        raise BudgetStop(
            SPEND_HALT, unit_id=unit_id, done=done, remaining=remaining
        ) from exc


def check_response(response: Any) -> None:
    """Raise :class:`~content_pipeline.llm.platform.PipelineHaltError` on a hard-stop response.

    Inspects a response's text channel (``response.text`` or ``str(response)``)
    for a persistent-failure marker via ``llm.classify_halt_text`` -- the
    text-channel hard-stop the CLI backend surfaces even on a 200 envelope. A
    marker raises ``PipelineHaltError`` (which a caller's halt handling catches); a clean response returns ``None``.
    """
    text = getattr(response, "text", None)
    if text is None:
        text = str(response)
    kind = classify_halt_text(text)
    if kind is not None:
        raise PipelineHaltError(kind, text[:200])


__all__ = [
    "SPEND_CAP",
    "SPEND_HALT",
    "BudgetStop",
    "preflight_check",
    "spend_stop",
    "check_response",
]
