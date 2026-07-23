"""The ensure-chain: always regenerate cheaply, write only on real change.

The cheap derived artifacts in a content pipeline depend on more than their
own file inputs -- they depend on provider code, indexer code, and backing
configs that a stored-hash-over-bytes freshness check cannot see. The fix
both source systems converged on: *always* regenerate the artifact in memory
(it is cheap -- no machine call), then compare the freshly-computed content
hash against the on-disk artifact's recorded hash. Identical hash => no
write, no version-control churn, no downstream cascade. Different hash =>
write, and any downstream stored-hash chain fires naturally.

:func:`ensure` embodies that discipline generically. Everything that touches
the outside world is a callable the caller supplies -- regeneration, content
hashing, loading the existing artifact, and the write itself -- so this
module stays pure and I/O-agnostic. In particular:

- **Writing is caller-supplied.** ``freshness`` does not own the artifact's
  on-disk format. :func:`atomic_write` is offered as a dependency-free
  default (stdlib ``tempfile`` + ``os.replace``), nothing more.
- **The pre-write hook is a plain callable.** It is where a version-control
  seam (open-for-edit) belongs, but ``freshness`` must not depend on
  ``content_pipeline.vcs`` (CRP: no consumer of freshness is forced to drag
  in VCS). The caller passes a callback; this module only promises to invoke
  it *before a real write* and *never on the no-op path*.

Cascades (artifact B's freshness depends on artifact A being current first)
are expressed by ``prerequisites``: zero or more ensures run before
regeneration, so upstream drift is materialized before this artifact is
compared.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Generic, Optional, Sequence, TypeVar

T = TypeVar("T")


def atomic_write(path, content: str, *, encoding: str = "utf-8") -> None:
    """Write ``content`` to ``path`` atomically (mkstemp + ``os.replace``).

    The temp file is created in the destination's own directory so
    ``os.replace`` is a same-filesystem rename (atomic; a fixed system temp
    dir could be a different filesystem). A random ``mkstemp`` name means
    concurrent writers never collide. On any failure the temp file is
    removed and the exception re-raised, so the destination is either fully
    updated or untouched. Dependency-free by design -- this is only a
    convenience default for ``ArtifactSpec.write``.
    """
    directory = os.path.dirname(os.path.abspath(str(path)))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".atomic.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class EnsureResult(Generic[T]):
    """Outcome of an :func:`ensure` call.

    ``representation`` is the freshly regenerated in-memory artifact (always
    produced, even on the no-op path). ``written`` is True only when the
    content differed and the artifact was persisted.
    """

    representation: T
    written: bool


@dataclass(frozen=True)
class ArtifactSpec(Generic[T]):
    """Everything :func:`ensure` needs, as caller-supplied callables.

    - ``regenerate`` -- produce the cheap in-memory representation from
      current inputs. Called on every ensure.
    - ``content_hash`` -- a stable content hash of a representation. The
      no-op decision is ``existing_hash and existing_hash == new_hash`` --
      an empty/absent existing hash (legacy artifact) forces one rewrite.
    - ``load_existing`` -- return the on-disk representation, or ``None``
      when it is missing or unreadable (a corrupt artifact forces a rewrite).
    - ``write`` -- persist a representation. Only called on a real change.
      :func:`atomic_write` is a fine default wrapped to this signature.
    - ``pre_write`` -- optional hook invoked immediately before ``write``
      (the version-control seam: open-for-edit). Never called on the no-op
      path. Keep it best-effort; ``ensure`` does not catch its exceptions.
    - ``prerequisites`` -- ensures to run before ``regenerate`` so upstream
      drift is materialized first (the cascade).
    """

    regenerate: Callable[[], T]
    content_hash: Callable[[T], str]
    load_existing: Callable[[], Optional[T]]
    write: Callable[[T], None]
    pre_write: Optional[Callable[[], None]] = None
    prerequisites: Sequence[Callable[[], "EnsureResult"]] = field(default_factory=tuple)


def ensure(spec: ArtifactSpec[T]) -> EnsureResult[T]:
    """Regenerate in memory, compare content hashes, write only on change.

    1. Run every prerequisite ensure (the cascade), so any upstream drift is
       on disk before this artifact is regenerated.
    2. Regenerate the representation in memory.
    3. Load the existing artifact; if present and its content hash is
       non-empty and equal to the new one, return without writing.
    4. Otherwise call ``pre_write`` (if given), then ``write``.

    Returns an :class:`EnsureResult` carrying the fresh representation and
    whether a write happened.
    """
    for prerequisite in spec.prerequisites:
        prerequisite()

    new_repr = spec.regenerate()

    existing = spec.load_existing()
    if existing is not None:
        existing_hash = spec.content_hash(existing)
        if existing_hash and existing_hash == spec.content_hash(new_repr):
            return EnsureResult(new_repr, written=False)

    if spec.pre_write is not None:
        spec.pre_write()
    spec.write(new_repr)
    return EnsureResult(new_repr, written=True)
