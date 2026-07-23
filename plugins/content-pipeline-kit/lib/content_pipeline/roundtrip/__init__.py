"""roundtrip -- default human-in-the-loop round-trip abstraction.

``questions`` runs the machine-asks -> human-answers -> answers-re-enter-
as-context loop. ``returns`` runs the paired export-for-review /
intake-corrections closed loop. Both systems this plugin unifies converge on
needing this; shipping it as a default component closes a loop one of them
currently leaves open rather than requiring every consumer to reinvent it.

Dependency contract: ``roundtrip`` may import ``store`` and stdlib only.

Deviations from the source systems
----------------------------------

1. **Answer preservation reuses ``store.attributed``, not a bespoke merge.**
   ``questions.merge_questions`` delegates to
   ``store.attributed.merge_preserved_fields`` with a ``CollectionMerge`` that
   carries human answers forward by id AND retains an answered question dropped
   from a regenerated set (an orphaned answer is authored work) -- so the
   do-no-harm answer rule lives once, shared with the store, rather than the
   source ``conversation_file`` loop's hand-wired copy.
2. **The workbook format is pluggable.** ``returns.export_for_review`` /
   ``intake_corrections`` take ``serialize`` / ``parse`` callables, so the xlsx
   specifics stay project-side; the generic part is "snapshot to review rows"
   and "ingest only the rows a human corrected, as ``human``-attributed
   ``Correction`` values." This module stays store-shape-agnostic -- it emits
   ``Correction`` values and lets the caller land them on its store's human
   slice.
"""
