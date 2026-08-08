"""Classify every output vs. policy + store + ground truth, runtime-shared classifiers.

The audit reuses the EXACT classifier callables the pipeline runtime uses
during generation and delivery, rather than a separate audit-only rule set --
so an audit finding can never simply be the audit and the runtime disagreeing
about the same rule. Those classifiers are INJECTED (per the dependency
contract: ``audit`` takes callables, it does not reach across into ``deliver``
or a pipeline). Concretely a caller wires the same functions it uses at
runtime: the policy/excluded classifier that ``pipeline`` gates on, the marker-
ownership classifier from ``deliver.inplace``, and the projected pick from
``store.projection``.

The generalized findings taxonomy:

- ``FALSE_NEGATIVE`` -- policy says this entity SHOULD carry machine output and
  the store has a value for it, but the delivered output is absent / not
  machine-marked. Work that should exist does not.
- ``FALSE_POSITIVE`` -- policy says this entity should be EXCLUDED from
  generation, yet a machine-marked output exists. Work that should not exist
  does.
- ``STORE_OUTPUT_MISMATCH`` -- a machine-marked output exists but its delivered
  value differs from the store's projected value. The next delivery would
  silently change it (or a human hand-edited the output without updating the
  store).
- ``MISSING_VALUE`` -- policy says apply and no machine output exists, and the
  store has NO usable value yet (either no record, or a record with no pick).
  Distinct from FALSE_NEGATIVE, where the store DOES have a value to deliver.
- ``ORPHANED_OUTPUT`` -- a machine-marked output exists but the store has no
  record backing it (the store was deleted / never generated; the marker lies).
- ``STALE_REF`` -- an index/reference points at a source that no longer
  resolves (a corpus reference to a missing/empty file).

An entity is duck-typed; the :class:`AuditSpec` callables read whatever shape
the caller stores. ``excluded``/``applies`` is the caller's domain verdict, not
this module's -- exactly the ``freshness.classify`` discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


class FindingKind(str, Enum):
    """The generalized audit finding taxonomy."""

    FALSE_NEGATIVE = "false_negative"
    FALSE_POSITIVE = "false_positive"
    STORE_OUTPUT_MISMATCH = "store_output_mismatch"
    MISSING_VALUE = "missing_value"
    ORPHANED_OUTPUT = "orphaned_output"
    STALE_REF = "stale_ref"


# The three policy verdicts a runtime classifier returns for an entity.
POLICY_APPLY = "apply"  # this entity should carry machine output
POLICY_EXCLUDE = "exclude"  # this entity must NOT carry machine output
POLICY_UNKNOWN = "unknown"  # no expectation either way


@dataclass(frozen=True)
class Finding:
    """One audit finding against one entity."""

    kind: FindingKind
    entity_id: str
    detail: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditSpec:
    """The runtime-shared classifier callables, injected.

    - ``policy`` -- ``entity -> str``: the SAME apply/exclude/unknown verdict
      the pipeline gates on (:data:`POLICY_APPLY` / :data:`POLICY_EXCLUDE` /
      :data:`POLICY_UNKNOWN`).
    - ``output_marked`` -- ``entity -> bool``: is a machine-marked output
      present on the delivered (ground-truth) record? The SAME marker classifier
      ``deliver.inplace`` uses.
    - ``store_has_record`` -- ``entity -> bool``: does the store hold a record
      for this entity at all?
    - ``store_value`` -- ``entity -> value``: the store's projected value
      (``None``/empty == no usable pick). The SAME projection delivery reads.
    - ``output_value`` -- ``entity -> value``: the delivered value on the
      ground-truth record (used only for the mismatch comparison).
    """

    policy: Callable[[Any], str]
    output_marked: Callable[[Any], bool]
    store_has_record: Callable[[Any], bool]
    store_value: Callable[[Any], Any]
    output_value: Callable[[Any], Any]


def _has_value(value: Any) -> bool:
    """True when a projected/delivered value counts as present."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def audit_entity(entity: Any, entity_id: str, spec: AuditSpec) -> List[Finding]:
    """Classify one entity into zero or more :class:`Finding`.

    Applies the taxonomy in the module docstring using only ``spec``'s injected
    runtime classifiers -- so the audit's verdict is, by construction, the
    runtime's own verdict. Returns a list (usually zero or one finding; the
    apply-branch can only produce one).
    """
    verdict = spec.policy(entity)
    marked = spec.output_marked(entity)

    if verdict == POLICY_APPLY:
        if not marked:
            if _has_value(spec.store_value(entity)):
                return [
                    Finding(
                        FindingKind.FALSE_NEGATIVE,
                        entity_id,
                        detail="store has a value but no machine output was delivered",
                    )
                ]
            reason = (
                "store record present but carries no value"
                if spec.store_has_record(entity)
                else "no store record and no output"
            )
            return [Finding(FindingKind.MISSING_VALUE, entity_id, detail=reason)]
        # marked machine output present
        if not spec.store_has_record(entity):
            return [
                Finding(
                    FindingKind.ORPHANED_OUTPUT,
                    entity_id,
                    detail="machine-marked output with no backing store record",
                )
            ]
        expected = spec.store_value(entity)
        got = spec.output_value(entity)
        if _has_value(expected) and got != expected:
            return [
                Finding(
                    FindingKind.STORE_OUTPUT_MISMATCH,
                    entity_id,
                    detail=f"output={got!r} store={expected!r}",
                    context={"output": got, "store": expected},
                )
            ]
        return []

    if verdict == POLICY_EXCLUDE:
        if marked:
            return [
                Finding(
                    FindingKind.FALSE_POSITIVE,
                    entity_id,
                    detail="machine output on an excluded entity",
                )
            ]
        return []

    # POLICY_UNKNOWN: no expectation either way.
    return []


