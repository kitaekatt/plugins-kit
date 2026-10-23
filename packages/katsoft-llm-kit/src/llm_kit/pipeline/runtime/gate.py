"""Adaptive concurrency gate for endpoint overload.

A *backpressure* or *rate_limit* halt (``PipelineHaltError`` of kind ``HALT_BACKPRESSURE`` / ``HALT_RATE_LIMIT``) is
the endpoint saying "not now", not "this call failed". :class:`AdaptiveGate`
turns it into waiting: the refused call's slot is released, the concurrency
limit halves (floor 1, once per wave), and the call is retried after the
endpoint's ``retry_after_s`` (else exponential backoff with equal jitter).
Every ``recover_after`` consecutive admitted calls raise the limit by one,
back toward ``jobs``. A call still refused after ``give_up_s`` fails loudly
with :class:`EndpointBusyError`, and so does every later call at once.
Every other error (quota, auth, credit, unreachable) passes through unchanged.

Callers outside :func:`call_llm_gated` supply their own error shapes through
three seams. ``is_backpressure(exc)`` decides which errors the gate waits out
(default :func:`is_transient_halt`). ``retry_after(exc)`` reads the
endpoint's wait hint from such an error (default :func:`halt_retry_after`,
which refuses an error that is not a ``PipelineHaltError``); pass
``retry_after=None`` to always back off. ``not_admitted(exc)`` marks an
error raised BEFORE the call reached the endpoint (an open circuit, an
exhausted unit budget): its slot is released without counting toward
recovery. ``llm_kit.pipeline.execution.deadlines.refused_before_dispatch`` is
that predicate for :func:`~llm_kit.pipeline.execution.deadlines.call_within_budget`.

The backoff, cap, give-up and recovery numbers are library defaults and
constructor parameters (``base_s``, ``cap_s``, ``give_up_s``,
``recover_after``); see the razor table in the plugin CLAUDE.md.

Stdlib-only; the clock, sleeper and jitter are injectable.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Optional, TypeVar

from llm_kit.pipeline.runtime import platform

T = TypeVar("T")

BASE_SECONDS = 30.0
CAP_SECONDS = 600.0
GIVE_UP_SECONDS = 3600.0
RECOVER_AFTER = 5

#: Halt kinds the gate waits out. Quota, credit and auth halts are not here.
TRANSIENT_KINDS = frozenset({platform.HALT_BACKPRESSURE, platform.HALT_RATE_LIMIT})


class EndpointBusyError(RuntimeError):
    """The endpoint refused admission for longer than the gate's overall cap."""


def is_transient_halt(exc: BaseException) -> bool:
    """Default backpressure predicate: a halt whose kind is in TRANSIENT_KINDS."""
    return isinstance(exc, platform.PipelineHaltError) and exc.kind in TRANSIENT_KINDS


def halt_retry_after(exc: BaseException) -> Optional[float]:
    """Default retry-after extractor: ``PipelineHaltError.retry_after_s``.

    Raises ``TypeError`` for any other error: a gate given its own
    ``is_backpressure`` must also say how to read the wait hint, or pass
    ``retry_after=None`` to always back off.
    """
    if not isinstance(exc, platform.PipelineHaltError):
        raise TypeError(
            "AdaptiveGate: backpressure error %s is not a PipelineHaltError; pass "
            "retry_after=<extractor> or retry_after=None to always back off"
            % type(exc).__name__
        )
    return exc.retry_after_s


def _never(exc: BaseException) -> bool:
    return False


