"""Append-only projection delivery: rollback via .bak, never overwrite.

Writes generated content to a standalone projection artifact alongside (not
inside) the authored source -- the append-only counterpart to ``inplace``'s
in-place mutation. A write never overwrites the previous artifact directly; it
writes and validates a temporary sibling, copies the existing file to a ``.bak``
sibling, then swaps the temporary in with ``os.replace``, so rollback is a
rename, never a content reconstruction. A serialize or reload failure removes
the temporary file and leaves the artifact and ``.bak`` untouched, so an
interrupted write never leaves a partial artifact in place. Human-authored data is never overwritten -- the
projection is a separate artifact the pipeline owns wholesale.

Generalizes the localization append-only projection writers. The serialization
format is entirely the caller's: :func:`apply_projection` takes ``serialize`` /
``load`` callables and never binds a format.

XLIFF aggregation SHAPE (:func:`aggregate_projections`) is lifted too but not
its format: many ``(unit, artifact)`` pairs fold into one artifact -> list-of-
unit-contents mapping, so a caller emits one file per artifact from many source
units. How that list becomes the on-disk bytes stays project-side.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


@dataclass
class ProjectionResult:
    """Outcome of an :func:`apply_projection` write.

    - ``path`` -- the artifact written.
    - ``backup`` -- the ``.bak`` sibling created (``None`` on a first write
      when no prior artifact existed).
    - ``written`` -- True when the artifact was (re)written.
    - ``rolled_back`` -- True when reload validation failed and the ``.bak``
      was restored (``written`` is then False).

    On a failed write, :func:`apply_projection` re-raises the underlying
    exception before returning, so the ``ProjectionResult`` for that failed
    attempt is not the function's return value -- it is attached to the
    raised exception as ``exc.result``, which is how a caller observes
    ``rolled_back`` (or a no-backup removal, see :func:`apply_projection`).
    """

    path: Path
    backup: Optional[Path] = None
    written: bool = False
    rolled_back: bool = False


def apply_projection(
    artifact_path,
    content: Any,
    *,
    serialize: Callable[[Path, Any], None],
    load: Optional[Callable[[Path], Any]] = None,
    validate: Optional[Callable[[Any], bool]] = None,
    backup_suffix: str = ".bak",
) -> ProjectionResult:
    """Write ``content`` to ``artifact_path``, preserving the prior version.

    Interruption-safe: the target is only ever replaced by ``os.replace`` of a
    fully written and validated file, so a crash leaves the old artifact or the
    new one, never a partial one. Re-running with the same input converges.

    Steps:

    1. **Write** -- ``serialize(tmp, content)`` produces the new artifact in a
       temporary file in the SAME directory as the target.
    2. **Reload-validate** -- when ``load`` is given, reload the temporary file
       (and, when ``validate`` is given, assert ``validate(reloaded)``). On any
       failure the temporary file is removed and the target and any existing
       ``.bak`` are left untouched (``result.rolled_back`` is True when a prior
       artifact exists, since it is still in place). The exception is
       re-raised as-is with this attempt's :class:`ProjectionResult` attached
       as ``exc.result`` -- the function never returns on this path.
    3. **Back up** -- if the artifact already exists, COPY it to
       ``<path><backup_suffix>`` (replacing any stale backup); the original
       stays in place until the swap.
    4. **Swap** -- ``os.replace(tmp, path)``.

    A first write (no prior artifact) creates no backup. Returns a
    :class:`ProjectionResult`.
    """
    path = Path(artifact_path)
    backup: Optional[Path] = None
    if path.exists():
        backup = path.with_name(path.name + backup_suffix)
    result = ProjectionResult(path=path, backup=backup)

    tmp = path.with_name(f"{path.stem}.tmp{path.suffix}")
    try:
        serialize(tmp, content)
        if load is not None:
            reloaded = load(tmp)
            if validate is not None and not validate(reloaded):
                raise ValueError(
                    f"projection reload validation failed for {path.name}"
                )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        result.rolled_back = backup is not None
        exc.result = result  # noqa: B010 -- the only way to expose this result
        raise
    if backup is not None:
        shutil.copy2(path, backup)
    os.replace(tmp, path)
    result.written = True
    return result


def rollback_projection(artifact_path, *, backup_suffix: str = ".bak") -> bool:
    """Restore the ``.bak`` sibling over ``artifact_path``.

    Returns True when a backup existed and was restored; False when there was
    no backup to roll back to. Rollback is a rename, never a reconstruction.
    """
    path = Path(artifact_path)
    backup = path.with_name(path.name + backup_suffix)
    if not backup.exists():
        return False
    os.replace(backup, path)
    return True


def aggregate_projections(
    pairs: Iterable[Tuple[str, Any]],
) -> Dict[str, List[Any]]:
    """Fold ``(artifact, unit_content)`` pairs into ``{artifact: [content...]}``.

    The XLIFF-aggregation shape: many source units contribute to one artifact,
    so a caller collects every unit's content per artifact key and emits one
    file per artifact from the aggregated list. Order within each list follows
    input order (callers sort upstream if they need order-independence). The
    per-artifact serialization stays project-side.
    """
    out: Dict[str, List[Any]] = {}
    for artifact, content in pairs:
        out.setdefault(artifact, []).append(content)
    return out


__all__ = [
    "ProjectionResult",
    "apply_projection",
    "rollback_projection",
    "aggregate_projections",
]
