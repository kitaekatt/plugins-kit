"""audit -- audit framework, runtime-shared classifiers.

``auditor`` classifies every output against policy, brief, and ground truth
using the SAME classifiers the runtime uses to generate and validate --
so the audit cannot disagree with the runtime's own judgment of a candidate.
``reasoning_chain`` is a per-item sidecar recording why a candidate was
selected. ``report`` produces coverage views and impact-per-LLM-dollar
rollups over an audited batch.

Dependency contract: ``audit`` may import ``store`` / ``freshness`` /
``validate``; the runtime classifiers are INJECTED as callables (an
``AuditSpec``), so this package never reaches across into ``deliver`` or a
pipeline. ``reasoning_chain`` records an ``llm.submit_validated`` result by
duck-typing it (reads ``responses`` / ``rejections`` / ``payload``) rather than
importing ``llm`` -- keeping the audit within its import budget.

Deviations from the source systems
----------------------------------

1. **The findings taxonomy is generalized to six neutral kinds.**
   ``auditor.FindingKind`` unifies the first-pass auditor's buckets:
   ``FALSE_NEGATIVE`` (policy=apply, store has a value, no machine output),
   ``FALSE_POSITIVE`` (policy=exclude, machine output present),
   ``STORE_OUTPUT_MISMATCH`` (marked output differs from the store's value),
   ``MISSING_VALUE`` (policy=apply, no output, store has no usable value),
   ``ORPHANED_OUTPUT`` (marked output, no backing store record), ``STALE_REF``
   (an index reference that no longer resolves).
2. **The audit reuses the runtime's own verdicts.** ``AuditSpec`` carries the
   SAME policy / marker / projection callables the pipeline and ``deliver`` use,
   so a finding is by construction the runtime's judgment, not a second rule set
   that could disagree.
3. **Coverage reuses ``freshness.bucket_counts``.** ``report.coverage_report``
   folds freshness states through the same predicate the "needs generation" set
   uses, so buckets and regen set cannot drift. ``cost_effectiveness_report``
   takes a plain cost mapping (a consumer builds it from ``llm.platform``'s
   accounting) so ``audit`` stays LLM-free.
"""
