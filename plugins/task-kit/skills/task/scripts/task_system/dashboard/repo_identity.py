"""Dashboard-side task identity: which repository a task belongs to.

A task is shown as ``<repo>:<stub>``. The repository name is the basename of the
``origin`` remote URL (``.git`` stripped), so every checkout of one repository
(plugins-kit, plugins-kit2, plugins-kit3) is one project. A directory with no
``origin`` remote is identified by its own directory name.

This is a rendering and grouping key only. Task ids, ``task_list`` references
and the task verbs stay path-based (``dev/tasks/<stub>``) and never see it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import vcs

_URL_SEPARATORS = re.compile(r"[/\\:]")
_GIT_SUFFIX = ".git"


def repo_name_from_remote(url: str) -> str | None:
    """The repository name a remote URL ends in, or None when it names nothing.

    Handles ``https://host/org/repo.git``, ``ssh://git@host/org/repo``,
    ``git@host:org/repo.git``, trailing slashes and local paths of either style.
    """
    text = url.strip().rstrip("/\\")
    name = _URL_SEPARATORS.split(text)[-1] if text else ""
    if name.lower().endswith(_GIT_SUFFIX):
        name = name[: -len(_GIT_SUFFIX)]
    return name or None


def repo_name_for(root: Path) -> str:
    """The repository name for a checkout; raises :class:`vcs.GitError` when git
    cannot say (the caller falls back to the directory name and reports it)."""
    url = vcs.remote_url(Path(root))
    return (repo_name_from_remote(url) if url else None) or Path(root).name


def repo_id_for(name: str) -> str:
    """Project identity in the API: SHA-256 of the case-folded repository name.

    Names are not guaranteed unique across unrelated repositories; the user
    accepted that for a display key (two such repositories share one project).
    """
    return hashlib.sha256(("repo:" + name.casefold()).encode("utf-8")).hexdigest()
