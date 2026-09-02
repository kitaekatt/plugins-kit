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

## Dispatch implementation boundary

Implemented behavior in `dispatch/`:

- `request.py` loads the corpus path, comment-store path, optional selection,
  driver name, and run directory from a YAML file with diagnostics.
- `planner.py` implements `WorkUnitStrategy`. It groups comments on one
  anchored record, gives each document-level comment its own unit, and puts
  the anchored slice, comment text, and CPK content hash in the payload.
- `run.py` executes the `inline` lane through CPK's execution store and inline
  driver, then writes accepted text to `machine` in a file-backed attributed
  store. A hash mismatch at result time is a stale rejection. Human slices
  are preserved and never written by this lane.

Deferred behavior:

- agentic grouping of comments into larger tasks;
- conversion of a worker ambiguity into a fail-to-anchored-question record;
- `claude_bg` execution and its background-session machinery. The request
  schema recognizes the driver so selection is explicit; dispatch raises a
  clear `NotImplementedError` for it.

Deliberately NOT adopted:

- `freshness.classify`'s two-tier predicate wholesale. A comment IS an
  explicit staleness signal; hashing the whole corpus to rediscover it is
  work for nothing.
- `roundtrip.questions` in its shipped direction. It is machine-asks-human;
  comments are human-asks-machine. Both directions do exist in the finished
  system -- a worker that runs `fail` on a genuine ambiguity becomes an
  anchored question awaiting a ruling -- but that is a path built on
  the comment model, not a reuse of `roundtrip`.

Two extension points define the dispatch seam: the planner implements CPK's
`pipeline.workunit.WorkUnitStrategy` protocol (`.units(store) ->
list[WorkUnit]`) with mechanical record grouping; agentic
grouping is deferred. "Do it inline vs spawn an agent" is the driver choice.
The `inline.py` lane is implemented; `claude_bg.py` selection is wired in the
request schema and rejected by the runner until its background-session
machinery is built.

## The two extension points

These define the dispatch seam; everything else above is adoption.

- **The planner implements `pipeline.workunit.WorkUnitStrategy`**
  (`.units(store) -> list[WorkUnit]`) with mechanical record grouping.
  Comment count is not unit count. Agentic grouping is deferred.
- **"Do it inline vs spawn an agent" is driver selection** --
  `execution/drivers/inline.py` is implemented per unit. The `claude_bg`
  choice is recognized and reports its deferred machinery through
  `NotImplementedError`.

## Do not build a quota predictor

content-pipeline-kit rules this explicitly and it carries here: the
guaranteed-correct path is the reactive halt. Code that parses a rate-limit
snapshot to decide whether to dispatch is the named anti-pattern, not an
optimization.
