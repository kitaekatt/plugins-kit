"""Dead-entry detection for the Windows User PATH.

The engine half of dead-path pruning. It only ever DETECTS and caches; the
removal happens in :mod:`bootstrap_lib.fix_runner` (a ``path_prune`` queue
task), because deleting PATH entries is destructive and must be something the
user consented to, not something a background SessionStart hook did to their
machine while they were reading the scrollback.

Why the User PATH accumulates garbage
-------------------------------------
Entries are appended by installers and by bootstrap itself; nothing ever removes
one. A directory that later disappears leaves an entry pointing at nothing, and
because each is textually unique nothing collapses them either. The live example
that motivated this module: bootstrap's own test suite leaked a fresh tmp-dir
entry per run into the developer's real PATH, reaching 30 dead entries out of 37
-- 81% garbage. That leak is fixed at its source, but the residue is still
there, and no existing mechanism could ever clean it.

This matters beyond tidiness: a bloated PATH is exactly what
:mod:`bootstrap_lib.path_repair` exists to survive (cmd.exe silently truncates
an oversized PATH during venv activation, and the Python child then inherits a
stripped PATH and cannot find its tools).

Detection is cached; the FINDING is not
---------------------------------------
Scanning means a filesystem probe per entry, some of which may be slow or hang
(an offline network share). So the scan is gated on the PATH actually having
changed, keyed by a hash of the raw registry value.

The subtle part -- and the thing that makes "skip once" not mean "never again"
-- is WHAT is cached. Caching "did I already report this?" would be a bug: the
user declines, the PATH does not change, detection is skipped, and the finding
goes silent forever. So the cache stores the RESULT (the dead entries), and the
caller surfaces a finding whenever that result is non-empty -- rescan or not.
The state machine that falls out:

  * PATH unchanged, still dirty -> no scan, finding re-surfaces every session;
  * PATH pruned                 -> hash changed, rescan, result empty, finding
                                   self-clears (no "fixed" ritual needed);
  * a new dead entry appears    -> hash changed, rescan, finding names it.

Windows-only. Elsewhere PATH comes from shell rc files, where "dead entry" is a
different question with a different owner.
"""

import hashlib
import json
import os
import sys
from typing import List, Optional, Tuple

# The deadness predicate lives in fix_runner, not here, and this module borrows
# it back -- the same direction fix_queue already borrows that module's
# constants. It has to: fix_runner re-checks deadness at PRUNE time (a verdict
# cached here can be stale by then) and must work when run as a bare script with
# no package context, so it cannot import this module. One copy of the rule, in
# the place with the harder constraint.
from .fix_runner import is_dead

# The registry value the whole module is about.
_ENV_KEY = "Environment"
_PATH_VALUE = "Path"

# Cache file (a stamp, but JSON rather than a bare string -- it holds the hash
# AND the result, which must move together or the state machine above breaks).
STAMP_NAME = "path_prune.json"


def stamp_path(data_dir: str) -> str:
    return os.path.join(data_dir, STAMP_NAME)


def read_user_path() -> Optional[Tuple[str, int]]:
    """``(raw_value, value_type)`` of HKCU\\Environment Path, else None.

    None means "nothing to do here": not Windows, no winreg, the registry is
    off-limits, or the value does not exist. Deliberately NOT an empty-string
    fallback -- "no PATH value at all" and "an empty PATH value" are different
    states, and only the caller knows that both simply mean there is nothing to
    prune.

    BOOTSTRAP_SKIP_REGISTRY suppresses the READ, not just writes. The registry
    is global state that ignores the HOME isolation tests rely on, so a scan
    inside a test would read the DEVELOPER's PATH: every engine test would then
    inherit however much dead junk that machine happens to have and report a
    finding that has nothing to do with the test. (This is not hypothetical --
    it turned 25 unrelated engine tests red the first time this ran.) The var
    already means "do not touch the real registry"; reading couples to it just
    as surely as writing does.
    """
    if sys.platform != "win32" or os.environ.get("BOOTSTRAP_SKIP_REGISTRY"):
        return None
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, _PATH_VALUE)
    except OSError:
        return None
    if not isinstance(value, str):
        return None
    return value, value_type


def path_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def split_entries(raw: str) -> List[str]:
    """PATH entries, verbatim and in order, minus the empty ones.

    Entries keep their original spelling -- unexpanded variables, trailing
    slashes and all. The prune removes entries BY THEIR TEXT, so any
    normalization here would be a bug: it would hand the runner a string that
    does not appear in the registry.
    """
    return [e for e in raw.split(";") if e.strip()]


def dead_entries(raw: str) -> List[str]:
    """The entries of `raw` that name nonexistent directories, in PATH order."""
    return [e for e in split_entries(raw) if is_dead(e)]


def _read_stamp(data_dir: str) -> Tuple[Optional[str], List[str]]:
    """``(cached_hash, cached_dead)``; ``(None, [])`` when unreadable.

    A corrupt/absent stamp degrades to "no cache" -- forcing a rescan, never a
    crash and never a stale answer.
    """
    try:
        with open(stamp_path(data_dir), "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return None, []
    if not isinstance(body, dict):
        return None, []
    cached_hash = body.get("path_hash")
    cached_dead = body.get("dead")
    if not isinstance(cached_hash, str) or not isinstance(cached_dead, list):
        return None, []
    return cached_hash, [e for e in cached_dead if isinstance(e, str)]


def _write_stamp(data_dir: str, current_hash: str, dead: List[str]) -> None:
    from .atomic_write import write_atomic

    write_atomic(
        stamp_path(data_dir),
        json.dumps({"path_hash": current_hash, "dead": dead}, indent=2) + "\n",
    )


def scan(data_dir: str) -> Optional[List[str]]:
    """The dead entries in the current User PATH, scanning only when it changed.

    Returns the cached result verbatim on a hash hit -- so a user who declines
    the prune still gets the finding next session (see the module docstring),
    without paying for the filesystem probes again.

    ``None`` means NO VERDICT: there was no User PATH to read (not Windows, or
    the registry is off-limits). That is distinct from ``[]``, which means the
    scan ran and found it clean. Collapsing the two would let a check that never
    ran report itself as a check that passed.
    """
    current = read_user_path()
    if current is None:
        return None
    raw, _value_type = current
    current_hash = path_hash(raw)
    cached_hash, cached_dead = _read_stamp(data_dir)
    if cached_hash == current_hash:
        return cached_dead
    dead = dead_entries(raw)
    _write_stamp(data_dir, current_hash, dead)
    return dead
