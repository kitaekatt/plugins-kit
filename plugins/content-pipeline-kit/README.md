# content-pipeline-kit

content-pipeline-kit answers the questions a run of many LLM calls raises: which
units need doing or regenerating, whether a result is valid, what the run cost,
and where the output goes. It works across a run and depends on
llm-scripting-kit, which owns making one call correctly (endpoint, model, key,
transport, halt taxonomy); the reverse edge does not exist. A consumer imports
the library and drives it from its own entry point.

## Structured output

A caller declares the shape it needs with an
`llm_scripting_kit.completion.OutputContract` and passes it to
`submit_validated(output_contract=...)` or `BackendOptions.output_contract`.
Structural validity (the output conforms to the schema) is judged first, by
llm-scripting-kit; domain validity (the content is acceptable) stays with the
`validate.contract` validators, which run only on a structurally valid object.
A schema failure is one HARD `schema_violation` Rejection carrying the contract
identity, the schema errors, and the raw output, and it feeds the normal retry
loop. Under a `text-only` contract the report is recorded and `parse_fn` still
parses. The contract is part of the cache key; a cache hit needs a report for
the same contract with a success disposition.

`audit.reasoning_chain.record_submission` records, per attempt, the contract
`id`, `schema_version`, `schema_digest`, `policy`, `delivery` (`native`,
`prompt`, `none` for a text-only contract that delivers no schema, or `unreported`
when no seam report exists), and `disposition`. It never records the schema body.

The codex adapter requires OpenAI strict-mode schemas (`additionalProperties:
false` and a full `required` list at every object level); prompt-delivered
adapters accept the package's schema subset. The contract path needs
llm-scripting-kit 0.56.0 or later and refuses without it.

## Cost and budget

`call_llm` charges each live response against an optional `CostBudget`: a cache hit
costs zero, an authoritative reported cost (`reported_cost_usd` plus
`reported_cost_source`) costs exactly that, otherwise tokens are priced against
`pricing`, and with neither the cost is unknown and nothing is charged. The full
order and the validity rules for the reported pair:
`skills/content-pipeline-domain/references/building-a-pipeline.md`, step 6.

## The cross-process spend cap (opt-in)

`CostBudget` is an in-process float accumulator, so processes that each hold one
do not share a cap. `content_pipeline.llm.spend_ledger` is a USD cap that every
process opening the same SQLite file shares. It is stdlib only, opt-in, and the
library picks no path -- the consumer names the file. Pass the ledger as
`call_llm(spend=ledger)` and each provider attempt reserves against the cap
before it pays and settles after; `ledger.halt()` stops further admission for
every process, and `cli.budget.spend_stop` turns a cap or halt verdict into a
clean partial `BudgetStop`. The ledger cannot see a `call_llm` made without
`spend=`, so reserve-before-pay is a property of the call site. Reservation
sizing, the halt lifecycle, orphan reclaim and its overshoot bound, and the
`status()` fields:
`skills/content-pipeline-domain/references/building-a-pipeline.md`, "The
cross-process spend cap".

## Routing a call (per pipeline)

Pass `models=[...]` (ordered llm-scripting-kit ids) to `route`,
`routed_model` and `declared_model_names` to route each pipeline on its own;
`CONTENT_PIPELINE_LLM_MODELS` is only the default when `models` is `None`.
Rules: `CLAUDE.md`, "Backend selection".

## Waiting out endpoint overload (the adaptive gate)

Wrap calls in `content_pipeline.llm.gate.AdaptiveGate` (or
`call_llm_gated(gate, backend, ...)`), one gate per run. An overload halt is
waited out, not failed. Rules, defaults and the give-up behavior: `CLAUDE.md`,
"The adaptive gate".

## Parallel dependency graphs, deadlines and the failure cache

Three stdlib-only run-layer pieces in `content_pipeline.execution`, each
adoptable on its own: `ParallelGraphStrategy` / `scheduler` (dependency graph
with capacity), `deadlines` (per-unit budget, run-wide circuit, deadline notes)
and `failure_cache` (negative cache of deterministic failures). The consumer
supplies the paths of the notes and cache files. Rules: `CLAUDE.md`, "Parallel
graph, deadlines and the failure cache".

## Durable waits

A unit of a tracked run can ask a person a typed question and wait for the
answer without holding a claim, while the rest of the run continues. The
question, the answer and the outcome are rows in the execution store
(`content_pipeline.execution`), and a per-request policy decides whether a
rejection or an expiry stops the unit (`stop`, the default) or returns it to
`pending` (`release`) so its next attempt can branch on the outcome.

It is opt-in per unit. Calling `store.request_interrupt`, or raising
`InterruptRequested` from an inline `generate`, is the whole opt-in: there is no
flag or config key. A pipeline that does neither records no interrupt rows, and
none of its units ever enters the waiting state. `content_pipeline.roundtrip`
does not use the execution store's interrupt code, so its questions and
returns work the same with or without durable waits and can be used beside
them.

Lane scope:

- The inline lane can ask, and so can a consumer's own loop over the store.
- The background lane refuses a wait under its open dispatch
  (`WaitUnderDispatchError`).
- The workflow lane has no supported request surface: its worker protocol has no
  wait verb, and nothing in the library handles a wait requested through a verb a
  consumer mounts itself.

The requesting verbs use `bootstrap_lib` (bootstrap 0.137.0 or later) and
`llm_scripting_kit` (0.56.0 or later), probed inside the verb. Without them the
verb raises `InterruptSupportError` before it writes. How to ask, answer,
expire and drain a run with a waiting unit:
`skills/content-pipeline-domain/references/building-a-pipeline.md`, "Durable
waits (opt-in)".

## Run provenance and stage replay

`content_pipeline.provenance` records a run and replays one stage of it. It is
opt-in and stdlib only; every write lands under a directory the caller names.

- `record.start_run` writes `run.json` (params, hashed inputs, module versions)
  and returns a `RunRecorder`. Call `finish` on success; leaving the `with`
  block on an exception records `error`, and a clean exit leaves the status
  `running` until `finish` is called.
- `call_audit.CallAuditor` is an `on_attempt` observer for `call_llm` and
  `submit_validated`; it writes per-call prompt, response, and metadata files.
- `snapshot.StageSnapshotter` and `snapshot.EventLog` are `LoopObserver`s for
  `pipeline.convergence_loop.run`: they save the store around each stage and log
  each loop event. A `STAGE_FINISHED` `LoopEvent` carries the stage's return
  value in `result`. `cell_policy.measure_from(..., detail_of=fn)` attaches
  `fn(store)` to each `Round.detail`.
- `replay.replay_stage` re-runs one stage from a snapshot with an edited input
  and writes a new bundle whose record links back to the source.

`content_pipeline.__version__` equals the plugin manifest version.

## Checking a consumer against the public surface

`scripts/check_consumer_contract.py` reports which names a consumer uses are
deprecated or removed in `contract/public-surface.json`, and exits 1 when a
removed name is used. Invocation and detection limits:
`skills/content-pipeline-domain/references/building-a-pipeline.md`, intro.
