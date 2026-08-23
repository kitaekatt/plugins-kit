"""Errors and diagnostics produced by the dialect loader and validator.

Two failure channels, deliberately separate:

- ``ProfileError`` -- the PROFILE is not a legal dialect document. Raised, not
  collected, because nothing downstream can be validated against a broken
  declaration.
- ``Diagnostic`` -- a DATA record does not satisfy a legal profile. Collected,
  because a corpus wants every problem in one pass, and every diagnostic names
  the file, the record and the field so a person can act on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ERROR = "error"
ADVISORY = "advisory"


class ProfileError(Exception):
    """The profile itself is malformed: a dialect document cannot be read."""

    def __init__(self, message: str, document: Path | None = None) -> None:
        self.document = document
        self.raw_message = message
        if document is not None:
            message = "{0}: {1}".format(document.name, message)
        super().__init__(message)


@dataclass(frozen=True)
class Diagnostic:
    """One actionable finding about a data corpus.

    ``file``/``record``/``field`` are the address. ``record`` is ``None`` only
    for a ``single``-layout document (which has no identity) or a
    document-level finding, and ``field`` is ``None`` only for a record-level
    finding.
    """

    message: str
    file: str
    record: str | None = None
    field: str | None = None
    severity: str = ERROR

    @property
    def is_error(self) -> bool:
        return self.severity == ERROR

    def __str__(self) -> str:
        parts = ["{0}: file '{1}'".format(self.severity, self.file)]
        if self.record is not None:
            parts.append("record '{0}'".format(self.record))
        if self.field is not None:
            parts.append("field '{0}'".format(self.field))
        return ": ".join(parts) + ": " + self.message


def errors_only(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """The subset that fails a corpus; advisories are reported, not fatal."""
    return [d for d in diagnostics if d.is_error]
