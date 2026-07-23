"""Coverage views and impact-per-LLM-dollar rollups.

Aggregates per-entity audit results (from ``auditor``) into batch-level
views: coverage (how much of the corpus was audited, and at what
freshness), and cost-effectiveness (impact per LLM dollar spent, using the
cost accounting ``llm.platform`` tracks).
"""


def coverage_report(audit_results: list) -> dict:
    """Aggregate per-entity audit results into a coverage view."""
    raise NotImplementedError


def cost_effectiveness_report(audit_results: list, cost_ledger: dict) -> dict:
    """Aggregate audit results and cost ledger into an impact-per-dollar rollup."""
    raise NotImplementedError
