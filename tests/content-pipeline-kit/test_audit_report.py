"""Tests for content_pipeline.audit.report.

Pins the coverage rollup (reusing freshness.bucket_counts, so buckets and the
regen set cannot drift), the finding rollup, impact ranking, and the impact-
per-dollar cost-effectiveness view (no division-by-zero fabrication).
"""

from content_pipeline.audit.auditor import Finding, FindingKind
from content_pipeline.audit.report import (
    coverage_report,
    cost_effectiveness_report,
    finding_rollup,
    impact_ranking,
)
from content_pipeline.freshness.classify import FreshnessState


def test_coverage_report_buckets_and_fractions():
    states = [
        FreshnessState.FRESH,
        FreshnessState.FRESH,
        FreshnessState.MISSING,
        FreshnessState.STALE,
    ]
    report = coverage_report(states)
    assert report["total"] == 4
    assert report["buckets"]["fresh"] == 2
    assert report["needs_generation"] == 2  # MISSING + STALE
    assert report["fresh_fraction"] == 0.5


def test_coverage_report_empty_corpus():
    report = coverage_report([])
    assert report["total"] == 0
    assert report["fresh_fraction"] == 0.0


def test_coverage_report_includes_findings_when_supplied():
    findings = [Finding(FindingKind.FALSE_POSITIVE, "e1")]
    report = coverage_report([FreshnessState.FRESH], findings=findings)
    assert report["findings"]["false_positive"] == 1


def test_finding_rollup_counts_by_kind():
    findings = [
        Finding(FindingKind.FALSE_NEGATIVE, "e1"),
        Finding(FindingKind.FALSE_NEGATIVE, "e2"),
        Finding(FindingKind.ORPHANED_OUTPUT, "e3"),
    ]
    rollup = finding_rollup(findings)
    assert rollup["total"] == 3
    assert rollup["by_kind"]["false_negative"] == 2
    assert rollup["by_kind"]["orphaned_output"] == 1


def test_impact_ranking_orders_by_weighted_impact():
    findings = [
        Finding(FindingKind.FALSE_POSITIVE, "e1"),
        Finding(FindingKind.FALSE_POSITIVE, "e1"),
        Finding(FindingKind.MISSING_VALUE, "e2"),
    ]
    ranked = impact_ranking(findings)
    assert ranked[0]["entity_id"] == "e1"  # 2 findings > 1
    assert ranked[0]["impact"] == 2.0


def test_impact_ranking_respects_weights():
    findings = [
        Finding(FindingKind.MISSING_VALUE, "e1"),  # weight 1
        Finding(FindingKind.FALSE_POSITIVE, "e2"),  # weight 5
    ]
    ranked = impact_ranking(
        findings, weights={FindingKind.FALSE_POSITIVE: 5.0}
    )
    assert ranked[0]["entity_id"] == "e2"  # heavier kind ranks first


def test_cost_effectiveness_report():
    findings = [Finding(FindingKind.MISSING_VALUE, "e1")]
    report = cost_effectiveness_report(
        findings, {"total": 10.0}, resolved=100
    )
    assert report["total_cost"] == 10.0
    assert report["findings"] == 1
    assert report["cost_per_resolved"] == 0.1
    assert report["findings_per_dollar"] == 0.1


def test_cost_effectiveness_no_division_by_zero():
    report = cost_effectiveness_report([], {"total": 0.0}, resolved=0)
    assert report["cost_per_resolved"] is None
    assert report["findings_per_dollar"] is None
