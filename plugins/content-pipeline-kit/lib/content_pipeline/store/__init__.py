"""store -- the canonical, attributed, freshness-anchored artifact.

Holds the four pieces of the canonical-store abstraction: field-level
attribution with human-always-wins precedence (``attributed``), the
single-intermediary hashing anchor that makes regeneration intelligent by
construction (``intermediary``), the candidate cell for the many-candidates
case -- active/shadow/retired lists plus cached grades and deterministic fact
riders (``candidate``), and the canonical-store-to-consumer-visible
projection (``projection``). Depends on nothing outside this package (REP:
``store`` is usable without ``llm``, without ``vcs``).
"""
