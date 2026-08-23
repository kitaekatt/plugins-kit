# Binding to content-pipeline-kit

`dispatch/` is the only package that imports content-pipeline-kit. This
document records WHAT it binds to and, more usefully, what it deliberately
does not -- so the next person to open `dispatch/` does not re-litigate it or
reimplement a concern that already ships.

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

Deliberately NOT adopted:

- `freshness.classify`'s two-tier predicate wholesale. A comment IS an
  explicit staleness signal; hashing the whole corpus to rediscover it is
  work for nothing.
- `roundtrip.questions` in its shipped direction. It is machine-asks-human;
  comments are human-asks-machine. Both directions do exist in the finished
  system -- a worker that runs `fail` on a genuine ambiguity becomes an
  anchored question awaiting a ruling -- but that is a new path built on
  the comment model, not a reuse of `roundtrip`.

Two extension points carry the new work: the planner implements CPK's
`pipeline.workunit.WorkUnitStrategy` protocol (`.units(store) ->
list[WorkUnit]`) but agentically, where the shipped `FlatChunkStrategy` and
`GraphWalkStrategy` are mechanical; and "do it inline vs spawn an agent" is
selecting `execution/drivers/inline.py` or `claude_bg.py` per unit. Both
lanes already ship.

## The two extension points

These carry the new work; everything else above is adoption.

- **The planner implements `pipeline.workunit.WorkUnitStrategy`**
  (`.units(store) -> list[WorkUnit]`) but AGENTICALLY, where the shipped
  `FlatChunkStrategy` and `GraphWalkStrategy` are mechanical. Comment count is
  not unit count.
- **"Do it inline vs spawn an agent" is driver selection** --
  `execution/drivers/inline.py` or `claude_bg.py`, per unit. Both lanes
  already ship; nothing new is needed to choose between them.

## Do not build a quota predictor

content-pipeline-kit rules this explicitly and it carries here: the
guaranteed-correct path is the reactive halt. Code that parses a rate-limit
snapshot to decide whether to dispatch is the named anti-pattern, not an
optimization.
