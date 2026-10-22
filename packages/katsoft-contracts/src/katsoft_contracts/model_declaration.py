"""Parse and structurally validate a model declaration.

A model declaration answers "which model(s) may do this unit of work" as a
list of registry ids. The format is specified in the plugin-dev skill's
``references/model-declaration.md``; this module is its one validator.

Scope is SHAPE only:

* a bare string reads as a one-element list, and every result is a list;
* each entry is a non-blank string, normalized by stripping surrounding
  whitespace;
* a literally-empty list is an error;
* the same id twice is an error, because a repeated entry is a fact about the
  declaration itself, whatever the ids turn out to mean.

``parse`` accepts only string entries. ``parse_entries`` additionally accepts
``{id, effort}`` mappings, for a site that delivers a per-entry effort; a site
that cannot deliver one keeps calling ``parse``, so an effort it would drop is
refused rather than silently ignored.

Whether an id resolves to a registry entry, and whether the caller can route
it, is decided at runtime where the declaration is dispatched. This module
therefore reads no registry, opens no file, and never imports
llm-scripting-kit. It is stdlib-only because ``bootstrap_lib`` is linked into
venvs that carry no third-party dependency.

The one id set it owns is ``CORE_IDS``: the Claude model ids the harness itself
defines, valid on every installation and routable without llm-scripting-kit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator


# The harness-defined ids. Every plugin can route these through the harness
# alone (Agent tool, Workflow agent(), background task, `claude -p`). The set
# is closed and case-sensitive: "Opus" or "sonnett" is an ordinary id, which a
# runtime resolver treats like any other unknown id.
CORE_IDS = frozenset({"fable", "opus", "sonnet", "haiku"})

# The reasoning-effort menu an entry may state, lowest first. Closed, because
# effort is a vocabulary the dispatchers define and a typo must not pass.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

# The fields an entry mapping may carry.
ENTRY_FIELDS = frozenset({"id", "effort"})


class DeclarationError(ValueError):
    """A declaration is not a structurally valid list of ids.

    ``index`` is the offending entry's position in the declared list, or
    ``None`` when the fault is the value as a whole (wrong type, empty list).
    """

    def __init__(self, message: str, *, index: int | None = None) -> None:
        super().__init__(message)
        self.index = index


@dataclass(frozen=True)
class Declaration:
    """A structurally valid declaration: ordered, non-empty, duplicate-free."""

    ids: tuple[str, ...]

    def __iter__(self) -> Iterator[str]:
        return iter(self.ids)

    def __len__(self) -> int:
        return len(self.ids)

    @property
    def core_ids(self) -> tuple[str, ...]:
        """The declared ids that are core ids, in declaration order."""
        return tuple(entry for entry in self.ids if entry in CORE_IDS)


@dataclass(frozen=True)
class Entry:
    """One declared model: its id and, when stated, its effort."""

    id: str
    effort: str | None = None


def is_core_id(value: str) -> bool:
    """Return whether ``value`` (whitespace-stripped) is a core id."""
    return isinstance(value, str) and value.strip() in CORE_IDS


def _entry(value: Any, index: int | None) -> str:
    """Return one normalized entry, or raise naming its position."""
    if not isinstance(value, str):
        raise DeclarationError(
            f"each entry must be a non-empty string, got {type(value).__name__}",
            index=index,
        )
    text = value.strip()
    if not text:
        raise DeclarationError("each entry must be a non-empty string", index=index)
    return text


def parse(value: Any) -> list[str]:
    """Return ``value`` as a normalized list of ids.

    Accepts a string (a one-element declaration) or a list or tuple of
    strings. Raises ``DeclarationError`` for any other type, a blank entry, a
    literally-empty list, or a repeated id.
    """
    if isinstance(value, str):
        return [_entry(value, None)]
    if not isinstance(value, (list, tuple)):
        raise DeclarationError(
            f"must be a string or a list of strings, got {type(value).__name__}"
        )
    if not value:
        raise DeclarationError(
            "must not be an empty list: a declaration names at least one id"
        )

    ids: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        entry = _entry(raw, index)
        if entry in seen:
            raise DeclarationError(f"duplicate id {entry!r}", index=index)
        seen.add(entry)
        ids.append(entry)
    return ids


def _mapping_entry(value: dict, index: int | None) -> Entry:
    """Return one ``{id, effort}`` entry, or raise naming its position."""
    unknown = sorted(str(key) for key in value if key not in ENTRY_FIELDS)
    if unknown:
        raise DeclarationError(
            f"unknown entry field(s) {', '.join(unknown)}; an entry is {{id, effort}}",
            index=index,
        )
    if "id" not in value:
        raise DeclarationError("an entry mapping must state an id", index=index)
    entry_id = _entry(value["id"], index)
    effort = value.get("effort")
    if effort is not None and effort not in EFFORT_LEVELS:
        levels = ", ".join(EFFORT_LEVELS)
        raise DeclarationError(
            f"unknown effort {effort!r} for {entry_id!r}; known levels: {levels}",
            index=index,
        )
    return Entry(entry_id, effort)


def parse_entries(value: Any) -> list[Entry]:
    """Return ``value`` as a normalized list of ``Entry``.

    Accepts everything ``parse`` accepts, plus ``{id, effort}`` mappings in
    place of any string, and a single mapping as a one-element declaration.
    ``effort`` is optional and, when present, one of ``EFFORT_LEVELS``.
    Raises ``DeclarationError`` for the faults ``parse`` raises, an unknown
    entry field, a mapping without an id, or an unknown effort.
    """
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, (list, tuple)):
        if not value:
            raise DeclarationError(
                "must not be an empty list: a declaration names at least one id"
            )
        entries: list[Entry] = []
        seen: set[str] = set()
        for index, raw in enumerate(value):
            if isinstance(raw, dict):
                entry = _mapping_entry(raw, index)
            else:
                entry = Entry(_entry(raw, index))
            if entry.id in seen:
                raise DeclarationError(f"duplicate id {entry.id!r}", index=index)
            seen.add(entry.id)
            entries.append(entry)
        return entries
    return [Entry(entry_id) for entry_id in parse(value)]


def validate(value: Any) -> Declaration:
    """Structurally validate ``value`` and return it as a ``Declaration``."""
    return Declaration(tuple(parse(value)))


__all__ = [
    "CORE_IDS",
    "Declaration",
    "DeclarationError",
    "EFFORT_LEVELS",
    "ENTRY_FIELDS",
    "Entry",
    "is_core_id",
    "parse",
    "parse_entries",
    "validate",
]
