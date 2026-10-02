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

## The cross-process spend cap (opt-in)

`CostBudget` is an in-process float accumulator. `content_pipeline.llm.spend_ledger`
is the other thing: a USD cap that every process opening the same SQLite file
shares. It is stdlib only (`sqlite3` plus `decimal`), opt-in, and the library
picks no path -- the consumer names the file.

```python
from content_pipeline.llm.spend_ledger import create_ledger, open_ledger

ledger = create_ledger(run_dir / "spend.sqlite", cap_usd=5.00, run_id=run_id)
# every other process on the same run:
ledger = open_ledger(run_dir / "spend.sqlite")
```

`create_ledger` creates the file exclusively and writes the cap into a pinned
policy row; a second process racing it gets `FileExistsError` and calls
`open_ledger`. The cap is read-only after that -- there is no setter, and it is
re-validated inside every transaction, so a run cannot widen its own cap
mid-flight. A different cap means a different file.
`spend_ledger_from_env()` opens the ledger named by
`CONTENT_PIPELINE_SPEND_LEDGER`, and returns `None` when that variable is unset.

**Reserve and settle happen per PROVIDER ATTEMPT, not per `call_llm`.** Pass the
ledger as `call_llm(spend=ledger)` and every iteration of the retry loop takes
its own reservation immediately before the provider call and settles it in a
`finally`, so exactly one settle runs on every exit of every attempt. That
matters because `call_llm(retries=N)` bills up to `N + 1` times: one reservation
per invocation would let a four-attempt call bill four times against one
admission, and the cap would be wrong by a factor of four.
`submit_validated` forwards `spend=` verbatim, so its loop admits up to
`max_attempts * (retries + 1)` attempts against the same cap.

Sizing: left to itself the reservation is priced from the request plus the full
`options.max_tokens`, so `spend=` with neither `pricing=` nor
`spend_reserve_usd=` is a `ValueError` raised before any provider call --
reserving 0 would be admitted by any cap and then billed. `spend_reserve_usd=`
sets the amount outright. A cache hit returns before the attempt loop: it
reserves nothing and writes no row.

When an attempt's cost cannot be read, `settle(None)` HOLDS the reservation at
its reserved amount as `unknown`. There is no release-on-failure rule: a failed
attempt may well have billed, so the cap keeps counting it. A cost above its
reservation records `overbilled`, keeps the excess visible as leaked money,
sets the halt row (`halt_on_overbilled`, default true) and raises.

**Halt.** `ledger.halt(reason, detail)` stops every further admission durably,
for every process. Each `reserve` reads the halt row inside its own transaction,
so at most one in-flight attempt per process completes after the halt commits.
`check_halted()` is a read-only probe a consumer loop calls between units to
stop sooner; it makes the stop earlier, it is not what enforces it.
`resume()` clears the halt, but refuses an `overbilled` one with a `ValueError`
unless called as `resume(force=True)` -- that halt means recorded spend exceeded
its reservation, so the cap arithmetic is already known to have understated real
spend; a forced resume is written to `halt_history`.

**Status.** `ledger.status()` returns a `SpendStatus`: `run_id`,
`cap_usd`, `settled_usd`, `reserved_usd`, `unknown_usd`, `leaked_usd`,
`reclaimed_usd`, `written_off_usd`, `outstanding_usd`, `remaining_usd`, `reservations_open`,
`calls_settled`, `requests`, `halted`, `halt_reason`, `halt_detail`, `as_of`.
`outstanding_usd` is the admission quantity (`settled + reserved + unknown +
leaked`); `remaining_usd` is `cap - outstanding` and may be negative -- clamp it
for display, never for arithmetic.

**Two further `BudgetStop.reason` values.** `cli.budget` defines `SPEND_CAP`
(`"spend_cap"`) and `SPEND_HALT` (`"spend_halt"`), so a driver that switches on
`BudgetStop.reason` has two tokens to handle beside the `PipelineHaltError`
kinds. `spend_stop` is a CONTEXT MANAGER, not a function
taking a probe -- wrap the work that may hit the ledger:

