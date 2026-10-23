"""Sticky unsupported-stub registry: exclude forever, no re-pay.

When a unit is determined to be structurally unsupported by a pipeline (not a
transient failure -- a genuine "this pipeline cannot handle this shape":
multi-speaker, no data, a policy-excluded owner), it is recorded in a sticky
registry and filtered out of all future runs. This stops a bulk run from
re-attempting (and re-paying an LLM call for) a unit that will never succeed --
the source system's sticky unsupported-stub marker.

Two surfaces, matching how the source systems actually stored the marker:

- :class:`UnsupportedRegistry` -- a standalone, persistable ``{id -> {reason,
  marked_at}}`` registry (round-trips through any YAML engine via
  :meth:`to_doc` / :meth:`from_doc`). Its :meth:`filter` drops marked units from
  a work list before any cost is paid.
- Record-embedded marker (:func:`stub_record` / :func:`is_unsupported_record`) --
  the source stored the marker INSIDE the unit's own store record (a brief's
  ``unsupported`` block), so the sticky state travels with the record and a
  designer clears it by deleting the record. :func:`stub_record` builds that
  minimal stub, preserving carry-forward fields from an existing record.

Deviation from the skeleton: the bare module-level ``mark_unsupported`` /
``is_unsupported`` are kept as thin wrappers over a process-default registry for
the skeleton signature, but the explicit :class:`UnsupportedRegistry` (passed by
the caller) is the real surface -- module-global mutable state is an anti-pattern
for a library and is offered only for the trivial single-process case.

Stdlib only; imports nothing else from ``content_pipeline``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

REASON_KEY = "reason"
MARKED_AT_KEY = "marked_at"
UNSUPPORTED_KEY = "unsupported"


def _now() -> str:
    """A stable, coarse ISO-ish timestamp for the marker (UTC seconds)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


@dataclass
class UnsupportedRegistry:
    """A persistable sticky-unsupported registry keyed by unit id."""

    entries: Dict[str, dict] = field(default_factory=dict)

    def mark(self, unit_id: str, reason: str) -> None:
        """Record ``unit_id`` as sticky-unsupported with ``reason``.

        Idempotent on the id: re-marking refreshes the reason and timestamp
        rather than erroring (a later run may rediscover the same failure with
        a clearer reason).
        """
        self.entries[unit_id] = {REASON_KEY: reason, MARKED_AT_KEY: _now()}

    def clear(self, unit_id: str) -> bool:
        """Remove ``unit_id`` from the registry; True if it was present.

        The designer-fixed-the-data path: once the underlying shape is fixed,
        clearing lets the unit re-enter the pipeline on the next run.
        """
        return self.entries.pop(unit_id, None) is not None

    def is_unsupported(self, unit_id: str) -> bool:
        """True when ``unit_id`` is marked sticky-unsupported."""
        return unit_id in self.entries

    def reason(self, unit_id: str) -> Optional[str]:
        """Return the recorded reason for ``unit_id``, or ``None``."""
        entry = self.entries.get(unit_id)
        return entry.get(REASON_KEY) if entry else None

    def filter(self, unit_ids: Iterable[str]) -> List[str]:
        """Return ``unit_ids`` with every sticky-unsupported id dropped.

        The no-re-pay gate: a bulk work list runs through this before any unit
        is processed, so a marked unit never reaches an LLM call again.
        """
        return [uid for uid in unit_ids if uid not in self.entries]

    def to_doc(self) -> dict:
        """Serialize to a plain dict document for the caller's YAML engine."""
        return {"unsupported": {uid: dict(e) for uid, e in sorted(self.entries.items())}}

    @classmethod
    def from_doc(cls, doc: Optional[Mapping[str, Any]]) -> "UnsupportedRegistry":
        """Build a registry from a plain dict document (``None`` -> empty)."""
        registry = cls()
        if not doc:
            return registry
        for uid, entry in (doc.get("unsupported") or {}).items():
            registry.entries[str(uid)] = dict(entry or {})
        return registry


def stub_record(
    unit_id: str,
    reason: str,
    *,
    base: Optional[Mapping[str, Any]] = None,
    carry_fields: Iterable[str] = (),
) -> dict:
    """Build a minimal store stub record carrying the sticky-unsupported marker.

    Generalizes the source ``_build_unsupported_brief``: the marker lives inside
    the unit's own record so the sticky state travels with it (a designer clears
    it by deleting the record). ``carry_fields`` names fields to preserve from an
    existing ``base`` record (an identity / skeleton field the stub should keep),
    so re-stubbing does not lose already-known metadata.
    """
    record: dict = {"id": unit_id}
    if base:
        for key in carry_fields:
            if base.get(key):
                record[key] = base[key]
    record[UNSUPPORTED_KEY] = {REASON_KEY: reason, MARKED_AT_KEY: _now()}
    return record


def is_unsupported_record(record: Optional[Mapping[str, Any]]) -> bool:
    """True when a store record carries the sticky-unsupported marker block."""
    return bool(record) and bool(record.get(UNSUPPORTED_KEY))


def record_reason(record: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return the reason from a record's marker block, or ``None``."""
    if not record:
        return None
    block = record.get(UNSUPPORTED_KEY)
    if isinstance(block, Mapping):
        return block.get(REASON_KEY)
    return None


# -- process-default registry (skeleton-signature compatibility) --------------

_DEFAULT_REGISTRY = UnsupportedRegistry()


def mark_unsupported(entity_id: str, reason: str) -> None:
    """Record ``entity_id`` unsupported in the process-default registry.

    Convenience for the trivial single-process case; prefer an explicit
    :class:`UnsupportedRegistry` in library code (module-global state does not
    round-trip and is not per-run).
    """
    _DEFAULT_REGISTRY.mark(entity_id, reason)


def is_unsupported(entity_id: str) -> bool:
    """True when ``entity_id`` is unsupported in the process-default registry."""
    return _DEFAULT_REGISTRY.is_unsupported(entity_id)


def default_registry() -> UnsupportedRegistry:
    """Return the process-default registry (mainly for test teardown)."""
    return _DEFAULT_REGISTRY


__all__ = [
    "REASON_KEY",
    "MARKED_AT_KEY",
    "UNSUPPORTED_KEY",
    "UnsupportedRegistry",
    "stub_record",
    "is_unsupported_record",
    "record_reason",
    "mark_unsupported",
    "is_unsupported",
    "default_registry",
]
