"""Schema-version read primitive. Standard library only.

Reads the ``schema_version`` table both SQLite ledgers keep. It performs no
write and takes no write lock. Creating the table, inserting or updating the
version row, and deciding which steps to apply are product concerns and stay
with each product's migration loop.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def read_schema_version(conn: sqlite3.Connection, *, as_int: bool = False) -> int:
    """Return ``MAX(version)`` from ``schema_version``, or 0 when absent or empty.

    Plain reads only, so it is safe inside or outside a transaction. With
    ``as_int`` the stored value is coerced through ``int()`` (the job ledger's
    posture); without it the stored value is returned as read (the pipeline
    store's posture).
    """
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if table is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    value: Optional[object] = row[0] if row else None
    if value is None:
        return 0
    return int(value) if as_int else value  # type: ignore[return-value]
