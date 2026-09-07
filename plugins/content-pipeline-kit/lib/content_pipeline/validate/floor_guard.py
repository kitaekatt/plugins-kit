"""Advisory-only diagnostics with a known-good acceptance gate.

A *floor guard* is an opt-in diagnostic: it flags candidates that look
suspicious (a metric outside a tolerated band), but it never blocks
acceptance by itself -- it is guidance, not a gate the library forces on
every pipeline. A consumer registers a floor guard only when it wants the
signal; a minimal pipeline runs with none registered.

The discipline that makes a guard trustworthy (the generic part ported here;
the specific signals stay project-side): **a floor-raising signal that
disagrees with known-good human work on more than a small fraction of cases
is wrong, not the humans.** So before a guard is shipped it is run over a
known-good corpus and its flag rate measured; if the rate is not comfortably
under a configurable threshold (default 0.10), the guard is *rejected* -- it
is a bad signal, not integrated. :func:`evaluate_guard` produces that verdict
for one guard; :func:`evaluate_guards` runs the per-signal gate over several
(each signal gated independently, since a union rate hides which signal is
noisy).

A guard is any callable ``item -> bool`` (True == flagged/suspicious). It is
pure and deterministic; population 0 yields a 0.0 flag rate (an empty corpus
never rejects a guard on no evidence).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

# A floor guard: returns True when it considers the item suspicious.
Guard = Callable[[object], bool]

DEFAULT_THRESHOLD = 0.10


def corpus_flag_rate(guard: Guard, known_good: Iterable[object]) -> float:
    """Fraction of the known-good corpus that ``guard`` flags.

    An empty corpus is rate 0.0 (no evidence against the guard).
    """
    total = 0
    flagged = 0
    for item in known_good:
        total += 1
        if guard(item):
            flagged += 1
    return (flagged / total) if total else 0.0


@dataclass(frozen=True)
class GuardReport:
    """The known-good gate verdict for one guard.

    - ``name`` -- the signal's name (for the per-signal gate).
    - ``flagged`` / ``population`` -- raw counts over the known-good corpus.
    - ``flag_rate`` -- ``flagged / population`` (0.0 for an empty corpus).
    - ``threshold`` -- the acceptance band the rate is tested against.
    - ``accepted`` -- True when ``flag_rate < threshold``; a guard that
      disagrees with known-good work too often is NOT accepted (not shipped).
    """

    name: str
    flagged: int
    population: int
    flag_rate: float
    threshold: float
    accepted: bool


def evaluate_guard(
    guard: Guard,
    known_good: Iterable[object],
    *,
    name: str = "",
    threshold: float = DEFAULT_THRESHOLD,
) -> GuardReport:
    """Run ``guard`` over the known-good corpus and gate it on the flag rate.

    The guard is accepted only when its flag rate is strictly under
    ``threshold`` -- the "known-good <10% acceptance gate". An accepted guard
    is safe to use as an advisory signal; a rejected one is a bad signal and
    should not ship. The rate itself is :func:`corpus_flag_rate`'s to compute
    -- this function delegates rather than re-deriving the flagged/population
    ratio, so the two can never drift apart.
    """
    known_good = list(known_good)
    population = len(known_good)
    flagged = sum(1 for item in known_good if guard(item))
    flag_rate = corpus_flag_rate(guard, known_good)
    return GuardReport(
        name=name,
        flagged=flagged,
        population=population,
        flag_rate=flag_rate,
        threshold=threshold,
        accepted=flag_rate < threshold,
    )


def evaluate_guards(
    guards: Mapping[str, Guard],
    known_good: Iterable[object],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict:
    """Per-signal gate: evaluate each named guard independently.

    Returns ``{name: GuardReport}``. Each guard is gated on its own flag rate
    (a union rate would hide which signal is noisy), so a consumer ships only
    the accepted signals.
    """
    known_good = list(known_good)
    return {
        name: evaluate_guard(guard, known_good, name=name, threshold=threshold)
        for name, guard in guards.items()
    }


def flag(guard: Guard, items: Iterable[object]) -> list:
    """Return the items ``guard`` flags -- the advisory application.

    Used only after a guard has passed :func:`evaluate_guard`; the flagged
    items are surfaced for human review, never auto-rejected.
    """
    return [item for item in items if guard(item)]
