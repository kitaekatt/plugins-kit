"""provenance -- run records, call audits, stage snapshots, and stage replay.

Opt-in components (CRP): nothing here is imported by the core pipeline, and
this package re-exports nothing. Import the submodule you need.

- ``record`` -- the run record (``RunRecorder`` / ``start_run``), the source
  links it hashes, prior-run rotation, and the file helpers shared by the
  other modules.
- ``call_audit`` -- ``CallAuditor``, an ``on_attempt`` observer that writes
  per-call audit files under a caller-supplied directory.
- ``snapshot`` -- ``StageSnapshotter`` and ``EventLog``: per-stage input
  snapshots and a loop-event log.
- ``replay`` -- ``replay_stage``: re-run one stage from its snapshot.

Stdlib only; every write lands under a directory the caller names.
"""
