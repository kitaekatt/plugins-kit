"""Content hashing for the two-tier freshness engine.

Pure, deterministic content hashing over already-prepared values. Two
source systems duplicated this module almost verbatim; the shared shape is:

- ``stable_json`` -- sorted-key, ASCII-only JSON serialization, so dict
  ordering never leaks into a digest and non-ASCII escapes to ``\\uXXXX``.
- ``digest`` -- a SHA-256 over several byte blobs with a ``b"\\x00"``
  separator after each, so ``(b"ab", b"c")`` and ``(b"a", b"bc")`` differ.
- A configurable digest length: the per-item convention is a 16-hex-char
  (64-bit) truncation (short enough to read in an artifact, collision
  probability ~1e-15 at corpus scale); full 64-char digests are available
  for corpus-wide cross-references.

Hashing takes *already-prepared* values. Any domain-specific preparation --
stripping a documentation block out of a prompt before it is hashed, picking
the effective value out of an attributed field -- happens in the caller
(``config/loader`` owns doc-block stripping); this module never reaches back
into config or the store.

The two-tier split (a shared per-unit snapshot combined with per-item
inputs) is expressed by :func:`shared_snapshot` (canonicalize the shared
parts once, reuse across every item in the unit) plus :func:`combined_hash`
(fold one item's inputs into that snapshot). :func:`corpus_hash` produces the
cross-reference digest a derived artifact records so a change to any unit's
source hash is detectable without walking every item.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping as MappingABC
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Iterable, Tuple

# The per-item digest convention shared by both source systems: 16 hex
# chars == 64 bits. Short enough to read inline in a stored artifact.
DEFAULT_DIGEST_LENGTH = 16
# Full SHA-256 hex length, for corpus-wide cross-references where the
# collision budget is spent across every unit at once.
FULL_DIGEST_LENGTH = 64


def _canonicalize(obj, path: str = "$"):
    """Recursively convert ``obj`` into a JSON-native, order-independent shape.

    Handles the value families that appear in prepared pipeline payloads:
    ``set``/``frozenset`` become a list of canonicalized members sorted by
    their own canonical JSON text (so a set's iteration order, which is
    ``PYTHONHASHSEED``-dependent, never reaches the digest); ``tuple``
    becomes a list; ``pathlib.Path`` becomes its string form;
    ``datetime``/``date`` become an ISO-8601 string; ``Decimal`` becomes its
    string form (exact, unlike a float round-trip); ``Enum`` becomes its
    (canonicalized) value. A ``Mapping`` recurses per key; every other
    mapping/sequence/scalar type recurses structurally. Any value this
    function cannot place is a caller error: raise :class:`TypeError` naming
    the key path so the offending field is obvious without a debugger.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, MappingABC):
        return {str(k): _canonicalize(v, f"{path}.{k}") for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(v, f"{path}[{i}]") for i, v in enumerate(obj)]
    if isinstance(obj, (set, frozenset)):
        members = [_canonicalize(v, f"{path}{{*}}") for v in obj]
        members.sort(key=lambda v: json.dumps(v, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
        return members
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return _canonicalize(obj.value, path)
    raise TypeError(
        f"stable_json: cannot canonicalize {type(obj).__name__!r} at {path}"
    )


def stable_json(obj) -> bytes:
    """Serialize ``obj`` to a stable, deterministic ASCII byte string.

    Every value is canonicalized explicitly first (see :func:`_canonicalize`)
    -- a set/frozenset sorts to a list by its own canonical text, a tuple
    becomes a list, and ``Path``/``datetime``/``date``/``Decimal``/``Enum``
    each get a deterministic scalar form -- so the result never depends on
    ``PYTHONHASHSEED`` or on an object's ``id()``. ``sort_keys`` keeps dict
    ordering out of the digest; ``ensure_ascii`` escapes any non-ASCII to
    ``\\uXXXX`` so the output is pure ASCII; ``separators`` drops
    insignificant whitespace so the encoding is byte-stable across Python
    versions. A value :func:`_canonicalize` cannot place raises
    :class:`TypeError` naming the key path -- there is no ``default=str``
    fallback, because silently stringifying an arbitrary object (which may
    embed its ``id()``, e.g. ``<Foo object at 0x...>``) is exactly the
    non-determinism this function exists to prevent.
    """
    canonical = _canonicalize(obj)
    return json.dumps(
        canonical, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")


def canonical_bytes(value) -> bytes:
    """Canonicalize one value to bytes for hashing.

    ``bytes`` pass through unchanged -- they are assumed already canonical
    (e.g. a prompt payload the caller has finished assembling). Every other
    value is routed through :func:`stable_json`, so strings, dicts, lists,
    and numbers all get the sorted-key ASCII treatment. A bare string and a
    dict wrapping it therefore hash differently (``"foo"`` -> ``b'"foo"'``
    vs ``{"v": "foo"}`` -> ``b'{"v":"foo"}'``).
    """
    if isinstance(value, bytes):
        return value
    return stable_json(value)


def _truncate(hexdigest: str, length: int) -> str:
    """Clamp a hex digest to ``length`` chars (``FULL_DIGEST_LENGTH`` = all)."""
    if length >= FULL_DIGEST_LENGTH:
        return hexdigest
    if length <= 0:
        raise ValueError("digest length must be positive")
    return hexdigest[:length]


def digest(*parts: bytes, length: int = DEFAULT_DIGEST_LENGTH) -> str:
    """SHA-256 hex digest over prepared byte blobs, separator-delimited.

    A ``b"\\x00"`` separator is written after each part so concatenation
    ambiguity cannot collapse two distinct input tuples to one digest.
    Truncated to ``length`` hex chars (default 16); pass
    ``length=FULL_DIGEST_LENGTH`` for the untruncated 64-char digest.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(part)
        h.update(b"\x00")
    return _truncate(h.hexdigest(), length)


def content_hash(*values, length: int = DEFAULT_DIGEST_LENGTH) -> str:
    """Canonicalize each value, then hash the lot as one digest.

    The general-purpose entry point: ``content_hash(a, b, c)`` canonicalizes
    every argument via :func:`canonical_bytes` and folds them into a single
    separator-safe digest. Already-canonical ``bytes`` (e.g. a
    :func:`shared_snapshot` element) pass straight through, so a shared
    snapshot can be splatted directly: ``content_hash(item, *shared)``.
    """
    return digest(*(canonical_bytes(v) for v in values), length=length)


def shared_snapshot(*values) -> Tuple[bytes, ...]:
    """Canonicalize the shared, per-unit inputs once for reuse across items.

    Every item in a unit shares the same snapshot (e.g. unit-level source
    content, an assembled prompt). Canonicalizing it once and reusing the
    resulting byte tuple avoids re-serializing it per item. The tuple is fed
    to :func:`combined_hash` (or splatted into :func:`content_hash`).
    """
    return tuple(canonical_bytes(v) for v in values)


def combined_hash(
    item_value,
    shared: Tuple[bytes, ...],
    *,
    length: int = DEFAULT_DIGEST_LENGTH,
) -> str:
    """Hash one item's inputs folded into a precomputed shared snapshot.

    The per-item half of the shared-snapshot split: ``item_value`` is
    canonicalized and prepended to the already-canonical ``shared`` parts,
    so drift on either the item or the shared inputs invalidates the digest.
    """
    return digest(canonical_bytes(item_value), *shared, length=length)


def corpus_hash(
    pairs: Iterable[Tuple[str, str]],
    *,
    length: int = FULL_DIGEST_LENGTH,
) -> str:
    """Cross-reference digest over every unit/item's ``(id, source_hash)``.

    A derived artifact records this digest as the source state it was built
    from. On the next run, a recomputed digest that differs means at least
    one unit's source hash drifted and per-item hashes must be walked;
    an unchanged digest short-circuits the whole walk. Order is preserved
    from the iterable (callers sort upstream if they need order-independence).
    """
    payload = [{"id": pid, "source_hash": shash} for pid, shash in pairs]
    return content_hash(payload, length=length)
