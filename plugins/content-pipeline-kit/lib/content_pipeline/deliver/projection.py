"""Append-only projection delivery: rollback via .bak, never overwrite.

Writes generated content to a standalone projection artifact alongside (not
inside) the authored source -- the append-only counterpart to ``inplace``'s
in-place mutation. A write never overwrites the previous artifact directly; it
first moves the existing file to a ``.bak`` sibling, so rollback is a rename,
never a content reconstruction. After writing, the artifact is reloaded and
validated; a reload failure restores the ``.bak`` so a bad write never leaves a
corrupt artifact in place. Human-authored data is never overwritten -- the
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

    Steps:

    1. **Back up** -- if the artifact already exists, move it to
       ``<path><backup_suffix>`` (replacing any stale backup) so the previous
       version is recoverable by a rename.
    2. **Write** -- ``serialize(path, content)`` produces the new artifact.
    3. **Reload-validate** -- when ``load`` is given, reload the artifact (and,
       when ``validate`` is given, assert ``validate(reloaded)``). On any
       failure the ``.bak`` is restored over the artifact and the exception is
       re-raised as-is, so a corrupt write never survives.

    Never overwrites the ``.bak`` content-blind: a first write (no prior
    artifact) creates no backup. Returns a :class:`ProjectionResult`.
    """
    path = Path(artifact_path)
    backup: Optional[Path] = None
    if path.exists():
        backup = path.with_name(path.name + backup_suffix)
        os.replace(path, backup)

    result = ProjectionResult(path=path, backup=backup)
    try:
        serialize(path, content)
        if load is not None:
            reloaded = load(path)
            if validate is not None and not validate(reloaded):
                raise ValueError(
                    f"projection reload validation failed for {path.name}"
                )
    except Exception:
        # Restore the previous artifact from the backup, if we made one.
        if backup is not None and backup.exists():
            os.replace(backup, path)
            result.rolled_back = True
        raise
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
