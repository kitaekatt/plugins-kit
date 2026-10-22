"""An OS file lock: ``flock`` on POSIX, ``msvcrt.locking`` on Windows.

The operating system drops the lock when the holding process dies, so a crashed holder never leaves a
lock behind. Two handles on one file exclude each other, within a process and across processes.

- :func:`try_lock` / :func:`unlock` are the primitives on an open handle (the dashboard server holds one
  for its whole lifetime).
- :class:`FileLock` is the blocking-with-timeout context manager the task mutator and the session store
  use. A timeout raises :class:`LockTimeout`; a caller wraps it in its own error type.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

LOCK_POLL_S = 0.05


class LockTimeout(OSError):
    """The lock stayed held by another handle for the whole wait."""

    def __init__(self, path: Path, timeout_s: float) -> None:
        super().__init__(f"{path} stayed locked for {timeout_s:g} seconds")
        self.path = Path(path)
        self.timeout_s = timeout_s


def try_lock(handle) -> bool:
    """Take an exclusive, non-blocking lock on the open ``handle``; False when another handle holds it."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(handle) -> None:
    """Release a lock taken with :func:`try_lock`."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileLock:
    """A blocking-with-timeout OS file lock; separate handles exclude each other."""

    def __init__(self, path: Path, timeout_s: float, poll_s: float = LOCK_POLL_S) -> None:
        self.path = Path(path)
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            handle = open(self.path, "a+b")
            if try_lock(handle):
                self._handle = handle
                return self
            handle.close()
            if time.monotonic() >= deadline:
                raise LockTimeout(self.path, self.timeout_s)
            time.sleep(self.poll_s)

    def __exit__(self, *_exc) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            unlock(handle)
        finally:
            handle.close()
