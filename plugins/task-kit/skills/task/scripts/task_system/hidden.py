"""``task.hidden_until``: a task hidden from task menus until a calendar date.

The one rule. ``task.yaml`` may carry ``hidden_until: YYYY-MM-DD`` (a calendar date in local time). While
today is before that date the task is hidden: the views that offer tasks to pick (``list``, ``review``, the
dashboard export, ``answers refresh`` and ``status``) leave it out by default. On and after the date it is
visible. A missing or null field is visible. Hidden is independent of ``status``; the task keeps its status, and
every by-name verb (show, work, update, close, ...) still works on it.

A malformed value is NOT hidden: ``validate`` reports it, and the task stays findable meanwhile.

Nothing here reads the clock except :func:`local_today`, which only the entry points call; the rule takes
``today`` from its caller so tests inject it.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Mapping

HIDDEN_UNTIL_KEY = "hidden_until"
# The ``update --hidden-until`` value that removes the field.
CLEAR_VALUE = "none"
FORMAT_HINT = "YYYY-MM-DD"
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class HiddenUntilError(ValueError):
    """A ``hidden_until`` value that is not a calendar date."""


def local_today() -> datetime.date:
    """Today's date in local time (the one wall-clock read of this module)."""
    return datetime.date.today()


def parse_hidden_until(value: Any) -> datetime.date:
    """A ``hidden_until`` value as a date: an ISO ``YYYY-MM-DD`` string, or the ``datetime.date`` YAML loads
    from an unquoted ``2026-11-30``. Anything else raises :class:`HiddenUntilError`."""
    if isinstance(value, datetime.datetime):  # a datetime is a date subclass; it carries a time, so it is no date
        raise HiddenUntilError(f"{value!r} is a date and time, not a {FORMAT_HINT} date")
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and _ISO_DATE.match(value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    raise HiddenUntilError(f"{value!r} is not a calendar date; use {FORMAT_HINT}")


def hidden_until_of(task: Mapping[str, Any] | None) -> datetime.date | None:
    """The task's ``hidden_until`` as a date; None when absent or null. Raises on a malformed value."""
    value = None if task is None else task.get(HIDDEN_UNTIL_KEY)
    return None if value is None else parse_hidden_until(value)


def is_hidden(task: Mapping[str, Any] | None, today: datetime.date) -> bool:
    """Whether the task (its ``task.yaml`` ``task:`` mapping) is hidden on ``today``: ``today`` is before
    ``hidden_until``. Absent, null and malformed values are not hidden."""
    try:
        until = hidden_until_of(task)
    except HiddenUntilError:
        return False
    return until is not None and today < until


def display_value(task: Mapping[str, Any] | None) -> str | None:
    """The stored ``hidden_until`` as the ``YYYY-MM-DD`` text list, show and export print; None when absent.
    A malformed value is returned as written, so it stays visible to the reader."""
    value = None if task is None else task.get(HIDDEN_UNTIL_KEY)
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime) else str(value)
