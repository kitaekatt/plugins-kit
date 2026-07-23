"""Coverage views and impact-per-LLM-dollar rollups.

Aggregates per-entity audit findings (from ``auditor``) plus freshness states
(from ``freshness.classify``) into batch-level views: coverage (how much of the
corpus is fresh vs. needs work, and how many findings of each kind), and cost-
effectiveness (impact per LLM dollar spent, using a cost ledger the caller
carries -- ``llm.platform`` tracks it, but this module takes a plain mapping so
it never imports ``llm``).

Two-way reuse: :func:`coverage_report` folds an iterable of
``FreshnessState`` through ``freshness.classify.bucket_counts`` -- the SAME
predicate the "needs generation" set uses -- so the coverage buckets and the
regen set cannot disagree.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from content_pipeline.audit.auditor import Finding, FindingKind, counts_by_kind
from content_pipeline.freshness.classify import (
    FreshnessState,
    bucket_counts,
    needs_generation,
)


def coverage_report(
    states: Iterable[FreshnessState],
    findings: Sequence[Finding] = (),
) -> dict:
    """Aggregate freshness states (and optional findings) into a coverage view.

    Returns a dict carrying:

    - ``total`` -- number of entities classified.
    - ``buckets`` -- ``{state_value: count}`` over every
      :class:`~content_pipeline.freshness.classify.FreshnessState` (zero when
      unseen), via ``freshness.bucket_counts``.
    - ``needs_generation`` -- count of entities whose state calls for a
      (re)generation (``MISSING`` + ``STALE``), the work-remaining number.
    - ``fresh_fraction`` -- fraction whose state is ``FRESH`` (0.0 for an empty
      corpus).
    - ``findings`` -- ``{finding_kind: count}`` when ``findings`` is supplied
      (every kind present, zero when unseen).
    """
    states = list(states)
    counts = bucket_counts(states)
    total = len(states)
    fresh = counts[FreshnessState.FRESH]
    need = sum(1 for s in states if needs_generation(s))
    report: dict = {
        "total": total,
        "buckets": {state.value: counts[state] for state in FreshnessState},
        "needs_generation": need,
        "fresh_fraction": (fresh / total) if total else 0.0,
    }
    if findings:
        report["findings"] = {
            kind.value: n for kind, n in counts_by_kind(list(findings)).items()
        }
    return report


def finding_rollup(findings: Sequence[Finding]) -> dict:
    """Summarize findings: total plus a per-kind breakdown.

    ``by_kind`` carries every :class:`~content_pipeline.audit.auditor.
    FindingKind` (zero when unseen) so a report can index any bucket safely.
    """
    counts = counts_by_kind(list(findings))
    return {
        "total": len(findings),
        "by_kind": {kind.value: n for kind, n in counts.items()},
    }


def impact_ranking(
    findings: Sequence[Finding],
    *,
    weights: Optional[Mapping[FindingKind, float]] = None,
) -> List[dict]:
    """Rank entities by weighted finding impact, highest first.

    Each entity's impact is the sum of its findings' weights (default weight
    1.0 per finding; ``weights`` overrides per kind so a consumer can score a
    ``FALSE_POSITIVE`` heavier than a ``MISSING_VALUE``). Returns a list of
    ``{entity_id, impact, kinds}`` sorted by descending impact, ties broken by
    ``entity_id`` for byte-stable output.
    """
    weights = weights or {}
    per_entity: Dict[str, dict] = {}
    for finding in findings:
        weight = float(weights.get(finding.kind, 1.0))
        bucket = per_entity.setdefault(
            finding.entity_id, {"entity_id": finding.entity_id, "impact": 0.0, "kinds": []}
        )
        bucket["impact"] += weight
        bucket["kinds"].append(finding.kind.value)
    ranked = sorted(
        per_entity.values(), key=lambda e: (-e["impact"], e["entity_id"])
    )
    return ranked


def cost_effectiveness_report(
    findings: Sequence[Finding],
    cost_ledger: Mapping[str, Any],
    *,
    resolved: Optional[int] = None,
) -> dict:
    """Combine findings and a cost ledger into an impact-per-dollar rollup.

    ``cost_ledger`` is a plain mapping carrying at least a ``total`` USD spend
    (a consumer builds it from ``llm.platform``'s cost accounting; this module
    stays LLM-free). ``resolved`` is the count of units the spend actually
    produced good output for (defaults to "audited units minus outstanding
    findings" is NOT assumed -- the caller passes the real number). Returns:

    - ``total_cost`` -- the ledger's total spend.
    - ``findings`` -- outstanding finding count (lower is better output).
    - ``resolved`` -- units delivered (echoed).
    - ``cost_per_resolved`` -- ``total_cost / resolved`` (``None`` when
      ``resolved`` is 0 or unset -- no division-by-zero, no fabricated ratio).
    - ``findings_per_dollar`` -- ``findings / total_cost`` (``None`` when spend
      is 0).
    """
    total_cost = float(cost_ledger.get("total", 0.0) or 0.0)
    finding_count = len(findings)
    report: dict = {
        "total_cost": total_cost,
        "findings": finding_count,
        "resolved": resolved,
        "cost_per_resolved": None,
        "findings_per_dollar": None,
    }
    if resolved:
        report["cost_per_resolved"] = total_cost / resolved
    if total_cost:
        report["findings_per_dollar"] = finding_count / total_cost
    return report


__all__ = [
    "coverage_report",
    "finding_rollup",
    "impact_ranking",
    "cost_effectiveness_report",
]
