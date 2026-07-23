"""Deterministic seeding for stochastic gating decisions.

Some pipeline decisions are intentionally stochastic -- e.g. a coin flip for
"should this item be generated this pass?" that samples a bounded batch. The
hazard: if the RNG is seeded from run-local state (the OS entropy pool), the
same item flips differently between runs. Combined with per-item freshness
hashing, that means a run can drop an item's machine value that the previous
run produced, leaving the artifact *forever stale* -- every run re-rolls,
re-drops, and re-stales it.

The invariant that removes the hazard: **seed the RNG deterministically from
the item's stable identity, never from run-local state.** Same identifier ->
same seed -> same roll, every run, so a flag flip elsewhere in the pipeline
never perpetually invalidates the hash the sampling drives. An optional salt
lets one identifier drive several independent stochastic decisions without
correlating them.

Pure and stdlib-only: the seed is a truncation of ``sha256(identifier)`` (or
``sha256(identifier + salt)``), so it is reproducible across processes,
platforms, and Python versions.
"""

from __future__ import annotations

import hashlib
import random


def deterministic_seed(identifier: str, salt: str = "", *, bits: int = 64) -> int:
    """Derive a stable, repeatable non-negative seed from an identifier.

    The seed is the first ``bits`` bits of ``sha256`` over the UTF-8 bytes
    of the identifier (with the salt mixed in via a ``\\x00`` separator when
    non-empty). With the default ``salt=""`` and ``bits=64`` this is exactly
    ``int(sha256(identifier).hexdigest()[:16], 16)`` -- the formula both
    source systems already use, so a port reproduces their rolls byte-for-
    byte. ``bits`` must be a positive multiple of 4 (whole hex chars).
    """
    if bits <= 0 or bits % 4 != 0:
        raise ValueError("bits must be a positive multiple of 4")
    payload = identifier if salt == "" else f"{identifier}\x00{salt}"
    hexdigest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(hexdigest[: bits // 4], 16)


def seeded_random(identifier: str, salt: str = "") -> random.Random:
    """Return a ``random.Random`` seeded deterministically from an identity.

    Convenience wrapper over :func:`deterministic_seed`: the returned RNG
    produces the same sequence for the same ``(identifier, salt)`` on every
    run, which is what keeps stochastic gating from churning freshness
    hashes.
    """
    return random.Random(deterministic_seed(identifier, salt))
