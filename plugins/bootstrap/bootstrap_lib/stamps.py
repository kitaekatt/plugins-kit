"""Stamps: small string-valued marker files bootstrap uses to remember state
across passes (cooldown epoch, ``last_version``, ``engine_ran_version``).

ONE atomic-write convention (``write_atomic`` -> mkstemp + ``os.replace``) and
ONE missing-file convention (``read`` returns the caller's default; never raises
on a missing stamp). The module is generic over SCOPE (where the file lives) and
VALUE (an opaque string the caller parses -- epoch int, version string, ...); it
deliberately bakes in NO "timestamp" semantics.

mtime is load-bearing for the cooldown stamp: ``session-bootstrap.sh`` gates the
per-project throttle with ``[ ! installed_plugins.json -nt <cooldown stamp> ]``,
so a cooldown SKIP must NOT refresh the stamp. Therefore writes/touches are
always EXPLICIT (``Stamp.write``) and reads never modify mtime. ``Stamp.mtime``
exposes the value for callers (and the bash ``-nt`` gate's Python-side mirror).

bash/Python boundary: the per-project cooldown stamp is also read/written by
``hooks/sessionstart/session-bootstrap.sh`` (before Python is available). bash
and Python share the PATH CONVENTION -- ``cooldowns/<name>.<sha1-of-cwd>`` -- not
a single function. Keep ``project_stamp()``'s layout in lockstep with the shell's
``_COOLDOWN_FILE`` construction; there is intentionally no shared implementation
across the bash/Python boundary, only a shared path format.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional, Union

from .atomic_write import write_atomic

_PathLike = Union[str, "os.PathLike[str]"]

# Matches session-bootstrap.sh's fallback bucket when no usable project_dir/hash
# is available (``_PROJECT_KEY="_global_"``).
_GLOBAL_PROJECT_KEY = "_global_"


def _project_key(project_dir: Optional[str]) -> str:
    """``sha1(project_dir)`` hex, or ``"_global_"`` when no ``project_dir`` (or
    hashing fails). Mirrors ``session-bootstrap.sh``'s ``_PROJECT_KEY`` exactly so
    the bash and Python sides resolve the same cooldown file."""
    if project_dir:
        try:
            return hashlib.sha1(project_dir.encode("utf-8")).hexdigest()
        except Exception:
            pass
    return _GLOBAL_PROJECT_KEY


class Stamp:
    """A single string-valued marker file with atomic write + safe read.

    Construct via the scope helpers (:func:`global_stamp`, :func:`plugin_stamp`,
    :func:`project_stamp`) rather than directly, so the path conventions stay in
    one place.
    """

    def __init__(self, path: _PathLike) -> None:
        self.path = Path(path)

    def read(self, default: str = "") -> str:
        """Return the stamp's value (stripped), or ``default`` if the file is
        missing/unreadable. Pure: never creates the file and never touches its
        mtime -- the single missing-file convention for the whole module."""
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, ValueError):
            return default

    def write(self, value: str) -> None:
        """Atomically write ``value`` (mkstemp + ``os.replace``), creating parent
        dirs as needed. This is the ONLY way the stamp's mtime advances -- the
        explicit touch the cooldown ``-nt`` gate relies on."""
        write_atomic(str(self.path), str(value))

    def clear(self) -> None:
        """Remove the stamp if present. Idempotent -- a missing stamp is treated
        as already-cleared (never raises), matching the read convention."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def exists(self) -> bool:
        """True if the stamp file exists."""
        return self.path.is_file()

    def mtime(self) -> Optional[float]:
        """The stamp's mtime (epoch seconds), or ``None`` if missing/unreadable.
        Reading the mtime does not modify it."""
        try:
            return self.path.stat().st_mtime
        except (FileNotFoundError, OSError):
            return None


def global_stamp(data_dir: _PathLike, name: str) -> Stamp:
    """A bootstrap-global stamp at ``<data_dir>/<name>`` (e.g. ``last_version``,
    ``engine_ran_version``)."""
    return Stamp(Path(data_dir) / name)


def plugin_stamp(plugin_data_dir: _PathLike, name: str) -> Stamp:
    """A per-plugin stamp at ``<plugin_data_dir>/<name>`` (e.g. a plugin's own
    ``last_version``)."""
    return Stamp(Path(plugin_data_dir) / name)


def project_stamp(data_dir: _PathLike, name: str, project_dir: Optional[str]) -> Stamp:
    """A per-project stamp at ``<data_dir>/cooldowns/<name>.<sha1-of-project_dir>``.

    The path layout matches ``session-bootstrap.sh``'s ``_COOLDOWN_FILE``
    (``cooldowns/last_run_epoch.<sha1>``) EXACTLY, so the bash ``-nt`` gate and
    the Python cooldown helpers operate on the same file. ``name`` is the stem
    (``"last_run_epoch"``); the ``sha1`` suffix keys it per CWD. A falsy
    ``project_dir`` falls back to the ``_global_`` bucket, like the shell.
    """
    key = _project_key(project_dir)
    return Stamp(Path(data_dir) / "cooldowns" / f"{name}.{key}")
