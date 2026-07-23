"""roundtrip -- default human-in-the-loop round-trip abstraction.

``questions`` runs the machine-asks -> human-answers -> answers-re-enter-
as-context loop. ``returns`` runs the paired export-for-review /
intake-corrections closed loop. Both systems this plugin unifies converge on
needing this; shipping it as a default component closes a loop one of them
currently leaves open rather than requiring every consumer to reinvent it.
"""
