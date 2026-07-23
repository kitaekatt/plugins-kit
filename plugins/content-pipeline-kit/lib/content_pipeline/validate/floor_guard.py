"""Advisory-only diagnostics with a known-good <10% acceptance gate.

A floor guard is an opt-in diagnostic: it flags candidates that look
suspicious against a known-good fixture set (a metric drifting outside a
<10% acceptance band from the fixture baseline), but it never blocks
acceptance by itself -- it is guidance, not a gate the library forces on
every pipeline. A consumer registers a floor guard only when it wants the
signal; a minimal pipeline runs with none registered.
"""


def check_floor(candidate, known_good_fixture) -> bool:
    """Return True if candidate is within the known-good acceptance band."""
    raise NotImplementedError
