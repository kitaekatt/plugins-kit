# content-pipeline-kit

content-pipeline-kit answers the questions a run of many LLM calls raises: which
units need doing or regenerating, whether a result is valid, what the run cost,
and where the output goes. It works across a run and depends on
llm-scripting-kit, which owns making one call correctly (endpoint, model, key,
transport, halt taxonomy); the reverse edge does not exist. A consumer imports
the library and drives it from its own entry point.

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
