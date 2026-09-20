"""Platform-independent link detection.

``os.path.islink`` is already cross-platform, so it is not the gap. The gap
is Windows **directory junctions**: a junction is a reparse point, and
``islink`` reports ``False`` for it -- only ``os.path.isjunction`` (added in
Python 3.12) sees it. A predicate that checks ``islink`` alone therefore
treats every junction on a Windows host as an ordinary directory. Do not
"simplify" :func:`is_link` back to ``islink`` alone; that regresses Windows
junction detection silently, with no test failure on a non-Windows CI runner
to catch it.
"""

import os
from typing import Union


def is_link(path: Union[str, "os.PathLike[str]"]) -> bool:
    """Return True if ``path`` is a link of any kind, on any platform.

    Covers both POSIX/Windows symlinks (``os.path.islink``) and Windows
    directory junctions (``os.path.isjunction``, Python >= 3.12; treated as
    "not a junction" on older interpreters via ``getattr`` fallback, since
    junctions cannot be detected without it). A dangling symlink still
    counts as a link -- ``islink`` does not dereference the target, so a
    missing destination does not raise and does not change the answer. A
    path that does not exist at all is not a link, and neither check raises
    for it.

    Junction behavior is UNVERIFIED ON A REAL WINDOWS HOST; tests model the
    documented Python semantics.

    The bootstrap plugin's floor is Python 3.12 (see pyproject.toml
    ``requires-python``), where ``os.path.isjunction`` always exists -- but
    this module is imported by other bootstrap_lib call sites too, so look
    it up with ``getattr`` at call time rather than assume the floor never
    moves underneath it (and so a test can monkeypatch ``os.path.isjunction``
    and have this function see it).
    """
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return os.path.islink(path) or isjunction(path)
    return os.path.islink(path)
