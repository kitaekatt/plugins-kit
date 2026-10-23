"""Transaction scopes over a product-supplied connection. Standard library only.

Each scope takes a zero-argument ``connect`` callable that returns a context
manager yielding a connection (a product's own ``_connect`` method, or a lambda
over :func:`llm_kit.storage.connection.connect`). The scope issues the BEGIN,
runs the optional ``validate`` hook inside the transaction, and commits on a
clean exit or rolls back on any exception, which then propagates unchanged.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator, Optional

Connect = Callable[[], ContextManager[sqlite3.Connection]]
Validate = Callable[[sqlite3.Connection], None]

BEGIN_WRITE = "BEGIN IMMEDIATE"
BEGIN_READ = "BEGIN"


@contextmanager
def transaction(
    connect: Connect,
    begin: str,
    *,
    validate: Optional[Validate] = None,
    rollback_on_exit: bool = False,
) -> Iterator[sqlite3.Connection]:
    """Run one transaction opened with the statement ``begin``.

    ``validate`` runs after BEGIN and before the body, inside the rollback
    guard, so a refusal leaves nothing written. With ``rollback_on_exit`` a
    clean exit rolls back instead of committing (a read-only snapshot that must
    never write).
    """
    with connect() as conn:
        conn.execute(begin)
        if rollback_on_exit:
            try:
                if validate is not None:
                    validate(conn)
                yield conn
            finally:
                conn.rollback()
            return
        try:
            if validate is not None:
                validate(conn)
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def write_transaction(
    connect: Connect, *, validate: Optional[Validate] = None
) -> ContextManager[sqlite3.Connection]:
    """``BEGIN IMMEDIATE``: take the write lock up front, commit or roll back."""
    return transaction(connect, BEGIN_WRITE, validate=validate)


def read_transaction(
    connect: Connect,
    *,
    validate: Optional[Validate] = None,
    rollback_on_exit: bool = False,
) -> ContextManager[sqlite3.Connection]:
    """Plain deferred ``BEGIN``: one consistent read snapshot.

    Read-only by contract; the caller never writes inside it.
    """
    return transaction(
        connect, BEGIN_READ, validate=validate, rollback_on_exit=rollback_on_exit
    )
