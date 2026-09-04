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

# Where ``archive`` parks a folder version control will not carry (spec 2.5):
# the folder moves out of the live root so the root stays a working set, and
# the user can purge ``<location>/archived-tasks/`` wholesale. The name is
# reserved under BOTH roots -- it can never itself be a task stub.
#
# Two locations park, for the same reason expressed differently: a tmp task
# is local scratch by construction, and a dev/tasks task whose folder git is
# configured to IGNORE is local scratch in fact -- no commit can carry it, so
# there is no version-control record to archive into.
ARCHIVED_DIRNAME = "archived-tasks"


def archived_folder(project_root: Path, location: str, stub: str) -> Path:
    """The parking spot for an archived task's folder in ``location``."""
    return project_root / location / ARCHIVED_DIRNAME / stub


def archived_canonical(location: str, stub: str) -> str:
    """The project-relative parking path, in canonical (posix) form."""
    return f"{location}/{ARCHIVED_DIRNAME}/{stub}"


def is_parked_parts(parts: tuple[str, ...]) -> bool:
    """True when project-relative ``parts`` lie inside a parking directory.

    Used by discovery to skip the parked subtree under either root. It takes
    PARTS rather than a path so it works for a folder and for a document
    inside one, and so ``dev/tasks`` (two segments) is matched as a unit."""
    for root in KNOWN_ROOTS:
        root_parts = tuple(root.split("/"))
        depth = len(root_parts)
        if (
            parts[:depth] == root_parts
            and len(parts) > depth
            and parts[depth] == ARCHIVED_DIRNAME
        ):
            return True
    return False


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


def _reserved_message(location: str, original: str) -> str:
    return (
        f"{location}/{ARCHIVED_DIRNAME} is the reserved parking directory "
        f"for archived tasks, not a task: {original!r}"
    )


def _classify_parts(parts: tuple[str, ...], original: str) -> ResolvedRef:
    """Classify normalized parts as a tmp or dev/tasks task path."""
    if len(parts) == 2 and parts[0] == "tmp":
        if parts[1] == ARCHIVED_DIRNAME:
            raise RefResolutionError(_reserved_message(LOCATION_TMP, original))
        return ResolvedRef("/".join(parts), LOCATION_TMP, parts[1])
    if len(parts) == 3 and parts[0] == "dev" and parts[1] == "tasks":
        if parts[2] == ARCHIVED_DIRNAME:
            raise RefResolutionError(
                _reserved_message(LOCATION_DEV_TASKS, original)
            )
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
