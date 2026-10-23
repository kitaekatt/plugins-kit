"""Single-intermediary hashing anchor.

The core insight this module encodes: instead of hashing every raw input a
generation step could depend on, synthesize ONE per-entity intermediary
slice from those inputs, and hash only that slice. Downstream freshness
checks then depend on a single, narrow, purpose-built hash rather than a
sprawling set of raw-input hashes -- so an input change that does not affect
the synthesized slice does not trigger a spurious regeneration, and an input
change that does affect it is caught without downstream code needing to know
which raw inputs matter. A two-stage cheap-hash / full-rebuild split lets the
common case (nothing changed) skip the expensive synthesis path entirely.

:func:`ensure_intermediary` embodies that discipline generically. Everything
that touches raw sources or storage is a caller-supplied callable
(:class:`IntermediarySpec`), so this module stays pure and I/O-agnostic:

- **Cheap path (the dominant case).** Compute the per-entity inputs hash from
  raw-source slices (cheap: a few reads + a digest), load the stored
  intermediary, and compare its recorded hash. On a match, return without
  ever building the expensive synthesized content.
- **Full path (drift or missing).** Rebuild the synthesized content from raw
  sources, write it with the current hash stamped, and report the rebuild.
  The hash is re-stamped even when the synthesized content turns out
  identical, so the cheap path reclaims the entity on the next run rather
  than taking the full path forever.

Downstream consumers hash ONLY the returned intermediary slice, never the raw
inputs -- that is the whole point of the anchor. This module does not perform
that downstream hashing (it stays hashing-library-agnostic); a consumer runs
``freshness.hashing.content_hash`` over the intermediary it gets back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class IntermediarySpec(Generic[T]):
    """Everything :func:`ensure_intermediary` needs, as caller callables.

    - ``inputs_hash`` -- compute the per-entity inputs hash from raw-source
      slices. Cheap by construction (the two-stage split relies on it being
      far cheaper than ``rebuild``). This is where the "hash a narrow
      synthesized-from-this-entity-only slice" discipline lives: a change to
      an unrelated entity's sources must produce zero change here.
    - ``load_existing`` -- return the stored intermediary, or ``None`` when it
      is missing or unreadable (which forces the full path).
    - ``stored_hash`` -- extract the recorded inputs hash from a stored
      intermediary. An empty/absent stored hash forces the full path (a
      legacy intermediary written before the hash existed).
    - ``rebuild`` -- synthesize the full intermediary from current raw
      sources. Return ``None`` when the entity has no sources to build from
      (e.g. no animation set / no data yet); the ensure then no-ops.
    - ``write`` -- persist ``(intermediary, hash)``. Only called on the full
      path. The current hash is passed so the writer stamps it for the cheap
      path to read next run.
    - ``content_equal`` -- optional: compare a rebuilt intermediary against
      the existing one to report whether the *content* actually changed (vs.
      only the hash needing a re-stamp). Does NOT gate the write -- the full
      path always writes so the hash is re-stamped -- it only enriches the
      result's ``content_changed`` flag. ``None`` leaves that flag ``None``.
    """

    inputs_hash: Callable[[], str]
    load_existing: Callable[[], Optional[T]]
    stored_hash: Callable[[T], str]
    rebuild: Callable[[], Optional[T]]
    write: Callable[[T, str], None]
    content_equal: Optional[Callable[[T, T], bool]] = None


@dataclass(frozen=True)
class IntermediaryResult(Generic[T]):
    """Outcome of an :func:`ensure_intermediary` call.

    - ``intermediary`` -- the current intermediary slice (the loaded one on
      the cheap path, the rebuilt one on the full path, or ``None`` when the
      entity had no sources to build from).
    - ``changed`` -- True when the intermediary was written this call
      (rebuilt or re-stamped). Mirrors the source's "was written this call"
      notion of changed.
    - ``rebuilt`` -- True when the full (synthesis) path ran; False when the
      cheap path short-circuited.
    - ``content_changed`` -- whether the synthesized *content* differed from
      the stored content (only meaningful with ``content_equal`` supplied and
      an existing intermediary present; ``None`` otherwise).
    """

    intermediary: Optional[T]
    changed: bool
    rebuilt: bool
    content_changed: Optional[bool] = None


def ensure_intermediary(spec: IntermediarySpec[T]) -> IntermediaryResult[T]:
    """Two-stage cheap-hash / full-rebuild for one entity's intermediary.

    1. Compute the current inputs hash and load the stored intermediary.
    2. **Cheap path:** if the stored intermediary exists and its recorded
       hash is non-empty and equal to the current hash, return it without
       rebuilding (``changed=False``, ``rebuilt=False``).
    3. **Full path:** rebuild from raw sources. If ``rebuild`` returns
       ``None`` (no sources), no-op (``changed=False``, ``rebuilt=True``).
       Otherwise write the rebuilt intermediary with the current hash stamped
       and return it (``changed=True``, ``rebuilt=True``). ``content_changed``
       reports whether the content actually differed when ``content_equal``
       is supplied.
    """
    current_hash = spec.inputs_hash()
    existing = spec.load_existing()

    if existing is not None:
        recorded = spec.stored_hash(existing) or ""
        if recorded and recorded == current_hash:
            return IntermediaryResult(
                intermediary=existing, changed=False, rebuilt=False,
                content_changed=False,
            )

    rebuilt = spec.rebuild()
    if rebuilt is None:
        return IntermediaryResult(
            intermediary=existing, changed=False, rebuilt=True,
            content_changed=None,
        )

    content_changed: Optional[bool] = None
    if existing is not None and spec.content_equal is not None:
        content_changed = not spec.content_equal(existing, rebuilt)

    spec.write(rebuilt, current_hash)
    return IntermediaryResult(
        intermediary=rebuilt, changed=True, rebuilt=True,
        content_changed=content_changed,
    )
