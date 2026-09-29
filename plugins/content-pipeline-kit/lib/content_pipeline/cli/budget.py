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
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from content_pipeline.llm.platform import (
    PipelineHaltError,
    classify_halt_text,
)


class BudgetStop(Exception):
    """A bulk sweep hit a hard-stop and halted with partial progress.

    - ``reason`` -- the halt kind (``PipelineHaltError.kind``: auth / rate_limit /
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
    "BudgetStop",
    "preflight_check",
    "check_response",
]
