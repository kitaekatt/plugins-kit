"""audit -- audit framework, runtime-shared classifiers.

``auditor`` classifies every output against policy, brief, and ground truth
using the SAME classifiers the runtime uses to generate and validate --
so the audit cannot disagree with the runtime's own judgment of a candidate.
``reasoning_chain`` is a per-item sidecar recording why a candidate was
selected. ``report`` produces coverage views and impact-per-LLM-dollar
rollups over an audited batch.
"""