def audit_corpus(
    entities: Iterable[Any],
    spec: AuditSpec,
    *,
    entity_id: Callable[[Any], str],
) -> List[Finding]:
    """Audit every entity, concatenating findings in iteration order."""
    findings: List[Finding] = []
    for entity in entities:
        findings.extend(audit_entity(entity, entity_id(entity), spec))
    return findings


def audit_references(
    refs: Iterable[Any],
    *,
    ref_id: Callable[[Any], str],
    resolves: Callable[[Any], bool],
    reason: str = "reference does not resolve",
) -> List[Finding]:
    """Emit a :data:`FindingKind.STALE_REF` for every reference that fails to resolve.

    ``resolves`` is the caller's "does this index entry point at a real,
    non-empty source?" predicate. A reference that returns False becomes a
    STALE_REF finding -- the corpus-integrity half of the audit, kept separate
    from per-entity classification because a stale ref has no delivered output
    to classify.
    """
    findings: List[Finding] = []
    for ref in refs:
        if not resolves(ref):
            findings.append(Finding(FindingKind.STALE_REF, ref_id(ref), detail=reason))
    return findings


def counts_by_kind(findings: Sequence[Finding]) -> Dict[FindingKind, int]:
    """Tally findings per kind (every kind present, zero when unseen)."""
    out: Dict[FindingKind, int] = {kind: 0 for kind in FindingKind}
    for finding in findings:
        out[finding.kind] += 1
    return out


def group_by_kind(findings: Sequence[Finding]) -> Dict[FindingKind, List[Finding]]:
    """Group findings by kind (only non-empty kinds are keyed)."""
    out: Dict[FindingKind, List[Finding]] = {}
    for finding in findings:
        out.setdefault(finding.kind, []).append(finding)
    return out


__all__ = [
    "FindingKind",
    "POLICY_APPLY",
    "POLICY_EXCLUDE",
    "POLICY_UNKNOWN",
    "Finding",
    "AuditSpec",
    "audit_entity",
    "audit_corpus",
    "audit_references",
    "counts_by_kind",
    "group_by_kind",
]
