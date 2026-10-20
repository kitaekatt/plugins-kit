"""Dashboard-side task identity: which repository a task belongs to.

A task is shown as ``<repo>:<stub>``. The repository name is the basename of the
``origin`` remote URL (``.git`` stripped), so every checkout of one repository
(plugins-kit, plugins-kit2, plugins-kit3) is one project. A directory with no
``origin`` remote is identified by its own directory name.

This is a rendering and grouping key only. Task ids, ``task_list`` references
and the task verbs stay path-based (``dev/tasks/<stub>``) and never see it.

Consent is different. An endpoint helper is offered to a run only for a repository its
configuration names, and that decision is made on :func:`repo_key_for`, never on the display
name above (two unrelated repositories can share one): the ``origin`` URL normalized so every
spelling of one remote is one key, or ``path:<realpath>`` for a checkout with no ``origin``.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

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


PATH_KEY_PREFIX = "path:"
FILE_KEY_PREFIX = "file:"
_SCP_LIKE = re.compile(r"^(?:[^@/\\\s]+@)?(?P<host>[^:/\\\s]+):(?!//)(?P<path>.+)$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_DEFAULT_PORTS = {"ssh": 22, "https": 443, "http": 80, "git": 9418}


def _trim_repo_path(path: str) -> str:
    path = path.strip().replace("\\", "/").strip("/")
    if path.lower().endswith(_GIT_SUFFIX):
        path = path[: -len(_GIT_SUFFIX)].rstrip("/")
    return path


def path_key(path: str | Path) -> str:
    """The consent key of a checkout that has no ``origin``: ``path:`` plus its case-normalized real path."""
    return PATH_KEY_PREFIX + os.path.normcase(os.path.realpath(str(path)))


def normalize_remote(url: str) -> str:
    """One key for every spelling of a remote URL; raises ``ValueError`` when the text names no repository.

    Scheme and user are dropped, the host is lowercased (and so is a default port dropped),
    ``git@host:org/repo`` becomes ``host/org/repo``, and ``.git`` and trailing slashes are
    stripped. The repository path keeps its case: a host that tells ``Org/Repo`` from ``org/repo``
    must not have one consent cover both. A remote that is a local path becomes ``file:<path>``.
    """
    text = url.strip()
    if not text:
        raise ValueError("an empty repository URL")
    if _WINDOWS_DRIVE.match(text) or text.startswith(("/", "\\\\")) or text.startswith("file://"):
        local = urlsplit(text).path if text.startswith("file://") else text
        trimmed = _trim_repo_path(local)
        if not trimmed:
            raise ValueError(f"{url!r} names no repository")
        return FILE_KEY_PREFIX + os.path.normcase("/" + trimmed)
    if "://" in text:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        port = parts.port
        path = _trim_repo_path(parts.path)
        scheme = parts.scheme.lower()
    else:
        match = _SCP_LIKE.match(text)
        if match is None:
            raise ValueError(f"{url!r} is not a repository URL")
        host, port, path, scheme = match.group("host").lower(), None, _trim_repo_path(match.group("path")), "ssh"
    if not host or not path:
        raise ValueError(f"{url!r} names no repository")
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return f"{host}/{path}"


def repo_key_for(root: Path) -> str:
    """The consent key of the checkout at ``root``; raises :class:`vcs.GitError` when git cannot say."""
    url = vcs.remote_url(Path(root))
    return normalize_remote(url) if url else path_key(root)


def config_key(text: str) -> str:
    """The consent key a configured ``repositories`` entry names.

    An absolute path names a checkout with no ``origin`` (``path:`` plus its real path); anything else is an
    origin URL in any spelling. Raises ``ValueError`` when the text is neither.
    """
    value = text.strip()
    if value.startswith(PATH_KEY_PREFIX):
        value = value[len(PATH_KEY_PREFIX):]
        if not value:
            raise ValueError("an empty path")
        return path_key(value)
    if os.path.isabs(value) or _WINDOWS_DRIVE.match(value) or value.startswith("\\\\"):
        return path_key(value)
    return normalize_remote(value)
