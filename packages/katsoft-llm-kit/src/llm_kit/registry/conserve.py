"""Conserve-usage metadata: pacing statuses, pools, ``Budget`` and the declaration parser.

The values a quota-paced model declaration and a pacing verdict are made of.
They live in the registry because the endpoint registry parses a
``conserve_usage`` declaration (``ConserveSpec``) and the adapters' raw usage
readers and the dispatch pacing policy both return and consume ``Budget``
verdicts; this module imports none of them, so every one of those packages can
import it from below.

THE RULE these values encode, two thresholds and two different consequences.
Let

    r = remaining_percentage / 100
    t = (resets_at - now) / window_seconds

    r <= 0   -> OUT OF QUOTA.   The pool is spent. The model is DISABLED: a
                                call would fail, so it is removed from
                                selection entirely.
    r < t    -> UNDER QUOTA.    Spending faster than the clock. The model is
                                DE-PRIORITIZED: still usable, but it loses to
                                any equally-suitable model that is not.
    else     -> AVAILABLE.      At or ahead of pace.

Each opted-in entry names ITS OWN pool; pools are DECLARED per entry rather
than derived from the model id. Reading a pool, computing the verdict, the
session pin and the verdict cache are not here: raw readers belong to
``llm_kit.adapters`` and the pacing policy to ``llm_kit.dispatch``.

FAIL OPEN: :data:`STATUS_NO_DATA` is never a reason to disable or
de-prioritize a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

STATUS_AVAILABLE = "available"
"""The pool was read and the burn-down is at or ahead of the clock."""

STATUS_UNDER_QUOTA = "under-quota"
"""The pool was read and quota is being spent faster than time passes. The
model stays USABLE and is merely de-prioritized -- see the module docstring on
why this is ordering rather than exclusion."""

STATUS_OUT_OF_QUOTA = "out-of-quota"
"""The pool was read and nothing is left in it. The model is DISABLED: unlike
:data:`STATUS_UNDER_QUOTA` this is not a preference, it is the absence of the
capability, and a call against it would fail."""

STATUS_NO_DATA = "no-data"
"""No usable pool reading. NEVER a reason to disable or de-prioritize the model -- see the
module docstring on failing open. Distinct from :data:`STATUS_AVAILABLE`
because "nothing gated this" and "the pacing check passed" are different
facts, and a caller reporting capacity must not state the second when it
observed the first."""

#: The all-model weekly window. The default pool for an entry that opts in
#: with a bare ``conserve_usage: true``, on either harness.
POOL_SEVEN_DAY = "seven_day"

#: The per-model weekly bucket carried in the claude payload's ``model_scoped``
#: array. Selected by ``display_name`` (the raw reader lives in ``llm_kit.adapters``).
POOL_MODEL_SCOPED = "model_scoped"

#: Codex's own name for its principal window.
POOL_PRIMARY = "primary"


@dataclass(frozen=True)
class Budget:
    """One pacing verdict. Constructors never raise.

    ``status`` is one of :data:`STATUS_AVAILABLE`, :data:`STATUS_UNDER_QUOTA`,
    :data:`STATUS_OUT_OF_QUOTA`, or :data:`STATUS_NO_DATA`. ``remaining`` and ``window_remaining`` are the
    two fractions the rule compares, both None when there was no reading.
    ``pool`` names the window consulted and ``detail`` says, in one line, why
    the verdict came out as it did.
    """

    status: str
    pool: str
    detail: str
    remaining: Optional[float] = None
    window_remaining: Optional[float] = None
    resets_at: Optional[int] = None

    @property
    def usable(self) -> bool:
        """False ONLY when the pool is spent. Fails open on every other state.

        The predicate a caller gates selection on. Deliberately not the
        negation of :attr:`deprioritized`: an under-quota model is usable and
        a caller that conflated the two would drop a capability it still has.
        """
        return self.status != STATUS_OUT_OF_QUOTA

    @property
    def deprioritized(self) -> bool:
        """True when the model is usable but should lose to an equal peer."""
        return self.status == STATUS_UNDER_QUOTA

    def to_json(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "pool": self.pool,
            "detail": self.detail,
            "remaining": self.remaining,
            "window_remaining": self.window_remaining,
            "resets_at": self.resets_at,
        }


class ConserveConfigError(ValueError):
    """A ``conserve_usage`` declaration that cannot be read as one."""


@dataclass(frozen=True)
class ConserveSpec:
    """A parsed ``conserve_usage`` declaration.

    ``display_name`` applies only to :data:`POOL_MODEL_SCOPED`, where the pool
    is an array and an entry has to be picked out of it by label.
    """

    pool: str
    display_name: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"pool": self.pool}
        if self.display_name is not None:
            result["display_name"] = self.display_name
        return result


def parse_conserve_usage(
    value: object, *, source: str, entry_id: str
) -> Optional[ConserveSpec]:
    """Read a ``conserve_usage`` value, or None when the entry does not opt in.

    Accepted forms, and nothing else::

        conserve_usage: false                       # or omitted -- not opted in
        conserve_usage: true                        # the all-model weekly pool
        conserve_usage: {pool: seven_day}
        conserve_usage: {pool: model_scoped, display_name: Fable}

    Raises:
        ConserveConfigError: any other shape. A misspelled pool is a silent
            no-op if tolerated -- the entry would read as opted-in-but-never-
            paced -- so it is refused at parse time instead.
    """
    if value is None or value is False:
        return None
    if value is True:
        return ConserveSpec(pool=POOL_SEVEN_DAY)
    if not isinstance(value, Mapping):
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has an invalid 'conserve_usage' "
            f"({value!r}); expected a boolean or a mapping with 'pool'"
        )
    unknown = set(value) - {"pool", "display_name"}
    if unknown:
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has unknown 'conserve_usage' keys "
            f"({', '.join(sorted(unknown))}); expected 'pool' and 'display_name'"
        )
    pool = value.get("pool")
    if not isinstance(pool, str) or not pool.strip():
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has a 'conserve_usage' with no "
            "'pool' (a non-empty string is required)"
        )
    display_name = value.get("display_name")
    if display_name is not None and (
        not isinstance(display_name, str) or not display_name.strip()
    ):
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' has a non-string "
            f"'conserve_usage.display_name' ({display_name!r})"
        )
    spec = ConserveSpec(
        pool=pool.strip(),
        display_name=display_name.strip() if isinstance(display_name, str) else None,
    )
    if spec.pool == POOL_MODEL_SCOPED and spec.display_name is None:
        # The pool is an ARRAY; without a label there is no bucket to select, so
        # this opt-in could only ever return no-data. Refused for the same
        # reason as a misspelled pool: an entry that is opted in and can never
        # conserve is indistinguishable from a working one.
        raise ConserveConfigError(
            f"{source}: entry '{entry_id}' declares pool '{POOL_MODEL_SCOPED}' "
            "with no 'display_name'; that pool is an array and needs a label "
            "to select a bucket from it"
        )
    return spec