```python
from content_pipeline.cli.budget import BudgetStop, spend_stop

try:
    for unit_id in units:
        with spend_stop(done, remaining, unit_id=unit_id):
            call_llm(..., spend=ledger, pricing=pricing)
        done.append(unit_id)
except BudgetStop as stop:
    report_partial(stop.reason, stop.done, stop.remaining)
```

It translates exactly two exceptions -- `SpendCapExceeded` into
`BudgetStop(SPEND_CAP, ...)` and `SpendLedgerHalted` into
`BudgetStop(SPEND_HALT, ...)` -- copying `done`, `remaining` and `unit_id` onto
the stop. Everything else propagates unchanged on purpose, because as a budget
verdict each would be a lie: `sqlite3.OperationalError` (the busy timeout was
exhausted, so the ledger admitted nothing and failed closed), `ValueError`,
`StaleReservationError`, `LedgerStateInvalid`, `LedgerIdentityChanged` and
`KeyError`.

### The enforcement gap: the ledger cannot enforce reserve-before-pay

`call_llm(spend=None)` bills the provider with no ledger row, and nothing inside
the ledger can see it. `call_llm` also never reads
`spend_ledger_from_env()` for itself -- a cap must be passed in deliberately,
never materialize mid-run out of an inherited environment variable. So
reserve-before-pay is a property of the CALL SITE, not of the ledger: a caller
that omits `spend=` spends outside the cap while the ledger's own numbers stay
internally consistent and wrong about the run. The mitigation is conventional,
not mechanical -- a consumer that wants it mechanical wraps `call_llm` in its
own helper that supplies `spend=` and calls only that.

### Orphan reclaim, and the hole in it

A process killed between reserve and settle leaves an `open` row that nothing
will resolve, and its headroom would consume the cap for the life of the file.
`reclaim_orphans()` resolves such a row to `reclaimed`, bumps its `generation`,
and retains it permanently so a late settle can be told apart from a settle
against an id that never existed.

The hole, stated plainly: an expired lease does not prove the attempt is dead,
so a sweep may free headroom an attempt still goes on to spend. **The overshoot
bound of one pass is the SUM of `reserved` over ALL expired-but-live rows that
pass reclaimed, not one row** -- reported as `ReclaimReport.freed_nano`, and at
most concurrency times the largest reservation. `reclaim_batch_limit` is what
keeps the bound tight: its default of 1 holds the bound at one row per pass
while still converging, because every later sweep takes the next expired row.
Setting it to 0 means no limit, which widens the bound back to the whole
expired set.

A reservation made with no `ttl_s` stores a NULL lease, which no sweep ever
reclaims at any batch limit: no deadline is known, so no elapsed time is
evidence of anything. `call_llm` sets a lease only when `options.timeout_s` is
set, at `2 * timeout_s + 60`. `orphan_reclaim` defaults to `"manual"`, the
shipped default: sweeping happens only when the consumer calls
`reclaim_orphans()`. Opening with `orphan_reclaim="lease"` also sweeps inside
every `reserve`, in the same transaction as the admission. No renewal thread,
poller or signal ships; a consumer with its own watchdog calls `renew`.

`written_off_usd` names the crash case this reclaim exists for: a row whose
headroom was freed and whose settle never arrived. That is money the ledger
stopped charging against the cap, and the field is there so it is not unnamed.
It drops out once a late settle arrives, which is recorded as a leak instead and
counts in `leaked_usd`.

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

`contract/public-surface.json` lists each public name and whether it is
deprecated or removed. To see which names a consumer uses are affected:

```bash
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CONTENT_PIPELINE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/check_consumer_contract.py" <consumer source path>
```

The script exits 1 when a removed name is used.
