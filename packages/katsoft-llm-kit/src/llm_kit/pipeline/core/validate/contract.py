"""The Validator protocol and tiered rejection kinds.

A Validator is a pure function from (candidate value, context) to a list of
:class:`Rejection` (empty == accept). Every call site -- in-agent validation
during generation, and post-hoc validation during audit -- runs the SAME
validators through the SAME :func:`run_rules` helper, so the rule set cannot
drift between the two. This one-rule-set-many-call-sites boundary prevents
embedded, post-hoc, and standalone copies from becoming subtly different
versions of the same rules.

Rejections are tiered by :class:`Severity`:

- ``HARD`` -- a terminal contract violation. Always blocks acceptance;
  regeneration is required.
- ``SOFT`` -- a violation the default policy still blocks on, but which a
  consumer may choose to demote (an advisory-but-enforced rule).
- ``ADVISORY`` -- audit-only. Recorded for visibility, never blocks (the
  escape-valve / floor-guard tier).

A :class:`Rejection` carries a ``kind`` (the machine-readable category), a
``severity``, a human ``detail``, an optional ``rule_id`` (which data rule
fired), and a structured ``payload`` for downstream consumers that need the
facts, not the prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, List, Mapping, Protocol, Sequence


class Severity(str, Enum):
    """A rejection's blocking tier."""

    HARD = "hard"
    SOFT = "soft"
    ADVISORY = "advisory"


@dataclass(frozen=True)
class Rejection:
    """One reason a candidate was rejected.

    - ``kind`` -- machine-readable category (``"markup_mismatch"``,
      ``"missing_keys"``, ...); the consumer's stable vocabulary.
    - ``severity`` -- :class:`Severity`; drives :func:`blocks`.
    - ``detail`` -- human-facing description for the feedback surface.
    - ``rule_id`` -- the data rule that fired, when applicable.
    - ``payload`` -- structured facts a downstream stage can consume without
      re-parsing ``detail`` (e.g. the offending token, a measured width).
    """

    kind: str
    severity: Severity = Severity.HARD
    detail: str = ""
    rule_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


class ValidationError(Exception):
    """Raised by :func:`assert_valid` when any blocking rejection is present.

    Aggregates every blocking rejection's detail into one message, so a caller
    fixes them in a single pass rather than one crash at a time. The rejections are
    available on ``.rejections`` for programmatic handling.
    """

    def __init__(self, rejections: Sequence[Rejection]):
        self.rejections = tuple(rejections)
        super().__init__("\n".join(r.detail for r in self.rejections))


class Validator(Protocol):
    """A rule: candidate + context -> list of rejections (empty == accept)."""

    def __call__(self, candidate: Any, context: Any) -> Sequence[Rejection]:
        ...


def blocks(rejection: Rejection, *, block_soft: bool = True) -> bool:
    """True when ``rejection`` blocks acceptance.

    ``HARD`` always blocks; ``ADVISORY`` never blocks. ``SOFT`` blocks under
    the default policy (``block_soft=True``) -- an advisory-but-enforced rule
    is still something the pipeline should fix, not ship -- and a consumer
    that wants soft rejections to be non-blocking passes ``block_soft=False``.
    """
    if rejection.severity is Severity.HARD:
        return True
    if rejection.severity is Severity.ADVISORY:
        return False
    return block_soft


def is_rejecting(rejections: Iterable[Rejection], *, block_soft: bool = True) -> bool:
    """True when any rejection in the list blocks acceptance.

    The single accept/reject predicate every call site shares, so "failed
    validation during generation" and "failed validation during audit" mean
    exactly the same thing.
    """
    return any(blocks(r, block_soft=block_soft) for r in rejections)


def run_rules(
    candidate: Any,
    context: Any,
    validators: Sequence[Validator],
) -> List[Rejection]:
    """Run every validator over ``(candidate, context)``; concatenate results.

    The shared helper both the in-loop and post-hoc sites call. Results are
    sorted deterministically by ``(kind, detail)`` so successive runs over the
    same candidate produce byte-identical output (cache- and retry-friendly).
    """
    rejections: List[Rejection] = []
    for validator in validators:
        rejections.extend(validator(candidate, context))
    rejections.sort(key=lambda r: (r.kind, r.detail))
    return rejections


def assert_valid(rejections: Sequence[Rejection], *, block_soft: bool = True) -> None:
    """Raise :class:`ValidationError` if any rejection blocks acceptance.

    The post-hoc / hard-gate, raise-on-violation surface. Non-blocking
    (advisory / demoted-soft)
    rejections do not raise.
    """
    blocking = [r for r in rejections if blocks(r, block_soft=block_soft)]
    if blocking:
        raise ValidationError(blocking)


def format_rejections(
    rejections: Sequence[Rejection],
    *,
    block_soft: bool = True,
    valid_token: str = "VALID",
    header: str = "REJECTED. Fix the following and resubmit:",
) -> str:
    """Render rejections as agent-facing feedback text.

    Only blocking rejections are shown (advisory ones live on the audit
    surface, not the feedback string). When nothing blocks, the literal
    ``valid_token`` is returned. Each blocking rejection is one line, prefixed
    by its kind (and ``rule_id`` when set) in square brackets.
    """
    blocking = [r for r in rejections if blocks(r, block_soft=block_soft)]
    if not blocking:
        return valid_token
    lines = [header]
    for r in blocking:
        tag = f"{r.kind}:{r.rule_id}" if r.rule_id else r.kind
        lines.append(f"- [{tag}] {r.detail}")
    return "\n".join(lines)
