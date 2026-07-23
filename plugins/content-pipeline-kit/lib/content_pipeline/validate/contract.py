"""The Validator protocol and tiered rejection kinds.

A Validator is a pure function from (candidate value, context) to a
verdict. Every call site -- in-agent validation during generation, and
post-hoc validation during audit -- calls the SAME validator instance, so
the rule set cannot drift between the two. Rejections are tiered:

- hard: the candidate is unacceptable; regeneration is required.
- soft: the candidate is acceptable but flagged; a human may want to look.
- advisory: informational only, never blocks acceptance.
"""

from typing import Protocol


class Validator(Protocol):
    def __call__(self, candidate, context) -> "ValidationResult":
        ...


class ValidationResult:
    """Outcome of running a Validator: tier + message + optional riders."""

    def __init__(self, tier: str, message: str, riders: dict | None = None):
        self.tier = tier
        self.message = message
        self.riders = riders or {}
