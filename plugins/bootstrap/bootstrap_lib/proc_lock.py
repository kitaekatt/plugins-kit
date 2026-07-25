"""Single-instance mutex for the bootstrap engine.

Several independent paths can launch bootstrap_engine.py: session-bootstrap.sh
(SessionStart), the harvest's launch_new_engine (UserPromptSubmit, on a
version bump or registry change), and the SessionStart-missed rescue in
bootstrap-display.sh. Each of those has its own throttle against re-launching
(session_id guard, per-project cooldown, a launched-version stamp), but none
of them checks whether an engine pass is ACTIVELY RUNNING right now -- rapid
session start/exit/restart can fire several launchers within the same few
seconds, before any of them has completed and stamped its guard, producing
concurrent engine processes (observed: three at once, one of which crashed
mid-pass with a shutil.copytree race in the shared-lib sync).

This is a true mutex, independent of and in addition to those throttles: a
PID lock file in data_dir. Stale-checked against the recorded PID's liveness
(so a crashed or killed engine's lock is recovered promptly) AND against the
lock file's age (so a dead PID number later reused by an unrelated live
process can't wedge the lock forever) -- either way it can never wedge the
lock for future sessions.
"""

from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager
from typing import Optional

LOCK_FILENAME = "engine.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check for a PID, POSIX and Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, owned by someone else
    except OSError:
        return False
    return True


def _read_lock_pid(lock_path: str) -> Optional[int]:
    try:
        with open(lock_path, "r") as f:
            first_line = f.readline().strip()
        return int(first_line)
    except (OSError, ValueError):
        return None


def _lock_age_seconds(lock_path: str) -> Optional[float]:
    """Seconds since the lock file's mtime, or ``None`` if it doesn't exist."""
    try:
        return time.time() - os.stat(lock_path).st_mtime
    except OSError:
        return None


def _create_exclusive(lock_path: str, payload: str) -> bool:
    """Atomic create-exclusive: True on success, False if the file already
    exists (or the exclusive-create race was lost). The only path that ever
    claims the lock -- the steal path below reduces to this same call after
    clearing a stale file, so two processes can never both believe they hold
    the lock.

    PermissionError, not just FileExistsError, means "lost the race" here:
    on Windows, two concurrent O_CREAT|O_EXCL opens against the same path can
    surface the loser's failure as PermissionError rather than
    FileExistsError (a CreateFile sharing-violation artifact of the exclusive
    create, not a genuine ACL problem) -- observed directly in this module's
    own concurrency test. Treating it as "someone else is creating this
    right now" is the fail-closed behavior a lock needs: the caller falls
    through to reading back whatever now exists (or retries once), never to
    believing it holds an uncontended lock.
    """
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        return True
    except (FileExistsError, PermissionError):
        return False


_MAX_ACQUIRE_ATTEMPTS = 10

# A lock file that exists but whose content isn't a parseable PID (the
# window between _create_exclusive's os.open() and its payload write
# landing) is presumed IN-FLIGHT -- not stale -- while it's younger than
# this. A normal write lands in microseconds, so this is generous headroom
# against a slow disk, not a real "someone is still writing" wait.
_EMPTY_LOCK_GRACE_SECONDS = 1.0

# A lock whose recorded PID reads as alive is still treated as stale once
# its file has aged past this ceiling. Guards against PID reuse: a crashed
# holder's PID number recycled by an unrelated live process would otherwise
# wedge the lock forever (liveness alone can never expire). Deliberately
# generous -- a real pass can legitimately hold the lock a long time (a
# single env_check entry's timeout defaults to 600s and can be configured
# far higher for a large download; the --fix-all elevation flow waits on a
# UAC prompt plus every queued task's own timeout on top of that) and this
# ceiling exists only as a last-resort recovery for the rare PID-reuse case,
# not as the primary staleness signal (liveness is). Six hours comfortably
# exceeds any plausible legitimate single pass.
_STALE_AGE_SECONDS = 6 * 3600