class AdaptiveGate:
    def __init__(
        self,
        jobs: int,
        notice: Callable[[str], None] = lambda line: None,
        *,
        base_s: float = BASE_SECONDS,
        cap_s: float = CAP_SECONDS,
        give_up_s: float = GIVE_UP_SECONDS,
        recover_after: int = RECOVER_AFTER,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        is_backpressure: Callable[[BaseException], bool] = is_transient_halt,
        retry_after: Optional[Callable[[BaseException], Optional[float]]] = halt_retry_after,
        not_admitted: Callable[[BaseException], bool] = _never,
    ) -> None:
        self.jobs = max(1, jobs)
        self.limit = self.jobs
        self.notice = notice
        self.base_s = base_s
        self.cap_s = cap_s
        self.give_up_s = give_up_s
        self.recover_after = recover_after
        self.clock = clock
        self.sleep = sleep
        self.jitter = jitter
        self.is_backpressure = is_backpressure
        self.retry_after = retry_after
        self.not_admitted = not_admitted
        self.in_flight = 0
        self.refusals = 0
        self.gave_up: Optional[str] = None
        self.waiting = 0
        self._admitted = 0
        self._epoch = 0
        self._cond = threading.Condition()

    def _acquire(self) -> int:
        with self._cond:
            while True:
                if self.gave_up is not None:
                    raise EndpointBusyError(self.gave_up)
                if self.in_flight < self.limit:
                    break
                self.waiting += 1  # observable by tests: blocked in wait()
                try:
                    self._cond.wait()
                finally:
                    self.waiting -= 1
            self.in_flight += 1
            return self._epoch

    def _release(self, epoch: int, refused: bool, *, admitted: bool = True) -> int:
        """Free a slot; return the limit the refused call saw, or 0.

        A call that was not refused counts toward recovery only when it was
        ``admitted`` -- it reached the endpoint.
        """
        with self._cond:
            self.in_flight -= 1
            before = 0
            if refused:
                self.refusals += 1
                self._admitted = 0
                before = self.limit
                if epoch == self._epoch and self.limit > 1:
                    self.limit = max(1, self.limit // 2)
                    self._epoch += 1
            elif admitted:
                self._admitted += 1
                if self._admitted >= self.recover_after and self.limit < self.jobs:
                    self.limit += 1
                    self._admitted = 0
                    self._epoch += 1
            self._cond.notify_all()
            return before

    def _delay(self, attempt: int, retry_after_s: Optional[float]) -> float:
        if retry_after_s is not None and retry_after_s >= 0:
            return float(retry_after_s)
        step = min(self.cap_s, self.base_s * (2 ** attempt))
        return step / 2 + step / 2 * self.jitter()

    def _classify(self, epoch: int, exc: BaseException) -> bool:
        """Return whether ``exc`` is backpressure; release the slot if it is not."""
        try:
            backpressure = self.is_backpressure(exc)
            admitted = backpressure or not self.not_admitted(exc)
        except BaseException:
            self._release(epoch, refused=False)
            raise
        if not backpressure:
            self._release(epoch, refused=False, admitted=admitted)
        return backpressure

    def run(self, fn: Callable[[], T], *, label: Optional[str] = None) -> T:
        """Run ``fn`` under the gate, waiting out backpressure errors.

        ``label`` names the call in the notice line.
        """
        started = self.clock()
        attempt = 0
        while True:
            if self.gave_up is not None:
                raise EndpointBusyError(self.gave_up)
            epoch = self._acquire()
            try:
                result = fn()
            except BaseException as exc:
                if not self._classify(epoch, exc):
                    raise
                waited = self.clock() - started
                if waited >= self.give_up_s:
                    # Set gave_up BEFORE releasing, so a waiter woken by the
                    # release sees it and raises instead of taking the slot.
                    with self._cond:
                        if self.gave_up is None:
                            self.gave_up = (
                                "the endpoint refused admission for %ds; last: %s"
                                % (waited, str(exc)[:300])
                            )
                        self._cond.notify_all()
                    self._release(epoch, refused=True)
                    raise EndpointBusyError(self.gave_up) from exc
                before = self._release(epoch, refused=True)
                hint = None if self.retry_after is None else self.retry_after(exc)
                delay = min(
                    self._delay(attempt, hint),
                    max(0.0, self.give_up_s - waited),
                )
                self.notice(
                    "endpoint busy (%s); concurrency %d -> %d, retrying %sin %ds"
                    % (
                        str(exc)[:120], before, self.limit,
                        "" if label is None else label + " ", delay,
                    )
                )
                attempt += 1
                self.sleep(delay)
                continue
            self._release(epoch, refused=False)
            return result


def call_llm_gated(gate: AdaptiveGate, *args: Any, **kwargs: Any) -> Any:
    """``platform.call_llm(*args, **kwargs)`` under ``gate``."""
    return gate.run(lambda: platform.call_llm(*args, **kwargs))
