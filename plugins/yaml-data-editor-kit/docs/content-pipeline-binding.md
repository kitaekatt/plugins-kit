# Binding to content-pipeline-kit

`dispatch/` is the only package that imports content-pipeline-kit. This
document records WHAT it binds to and, more usefully, what it deliberately
does not -- so a reader of `dispatch/` does not re-litigate it or reimplement
a concern that already ships.

The rule behind every line here: if a concern is about running MANY calls --
cache, retry, cost, budget, leases, fencing -- it exists already. Bind to it.

Adopt directly, do not reimplement:

- `store.attributed` -- `human > machine > sourced` precedence. A human
  edit in the editor writes the `human` slice; an agent writes `machine`.
  This is the whole answer to "the human keeps authoring while work is in
  flight", and it is structural rather than a runtime check.
- `validate.contract` -- schema conformance as `Validator`s, one list
  shared by the in-loop generation site and the post-hoc audit.
- `execution/*` -- the claim/fence/lease/reclaim machinery, the protocol
  mount (`read`/`submit`/`fail`), `RunAdapter`, `WorkerCommand`.
- `deliver.inplace` + `vcs.git_vcs` -- ownership markers, first-class
  revert, exact-path never-wildcard adds.
- `freshness.hashing.content_hash` -- on the ANCHORED SLICE ONLY, to detect
  that data moved under a comment while its unit was in flight.

Submit acceptance and apply rejection are separate axes, and `execution/*`
models them separately. `ACCEPTED` records that adjudication accepted a
worker's text; `APPLY_REJECTED` records that delivery was refused without a
side effect. A refused apply therefore keeps the unit `ACCEPTED` -- staleness
at apply time changes whether the result is still applicable, not whether it
was accepted -- so a stale unit settles without stranding the healthy units
finalized alongside it.

## Dispatch implementation boundary

Implemented behavior in `dispatch/`:

- `request.py` loads the corpus path, comment-store path, optional selection,
  driver name, and run directory from a YAML file with diagnostics.
- `planner.py` implements `WorkUnitStrategy`. `MechanicalCommentPlanner`
  groups comments on one anchored record, gives each document-level comment its
  own unit, and puts the anchored slice, comment text, and CPK content hash in
  the payload. `AgenticCommentPlanner` and its default `CommentPlanner`
  submit a canonical grouping request through CPK when a backend is
  configured. An unusable response returns the complete mechanical plan.
- `protocol.py` wraps the CPK `fail` handler. A terminal
  `question/1:` failure names one target and becomes a guarded open question
  record. The durable failed attempt is the handoff between the worker and
  question materialization. Ordinary failures retain CPK behavior.
- `run.py` executes the `inline` lane through CPK's execution store and inline
  driver, then writes accepted text to `machine` in a file-backed attributed
  store. A hash mismatch at result time is a stale rejection. Human slices
  are preserved and never written by this lane.
- `background.py` provides a staged `claude_bg` lifecycle: prepare writes an
  immutable plan and runtime stores, run supervises a background wave, status
  reads durable state, and finalize applies accepted results. The worker mount
  in `worker_mount.py` authenticates the saved plan and exposes CPK's
  `read`/`submit`/`fail` protocol. `dispatch()` remains synchronous and
  inline-only; a background request raises `BackgroundStagesRequiredError`.
- Dispatch workers read the corpus and comment store and write only runtime
  execution artifacts. Finalize writes only the `machine` layer in the
  attributed store. It preserves `human`, `sourced`, and metadata.

Deferred behavior:

None.

Deliberately NOT adopted:

- `freshness.classify`'s two-tier predicate wholesale. A comment IS an
  explicit staleness signal; hashing the whole corpus to rediscover it is
  work for nothing.
- `roundtrip.questions` in its shipped direction. It is machine-asks-human;
  comments are human-asks-machine. The dispatch system supports both
  directions: a worker that runs `fail` on a genuine ambiguity becomes an
  anchored question awaiting a ruling. That path uses the comment model,
  not `roundtrip`.

Two extension points define the dispatch seam: the planner implements CPK's
`pipeline.workunit.WorkUnitStrategy` protocol (`.units(store) ->
list[WorkUnit]`) with mechanical grouping and optional agentic grouping.
"Do it inline vs spawn an agent" is the driver choice. The inline lane is
synchronous. The `claude_bg` lane uses the staged background API and the
consumer-owned worker protocol mount.

## The two extension points

These define the dispatch seam; everything else above is adoption.

- **The planner implements `pipeline.workunit.WorkUnitStrategy`**
  (`.units(store) -> list[WorkUnit]`) with mechanical record grouping and
  optional agentic grouping. Comment count is not unit count.
- **"Do it inline vs spawn an agent" is driver selection** --
  `execution/drivers/inline.py` handles synchronous work per unit.
  `background.py` handles `claude_bg` through prepare, run, status, and
  finalize stages. `worker_mount.py` supplies the consumer-owned protocol
  mount.

## Do not build a quota predictor

content-pipeline-kit rules this explicitly and it carries here: the
guaranteed-correct path is the reactive halt. Code that parses a rate-limit
snapshot to decide whether to dispatch is the named anti-pattern, not an
optimization.