def _try_acquire(lock_path: str) -> bool:
    pid = os.getpid()
    payload = f"{pid}\n{time.time()}\n"

    # Bounded retry loop (never loops indefinitely -- a pathological case
    # exhausts it and fails closed, standing down rather than spinning
    # forever). More than "one retry after clearing a stale lock" is needed
    # in practice: under N-way contention on the SAME stale lock, several
    # racers can all lose an attempt (someone else's create won first, or --
    # observed directly on Windows -- a just-created file gets a transient
    # PermissionError from e.g. AV scanning before its write completes,
    # which _create_exclusive treats as "lost the race" too). A short
    # jittered backoff between attempts lets that contention drain instead
    # of every loser giving up in lockstep with none of them ever winning.
    for attempt in range(_MAX_ACQUIRE_ATTEMPTS):
        if _create_exclusive(lock_path, payload):
            return True

        existing_pid = _read_lock_pid(lock_path)
        age = _lock_age_seconds(lock_path)

        if existing_pid is None and age is not None and age < _EMPTY_LOCK_GRACE_SECONDS:
            # The file exists but its payload hasn't landed yet -- another
            # process's _create_exclusive is still mid-write. NOT stale:
            # unlinking here would steal the true winner's lock out from
            # under it (observed directly -- this was the exact cause of a
            # flaky 8-way contention test). Just back off and re-check.
            time.sleep(random.uniform(0.001, 0.01))
            continue

        if (
            existing_pid is not None
            and _pid_alive(existing_pid)
            and (age is None or age < _STALE_AGE_SECONDS)
        ):
            return False  # a live engine holds the lock

        # Stale: either a confirmed-dead PID, unparseable content that has
        # sat past the in-flight grace window (a holder that crashed between
        # its open() and its payload write), or a live-looking PID whose
        # lock has aged past _STALE_AGE_SECONDS (PID reuse). Clear it and
        # retry the EXCLUSIVE create above, rather than writing over it
        # non-exclusively -- a non-exclusive overwrite lets two processes
        # that both observe the same stale lock both "win" and run
        # concurrently, exactly what this lock exists to prevent. Clearing
        # and retrying through _create_exclusive means at most one racer
        # wins each retry; the rest see FileExistsError/PermissionError
        # again and correctly re-check the (now live, or still contested)
        # holder.
        #
        # The removal is ownership-conditional (_remove_if_owned against the
        # PID we JUST judged stale, re-read at removal time), not a blind
        # unlink: without that, a racer that wins the exclusive-create
        # between our read and our unlink would have its brand-new, valid
        # lock deleted out from under it by this call.
        _remove_if_owned(lock_path, existing_pid)

        if attempt < _MAX_ACQUIRE_ATTEMPTS - 1:
            time.sleep(random.uniform(0.001, 0.01))

    return False


def _remove_if_owned(lock_path: str, pid: Optional[int]) -> None:
    """Remove the lock file only if it still records ``pid`` (or both the
    file's current content and ``pid`` are unreadable/None) -- never removes
    a lock some OTHER process has since created or claimed. Best-effort;
    never raises."""
    owner = _read_lock_pid(lock_path)
    if owner != pid:
        return
    try:
        os.remove(lock_path)
    except OSError:
        pass


def release_lock(data_dir: str) -> None:
    """Best-effort early release of a lock THIS process holds.

    For a caller that must let another process acquire the lock before this
    process's own engine_lock() context manager exits -- e.g. the fix-all
    elevation flow synchronously spawns a CHILD engine process (same
    --data-dir) and waits for it; without releasing first, the child would
    see this (still-running) process's PID as the live holder and stand down
    without doing its post-elevation re-check. Safe to call from deep inside
    a pass: only removes the lock file if it still records OUR pid, so it
    can never touch a lock another process has since legitimately acquired.
    The owning engine_lock()'s own exit is a no-op afterward (removing an
    already-removed, or since-reacquired-by-someone-else, file is handled by
    the same ownership check).
    """
    lock_path = os.path.join(data_dir, LOCK_FILENAME)
    _remove_if_owned(lock_path, os.getpid())


@contextmanager
def engine_lock(data_dir: str):
    """Context manager yielding True if the lock was acquired (caller should
    run its pass) or False if another engine instance is currently active
    (caller decides how to stand down -- proc_lock itself touches no
    cooldowns or logs; see engine.py's _stand_down_lock_contended for what
    the bootstrap engine's caller actually does on a False yield). Releases
    the lock on the way out whenever this process is the one that acquired
    it, including on exception -- unless the caller already released it
    early via release_lock() (idempotent either way).
    """
    os.makedirs(data_dir, exist_ok=True)
    lock_path = os.path.join(data_dir, LOCK_FILENAME)
    acquired = False
    try:
        acquired = _try_acquire(lock_path)
        yield acquired
    finally:
        if acquired:
            _remove_if_owned(lock_path, os.getpid())
