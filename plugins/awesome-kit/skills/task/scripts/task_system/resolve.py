"""Reference resolution and location logic (spec sections 2.3, 5, 7.4).

A ``<ref>`` is either a path (``tmp/<stub>`` or ``dev/tasks/<stub>``) or a bare
stub (no path separator). Stubs are searched in both known roots; an ambiguous
stub (a folder in both roots) is an error listing the candidates (spec 5).

Path canonicalization (spec 2.3): paths are lexically normalized -- ``.`` and
``..`` segments resolved, absolute paths re-expressed project-relative -- into
the normalized project-relative form used for equality/dedupe comparisons
(``tmp/<stub>`` / ``dev/tasks/<stub>``).

Minimal readings chosen in Step 1 (flagged in the implementation report):
- The known task locations are exactly ``tmp/<stub>`` and ``dev/tasks/<stub>``
  (spec 7.4); any other shape (different root, wrong depth, escape above the
  project root) is "outside the known roots" and raises RefResolutionError.
- A bare stub matching NO folder in either root cannot be classified by the
  tri-state (the intended root is unknown), so it is also a resolution error.

Host detection is the short hostname (``hostname -s`` equivalent), via stdlib
``socket``; callers inject a value for tests.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path

LOCATION_TMP = "tmp"
LOCATION_DEV_TASKS = "dev/tasks"
KNOWN_ROOTS = (LOCATION_TMP, LOCATION_DEV_TASKS)

# Where a tmp task's folder is parked by ``archive`` (spec 2.5): the folder
# moves out of the live root so tmp/ stays a working set, and the user can
# purge tmp/archived-tasks/ wholesale. The name is reserved -- it can never
# itself be a task stub under tmp/.
ARCHIVED_TMP_DIRNAME = "archived-tasks"


def archived_tmp_folder(project_root: Path, stub: str) -> Path:
    """The parking spot for an archived tmp task's folder."""
    return project_root / LOCATION_TMP / ARCHIVED_TMP_DIRNAME / stub


class RefResolutionError(ValueError):
    """A <ref> could not be resolved to a canonical task path."""


@dataclass(frozen=True)
class ResolvedRef:
    canonical: str  # normalized project-relative path, e.g. "tmp/spike-x"
    location: str  # LOCATION_TMP or LOCATION_DEV_TASKS
    stub: str

    def folder(self, project_root: Path) -> Path:
        return project_root / self.canonical


def short_hostname() -> str:
    """Short host name (``hostname -s`` equivalent)."""
    return socket.gethostname().split(".")[0]


def _normalize_parts(path_str: str, project_root: Path) -> tuple[str, ...]:
    """Lexically normalize a path string to project-relative parts."""
    p = Path(path_str)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(project_root.resolve())
        except ValueError as exc:
            raise RefResolutionError(
                f"path is outside the project root: {path_str!r}"
            ) from exc
        return rel.parts
    parts: list[str] = []
    for seg in path_str.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not parts:
                raise RefResolutionError(
                    f"path escapes the project root: {path_str!r}"
                )
            parts.pop()
        else:
            parts.append(seg)
    return tuple(parts)


def _classify_parts(parts: tuple[str, ...], original: str) -> ResolvedRef:
    """Classify normalized parts as a tmp or dev/tasks task path."""
    if len(parts) == 2 and parts[0] == "tmp":
        if parts[1] == ARCHIVED_TMP_DIRNAME:
            raise RefResolutionError(
                f"tmp/{ARCHIVED_TMP_DIRNAME} is the reserved parking "
                f"directory for archived tmp tasks, not a task: {original!r}"
            )
        return ResolvedRef("/".join(parts), LOCATION_TMP, parts[1])
    if len(parts) == 3 and parts[0] == "dev" and parts[1] == "tasks":
        return ResolvedRef("/".join(parts), LOCATION_DEV_TASKS, parts[2])
    raise RefResolutionError(
        "not a known task location (expected tmp/<stub> or dev/tasks/<stub>): "
        f"{original!r}"
    )


def resolve_path(path_str: str, project_root: Path) -> ResolvedRef:
    """Resolve a path-shaped ref (no stub search) to its canonical form."""
    return _classify_parts(_normalize_parts(path_str, project_root), path_str)


def resolve_ref(ref: str, project_root: Path) -> ResolvedRef:
    """Resolve a <ref> -- a path or a bare stub -- to its canonical form.

    A ref containing a path separator (or an absolute path) is treated as a
    path. A bare stub is searched in both known roots; ambiguous (folder in
    both) errors listing the candidates; no match errors (the intended root
    is unknowable).
    """
    if "/" in ref or Path(ref).is_absolute():
        return resolve_path(ref, project_root)

    candidates = [
        f"{root}/{ref}"
        for root in KNOWN_ROOTS
        if (project_root / root / ref).is_dir()
    ]
    if len(candidates) > 1:
        raise RefResolutionError(
            f"ambiguous stub {ref!r}: candidates: " + ", ".join(candidates)
        )
    if not candidates:
        raise RefResolutionError(
            f"stub {ref!r} matches no task folder under tmp/ or dev/tasks/"
        )
    return resolve_path(candidates[0], project_root)
