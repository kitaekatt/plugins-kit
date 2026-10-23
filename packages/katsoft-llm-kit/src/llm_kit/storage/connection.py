"""SQLite connection mechanics. Standard library only.

One function opens a connection from a :class:`ConnectionPolicy`; the policy
records exactly which settings, in which order, a product applies. Nothing
here knows a product schema, a migration step or an ownership rule.

Legacy equivalence, by product (all three open ``sqlite3.connect(path,
timeout=busy_timeout_ms / 1000.0)`` and then):

- pipeline execution store: ``row_factory = Row``; ``PRAGMA busy_timeout``;
  WAL (timeout error wrapped); ``PRAGMA foreign_keys = ON``.
- job ledger: the same sequence, but a WAL write that is still locked at the
  deadline re-raises the original error.
- spend ledger: ``row_factory = Row``; ``isolation_level = None``; ``PRAGMA
  busy_timeout``; WAL (timeout error wrapped); ``PRAGMA synchronous = <value>``.
"""

from __future__ import annotations

import sqlite3
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Tuple, Union

DEFAULT_BUSY_TIMEOUT_MS = 5000

# What ``ensure_wal`` does when ``PRAGMA journal_mode = WAL`` is still locked
# once the busy-timeout budget is spent.
WAL_REPORT_TIMEOUT = "report_timeout"  # raise OperationalError naming the budget
WAL_RERAISE = "reraise"  # re-raise the original "database is locked" error
_WAL_FAILURES = (WAL_REPORT_TIMEOUT, WAL_RERAISE)

_WAL_RETRY_SLEEP_SECONDS = 0.005


@dataclass(frozen=True)
class ConnectionPolicy:
    """The per-connection posture one product applies, in its legacy order.

    ``busy_timeout_ms`` feeds the ``sqlite3.connect`` timeout, ``PRAGMA
    busy_timeout`` and the WAL retry budget. ``explicit_transactions`` sets
    ``isolation_level = None`` (autocommit; the caller issues BEGIN) before any
    pragma. ``post_wal_pragmas`` are ``(name, value)`` pairs rendered as
    ``PRAGMA name = value`` after the WAL step, in the order given.
    """

    busy_timeout_ms: Union[int, float] = DEFAULT_BUSY_TIMEOUT_MS
    post_wal_pragmas: Tuple[Tuple[str, str], ...] = ()
    explicit_transactions: bool = False
    wal_failure: str = WAL_REPORT_TIMEOUT
    row_factory: Any = sqlite3.Row

    def __post_init__(self) -> None:
        if self.wal_failure not in _WAL_FAILURES:
            raise ValueError(
                "wal_failure must be one of %s, got %r" % (_WAL_FAILURES, self.wal_failure)
            )
        for item in self.post_wal_pragmas:
            if len(item) != 2 or not str(item[0]).isidentifier():
                raise ValueError(
                    "post_wal_pragmas entries must be (name, value) with an "
                    "identifier name, got %r" % (item,)
                )


def ensure_wal(
    conn: sqlite3.Connection,
    busy_timeout_ms: Union[int, float],
    *,
    wal_failure: str = WAL_REPORT_TIMEOUT,
) -> None:
    """Put ``conn`` into WAL mode without racing concurrent first-opens.

    ``PRAGMA journal_mode`` with no argument is a lock-free read. The form
    that SETS the mode takes a brief write lock even when the database is
    already in WAL mode, and does NOT honour ``busy_timeout``: it fails almost
    instantly with "database is locked". So read first and skip the write when
    the mode is already ``wal``; when the write is needed, retry it here
    against the busy-timeout budget. Any error other than a locked database is
    raised immediately.
    """
    if wal_failure not in _WAL_FAILURES:
        raise ValueError("wal_failure must be one of %s, got %r" % (_WAL_FAILURES, wal_failure))
    current = conn.execute("PRAGMA journal_mode").fetchone()[0]
    if isinstance(current, str) and current.lower() == "wal":
        return

    deadline = time.monotonic() + (busy_timeout_ms / 1000.0)
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if time.monotonic() >= deadline:
                if wal_failure == WAL_REPORT_TIMEOUT:
                    raise sqlite3.OperationalError(
                        f"could not set WAL journal mode within "
                        f"{busy_timeout_ms} ms (database is locked): {exc}"
                    ) from exc
                raise
            time.sleep(_WAL_RETRY_SLEEP_SECONDS)


@contextmanager
def connect(db_path: Union[str, Path], policy: ConnectionPolicy) -> Iterator[sqlite3.Connection]:
    """Open one connection, apply ``policy`` in order, close on exit."""
    conn = sqlite3.connect(str(db_path), timeout=policy.busy_timeout_ms / 1000.0)
    try:
        conn.row_factory = policy.row_factory
        if policy.explicit_transactions:
            conn.isolation_level = None  # explicit BEGIN; no implicit transaction
        conn.execute(f"PRAGMA busy_timeout = {int(policy.busy_timeout_ms)}")
        ensure_wal(conn, policy.busy_timeout_ms, wal_failure=policy.wal_failure)
        for name, value in policy.post_wal_pragmas:
            conn.execute(f"PRAGMA {name} = {value}")
        yield conn
    finally:
        conn.close()


@contextmanager
def connect_probe(
    db_path: Union[str, Path],
    busy_timeout_ms: Union[int, float],
    *,
    set_busy_timeout: bool,
) -> Iterator[sqlite3.Connection]:
    """Open a connection that changes nothing: no WAL, no other pragma.

    Used to read a stored version before a product decides whether to write
    anything. ``set_busy_timeout`` adds the ``busy_timeout`` pragma (the spend
    ledger's probe) or leaves it off (the job ledger's probe).
    """
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout_ms / 1000.0)
    try:
        conn.row_factory = sqlite3.Row
        if set_busy_timeout:
            conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        yield conn
    finally:
        conn.close()


def readonly_uri(db_path: Union[str, Path]) -> str:
    """The ``mode=ro`` SQLite URI for ``db_path``."""
    path = urllib.parse.quote(Path(db_path).resolve().as_posix())
    return f"file:{path}?mode=ro"


@contextmanager
def connect_readonly(
    db_path: Union[str, Path], busy_timeout_ms: Union[int, float]
) -> Iterator[sqlite3.Connection]:
    """Open a read-only connection that cannot write, even by pragma.

    ``mode=ro`` URI, ``PRAGMA query_only = ON``, then ``PRAGMA busy_timeout``;
    no journal mode is set.
    """
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, timeout=busy_timeout_ms / 1000.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        yield conn
    finally:
        conn.close()
