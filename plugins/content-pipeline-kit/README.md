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

`call_llm` charges each live response against an optional `CostBudget`.
`response_cost` decides the amount, in this order:

1. A cache hit costs zero on the current run. A cached response keeps its
   reported-cost provenance for audit, but the spend happened on the original
   call.
2. A live response carrying an authoritative reported cost
   (`reported_cost_usd` plus `reported_cost_source`, supplied by
   llm-scripting-kit) costs exactly that amount.
3. Otherwise the token counts are priced against the `pricing` table. A model
   missing from a supplied table raises `KeyError`.
4. With neither a reported cost nor a pricing table the cost is unknown:
   `response_cost` returns `None` (only when `pricing=None` is passed
   explicitly) and `call_llm` records no charge. Nothing is charged as zero.

Behavior to know: `call_llm` charges an authoritative reported cost even when
no `pricing` table is supplied. A caller that passes `cost_budget` without
`pricing` can therefore reach the budget cap when a trusted response reports
its cost. Without a reported cost or a pricing table the call stays unpriced.

The reported-cost pair is all-or-nothing. A present amount must be finite,
non-negative, and not a boolean; anything else is treated as unknown. Against
an llm-scripting-kit that lacks the fields, responses carry no reported cost and
are priced from the `pricing` table (step 3). Exception (transport error)
charges are always estimator-based and need a pricing table.

## Durable waits

A unit of a tracked run can ask a person a typed question and wait for the
answer without holding a claim, while the rest of the run continues. The
question, the answer and the outcome are rows in the execution store
(`content_pipeline.execution`), and a per-request policy decides whether a
rejection or an expiry stops the unit (`stop`, the default) or returns it to
`pending` (`release`) so its next attempt can branch on the outcome.

It is opt-in per unit. Calling `store.request_interrupt`, or raising
`InterruptRequested` from an inline `generate`, is the whole opt-in: there is no
flag or config key. A pipeline that does neither runs as before, and
`content_pipeline.roundtrip` is unchanged, so the questions and returns a
pipeline already uses keep working beside it.

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
