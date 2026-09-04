"""Apply a quota verdict to a caller's stated preference order.

The consumer-facing half of :mod:`llm_scripting_kit.usage_budget`. That module
answers "what is this one endpoint's quota state"; this one answers the
question a caller actually has -- **given the models I would delegate to, in
the order I prefer them, which one should run this?**

Worked, because the shape is easier to see than to state. Preference
``[opus, sol]``:

===============================  ====================================
state                            chosen
===============================  ====================================
both fine                        ``opus``  -- first preference wins
``opus`` out of quota            ``sol``   -- opus is disabled
``sol`` out of quota             ``opus``
both out of quota                the ``default``
``opus`` under quota, sol fine   ``sol``   -- opus is de-prioritized,
                                 not disabled, and loses to a peer
                                 that is not behind pace
both under quota                 ``opus``  -- neither is disabled, so
                                 the stated preference decides again
===============================  ====================================

Two rules produce all six rows, and the second is the one that is easy to get
wrong: **an out-of-quota endpoint is removed, an under-quota endpoint is only
moved.** Collapsing them -- treating "behind pace" as "unusable" -- would drop
a model that can still answer, which is the opposite of what pacing is for. And
the preference order survives both: it is the tiebreak inside each quota band,
so a caller's stated order is never silently reordered by anything except the
budget.

**This module ranks; it does not dispatch.** It answers which endpoint to use
and why, and the caller then does whatever it was going to do. That is the same
altitude split the rest of this package holds -- it classifies a halt and lets
the caller decide whether to stop -- and it is what lets a consumer with its own
selection rules (job-kit filters a preference order by capability requirements
before anything else) apply this as one input rather than inheriting a policy.

Endpoints that do not declare ``conserve_usage`` have no budget at all and rank
as AVAILABLE: opting in is what asks for pacing, so an endpoint that did not
opt in is never de-prioritized or disabled by it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .usage_budget import Budget, pinned_evaluate

#: Rank of each disposition within the ordering. Lower sorts first. Only two
#: values, because only two things can happen to a usable endpoint: it is
#: preferred, or it is de-prioritized behind its peers.
_RANK_AVAILABLE = 0
_RANK_UNDER_QUOTA = 1


@dataclass(frozen=True)
class Candidate:
    """One endpoint from the caller's preference list, with its verdict.

    ``budget`` is None when the endpoint declares no ``conserve_usage`` (or is
    not a harness entry) -- an unpaced endpoint, which ranks as available.
    ``preference_index`` is the endpoint's position in the ORIGINAL list, kept
    so a caller can see that the order it stated was honored inside its band.
    """

    endpoint: str
    preference_index: int
    budget: Optional[Budget] = None

    @property
    def usable(self) -> bool:
        return self.budget is None or self.budget.usable

    @property
    def deprioritized(self) -> bool:
        return self.budget is not None and self.budget.deprioritized

    def to_json(self) -> Dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "preference_index": self.preference_index,
            "budget": self.budget.to_json() if self.budget is not None else None,
        }


@dataclass(frozen=True)
class QuotaSelection:
    """The outcome. ``chosen`` is None only when nothing at all was usable.

    ``ranked`` is every usable candidate in the order they should be tried, so
    a caller with its own retry loop gets the whole fallback chain rather than
    just the head of it. ``disabled`` carries the out-of-quota ones, reported
    rather than dropped: "we skipped opus because its pool is spent" is a
    different fact from "opus was never a candidate", and only the first
    changes when the window resets.

    ``used_default`` says the answer came from the ``default`` rather than from
    the preference list -- the caller's own fallback, not a ranking decision.
    """

    chosen: Optional[str]
    ranked: tuple[Candidate, ...]
    disabled: tuple[Candidate, ...]
    used_default: bool = False
    reason: str = ""

    def to_json(self) -> Dict[str, Any]:
        return {
            "chosen": self.chosen,
            "used_default": self.used_default,
            "reason": self.reason,
            "ranked": [c.to_json() for c in self.ranked],
            "disabled": [c.to_json() for c in self.disabled],
        }


def rank_candidates(candidates: Sequence[Candidate]) -> "tuple[List[Candidate], List[Candidate]]":
    """Split into (ranked usable, disabled), pure and side-effect free.

    Exposed separately from :func:`choose_endpoint` because the ordering rule
    is the part worth testing and reusing; obtaining the budgets is I/O.
    The sort is STABLE on ``preference_index``, which is what keeps the
    caller's stated order intact inside each quota band.
    """
    usable = [c for c in candidates if c.usable]
    disabled = [c for c in candidates if not c.usable]
    usable.sort(
        key=lambda c: (
            _RANK_UNDER_QUOTA if c.deprioritized else _RANK_AVAILABLE,
            c.preference_index,
        )
    )
    return usable, disabled


def _reason(
    ranked: Sequence[Candidate],
    spent: Sequence[Candidate],
    unknown: Sequence[Candidate],
    default: Optional[str],
) -> str:
    """Say why the head was chosen, keeping the two exclusion causes apart.

    ``spent`` and ``unknown`` are deliberately separate parameters rather than
    one merged list: an endpoint that names no configured entry was excluded by
    a CONFIGURATION error, and calling that "out of quota" is a false claim
    about the account -- one that would send a reader looking at their usage for
    a typo. This sentence is what the `choose` verb prints, so it is the only
    place many callers ever learn why an endpoint was skipped.
    """
    spent_names = ", ".join(c.endpoint for c in spent)
    unknown_names = ", ".join(c.endpoint for c in unknown)
    if not ranked:
        causes = []
        if spent:
            causes.append(f"out of quota ({spent_names})")
        if unknown:
            causes.append(f"not configured ({unknown_names})")
        if not causes:
            return "no candidates were given"
        why = "every candidate was excluded: " + "; ".join(causes)
        if default is None:
            return f"{why}; no default was given"
        return f"{why}; fell back to '{default}'"
    head = ranked[0]
    passed_over = [c.endpoint for c in ranked[1:] if c.preference_index < head.preference_index]
    parts = [f"'{head.endpoint}'"]
    if head.deprioritized:
        parts.append("under quota, but the least-constrained candidate available")
    if passed_over:
        parts.append(f"preferred over {', '.join(passed_over)} (under quota)")
    if spent:
        parts.append(f"skipping {spent_names} (out of quota)")
    if unknown:
        parts.append(f"skipping {unknown_names} (not configured)")
    return "; ".join(parts)


def choose_endpoint(
    preferences: Sequence[str],
    *,
    default: Optional[str] = None,
    entries: Optional[Mapping[str, Any]] = None,
    project_root: Optional[str] = None,
) -> QuotaSelection:
    """Pick one endpoint from ``preferences``, applying each one's quota state.

    ``preferences`` is the caller's own order, most-preferred first. ``default``
    is what to use when every preference is out of quota -- the caller's
    fallback, returned with ``used_default`` set so the caller can tell the two
    apart rather than having to compare strings.

    An endpoint naming no configured entry is skipped as unusable and appears
    in ``disabled``; that is a configuration error the caller can see, and
    raising instead would make one typo take down a fallback chain that was
    otherwise fine.

    ``entries`` injects an already-discovered entry map (tests and callers that
    have one); otherwise the layered configuration and user registry are read.
    Verdicts go through :func:`~.usage_budget.pinned_evaluate`, so a selection
    made twice in one session returns the same answer.
    """
    if entries is None:
        from .models import discover_model_entries  # noqa: PLC0415 -- import cycle

        entries = discover_model_entries(project_root=project_root).entries

    candidates: List[Candidate] = []
    missing: List[Candidate] = []
    for index, name in enumerate(preferences):
        entry = entries.get(name)
        if entry is None:
            missing.append(Candidate(endpoint=name, preference_index=index, budget=None))
            continue
        spec = getattr(entry, "conserve_usage", None)
        budget = (
            pinned_evaluate(name, spec, getattr(entry, "harness", None))
            if spec is not None
            else None
        )
        candidates.append(Candidate(endpoint=name, preference_index=index, budget=budget))

    ranked, spent = rank_candidates(candidates)
    # An unknown name is not a quota fact. It rides in `disabled` because it is
    # equally unusable, but `_reason` is told about it SEPARATELY so the
    # sentence never calls a typo "out of quota".
    disabled = sorted(spent + missing, key=lambda c: c.preference_index)

    if ranked:
        chosen, used_default = ranked[0].endpoint, False
    else:
        chosen, used_default = default, default is not None
    return QuotaSelection(
        chosen=chosen,
        ranked=tuple(ranked),
        disabled=tuple(disabled),
        used_default=used_default,
        reason=_reason(ranked, spent, missing, default),
    )


__all__ = ["Candidate", "QuotaSelection", "choose_endpoint", "rank_candidates"]
