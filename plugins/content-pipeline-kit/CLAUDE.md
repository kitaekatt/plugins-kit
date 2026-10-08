# CLAUDE.md -- content-pipeline-kit plugin

Guidance for an AI agent working in this plugin.
`skills/content-pipeline-domain/SKILL.md` owns the vocabulary and the
abstraction map; `references/design-discipline.md` beside it carries the
philosophy behind the opt-in guardrails.

content-pipeline-kit answers the questions a RUN of many LLM calls raises:
which units need doing, which are stale and need regenerating, which are
already done and must be left alone, whether a result is valid, what the run
cost, and where the output goes. Those mechanics are the same whatever the
content is; the domain judgement stays in the consuming project.

A consumer imports the library and drives it from its own entry point. The
package ships no console script: `cli.scaffold.dispatch` is a dispatch helper a
consumer wires its own commands onto, and the orchestration loop, config
loading, prompt content, field names, the pricing table, and persistence are
all the consumer's. Say that before describing any subpackage -- a consumer
that expects a runnable tool has already misunderstood the layer.

`content_pipeline.__version__` (`lib/content_pipeline/__init__.py`) must equal the `plugin.json` and `pyproject.toml` versions on every bump; `tests/content-pipeline-kit/test_version_matches_plugin_json.py` enforces it.

## The dependency on llm-scripting-kit is one-way by design

`content_pipeline` depends on `llm_scripting_kit`; the reverse edge does not
exist and should not be added. llm-scripting-kit owns making ONE call correctly
-- endpoint, model, key, transport, halt taxonomy. This plugin owns what only
exists across a RUN of calls: the content-addressed response cache, run-level
retry, cost accounting, the token/cost budget guard, and the validate-until-valid
submission loop. Each of those is a policy a pipeline can answer and a transport
cannot. They live in `lib/content_pipeline/llm/platform.py`, whose module
docstring states the same split; `llm/backends.py` holds the thin adapters over
the shared lib and imports it lazily, so `MockBackend` needs neither the shared
lib nor an SDK.

So the lower layer not implementing these is the boundary working, not a gap a
consumer is left to fill. A consumer wanting pipeline-grade behaviour depends on
this plugin and gets them; a consumer wanting one call depends on
llm-scripting-kit alone and carries none of the weight. When adding a concern,
keep it on this side of the line unless it is a fact about making a call rather
than about running many.

`openai` is declared in this plugin's own `pyproject.toml` even though the SDK is
reached through the shared lib -- shared libs share source, not dependencies. See
`plugins/CLAUDE.md`, "Why shared libs rather than published packages".

## What consumers adopt, and what they rebuild

Two independent production consumers, building unrelated content on this
library without coordinating, stopped adopting at the same line. Both took the
HORIZONTAL concerns and both wrote their own VERTICAL ones. When two
unconnected consumers stop at the same line, treat that line as a real seam.
That is the durable part; the per-module detail below is a dated observation,
not a standing fact.

Observed 2026-08-11 by reading two consumer projects outside this repo, so
nothing here can verify or refresh it. Adopted from the library:
`freshness.hashing`, `freshness.classify`, `store.attributed`,
`store.intermediary`, `validate.contract`, `llm.platform`, `llm.backends`.
Written by each project instead: the orchestration loop, the candidate store,
the human round-trip, and the delivery choreography for its VCS.

Read it as strong evidence about the seam and weak evidence about any one
module. `pipeline.convergence_loop` was the sharp case at that date: one
consumer's hand-rolled sequencer had the same stage order and the same stall
window as the library's and still did not import it. The reasons that consumer
recorded were that the library signature could not carry its bookkeeping (a
resume token threaded through every stage, per-stage thread pools with
main-thread-only store mutation, a progressive save, a verdict read from
on-disk metrics), and that the port was incremental and converted the leaf
subsystems first. Nobody had reported trying the library loop and finding it
wanting.

Two consequences for how to answer a consumer. A consumer starting clean should
try `pipeline.*` before writing its own; a consumer porting a working loop
should expect to keep it. And when a consumer keeps its own, say the cost out
loud: at that same date one consumer was maintaining its convergence loop in
three variants while a generic implementation sat unused here. The seam is not
a reason to stop asking whether a vertical module has earned reuse.

## Cost accounting prefers reported cost

`response_cost` (in `llm/platform.py`) returns zero for a cache hit, then the
authoritative `reported_cost_usd` of a live response, then the pricing-table
estimate, and `None` (unknown, never zero) when there is neither and
`pricing=None` was passed. `call_llm` therefore charges a reported cost with no
pricing table; the budget cap is reachable that way.
`backends._from_completion_response` reads the pair with `getattr` (older
llm-scripting-kit) and drops an invalid or one-sided pair. `ResponseCache`
stores both fields as provenance; entries written without them still load.
Exception charges stay estimator-based. User docs: `README.md`.

## `looks_like_network_path` exists twice, and the parity test is not where you would look

`llm/spend_ledger.py` carries a behavioural duplicate of
`execution/store.py::looks_like_network_path`, because `llm/` may not import
`execution/`. The duplicate's docstring says so and flags that a parity test exists without naming it; the
ORIGINAL in `execution/store.py` says nothing, and the parity test lives in
`tests/content-pipeline-kit/test_llm_spend_ledger.py`. Under this repo's
targeted-test-run rule an agent editing `execution/store.py` would run
`test_execution_store.py` and never see the parity failure. So: when you change
either copy, run `test_llm_spend_ledger.py::TestNetworkPathParity` as well. The
cheaper fix is a back-pointer comment on the `execution/store.py` copy, which no
unit owns.

## Backend selection

Routing is per pipeline when the consumer passes the declaration explicitly:
`route(models=[...])`, `routed_model(requested, models=[...])` and
`declared_model_names(models)` take an ordered list of llm-scripting-kit ids.
The list wins over the environment, so two pipelines in one process can route
differently; the declaration memo is keyed on the names. An empty explicit list
raises `ConfigurationError`. `CONTENT_PIPELINE_LLM_MODELS` (comma list) is the
process-wide default only when `models` is `None`; a supplied `mock` still wins
unconditionally.

With the environment default, selection is process-global: one
`CONTENT_PIPELINE_LLM_MODELS` declaration picks the entry, and its model, for
the whole process. One exception:
when the declaration resolves to the `openrouter` entry, `route()` returns a
caller-supplied `openrouter=` instance instead of building a fresh one, and
`routed_model()` returns a non-empty caller-requested model instead of the
entry's model. An empty request, or an entry naming another transport, still runs
the declaration's entry and model. The consequence
to state to a consumer: two pipelines that need different backends cannot share
a process unless each passes `models=`, and with the environment default nothing at a call site signals that one of them got the other's
backend, so a changed environment variable can move output quality with no
local signal.

Only `CONTENT_PIPELINE_LLM_MODELS` routes. `_BACKEND`/`_MODEL`/`_ENDPOINT` raise `ConfigurationError` when set without it (`llm/backends.py::_refuse_removed_routing_env`; details in `skills/content-pipeline-domain/references/building-a-pipeline.md`).

A supplied `mock` wins unconditionally in `route()`, checked before
the declaration is even read: `route(mock=FakeBackend())` always returns the
supplied instance, regardless of `CONTENT_PIPELINE_LLM_MODELS`. A test needs
no environment setup to keep a routed call off a live transport.

## The adaptive gate

An overloaded endpoint is a halt of kind `HALT_BACKPRESSURE` (`"backpressure"`)
carrying `retry_after_s` (`None` without a hint), distinct from quota, credit
and auth halts. `call_llm` does not retry it; wrap calls in
`llm.gate.AdaptiveGate` (or `call_llm_gated`), one gate per run. On the halt
the gate releases the slot, halves the concurrency limit (floor 1, once per
wave), waits `retry_after_s` (else exponential backoff with equal jitter),
and retries. Every `recover_after` consecutive admitted calls raise the limit
by one, back toward `jobs`. A call still refused after `give_up_s` raises
`EndpointBusyError`, and so does every later call at once. Quota, credit,
auth, unreachable and all other errors pass through. `clock`, `sleep` and
`jitter` are injectable. The retry-after hint falls back through
`llm_scripting_kit.completion.halt.classify_backpressure`: the edge is DEGRADE
(`platform._backpressure_classifier`), since without it the halt is still true
and only the wait is less informed; an absent lib and a lib older than
llm-scripting-kit 0.61.0 each raise a distinct `RuntimeWarning` naming the
install or update command, once per state. Pinned by
`test_backpressure_classifier_seam.py`.

## Plugin-opinion razor (hardcoded numbers)

| Opinion | Default | Seam | Verdict |
| --- | --- | --- | --- |
| `BASE_SECONDS` | 30 | `AdaptiveGate(base_s=)` | constructor parameter with documented default; the seam |
| `CAP_SECONDS` | 600 | `AdaptiveGate(cap_s=)` | same |
| `GIVE_UP_SECONDS` | 3600 | `AdaptiveGate(give_up_s=)` | same |
| `RECOVER_AFTER` | 5 | `AdaptiveGate(recover_after=)` | same |
| `DEFAULT_CIRCUIT_LIMIT` | 2 | `DeadlineCircuit(limit=)` | same |

A constructor parameter with a documented default is the library's seam; no
config-file key is added, because a consumer calls these constructors itself.

## State files: which home

`FailureCache(path)` and `DeadlineNotes(path)` take the path from the consumer;
nothing derives from the module's location, and `_atomic_json` writes its
temporary file in the target's directory. `FailureCache` is project-durable
(`.plugin-data`): a cross-run record about the consuming project whose loss
costs a repeated model call. `DeadlineNotes` is project-ephemeral
(`.local-data`): it only orders work. Neither is user-scoped.

## Parallel graph, deadlines and the failure cache

`scheduler`, `deadlines` and `failure_cache` import only the standard library.

**Parallel dependency graph.** `parallel_graph.ParallelGraphStrategy(dependencies,
capacity=1, payload_of=None, deferred=frozenset())` names each unit's direct
dependencies. `wave.ready_wave` releases every `pending` unit whose dependencies
are settled (`skipped`, or `accepted` and applied), up to `capacity` in flight,
`deferred` units last. A unit that is `failed`, `operator_rejected`,
`interrupt_expired`, or accepted with its apply refused gates its transitive
dependents only; `wave.gated_units` lists them. The graph is validated at build
(unknown, duplicate and self dependencies, cycles), and registered unit ids must
equal its node ids (`GraphRegistrationMismatchError`); register them in
`strategy.node_ids` order. `max_wave_size` and `reclaim_at` apply as for the
other shapes; `prepare_run` and `workerpack.build_wave_args` narrow to the same
wave. A drain loop calls `finalize_run` between waves and is done when every
unit `unfinished_units` returns is in `gated_units`. Without a durable store,
`scheduler.Scheduler(nodes, capacity=)` holds the same rule in memory
(`admit_ready`, `complete`, `fail`, `requeue` with a quiet period) and
`scheduler.run_graph(scheduler, execute)` runs it on a thread pool. An executor
raising `NodeFailed` fails its node and gates its dependents; any other
exception stops admission, waits for running nodes, and is re-raised.

**Per-unit deadline and run-wide circuit.** `deadlines.UnitBudget(seconds)` is
one unit's wall-time budget, shared by its first call and repairs.
`call_within_budget(budget, timeout_s, call, is_timeout=..., circuit=...,
uncharged=...)` runs one admitted call with `min(timeout_s, remaining)`, charges
its wall time, turns a budget-imposed timeout into `DeadlineExpired`, and
refuses before dispatch when the budget is spent or the circuit is open
(`CircuitOpen`). Call it inside the gate, `gate.run(lambda:
call_within_budget(...))`, with `uncharged` accepting the backpressure halt so
queueing and backoff are not charged. `DeadlineCircuit(limit=2)` opens after
`limit` consecutive units END on a deadline (`record_outcome`, once per unit)
and stays open. `DeadlineNotes(path)` counts expiries per attempt key; a note
orders work (mark the unit `deferred`) and never suppresses it.

**Negative failure cache.** `FailureCache(path)` records a deterministic
failure as `FailureRecord(stage, unit_id, attempt_key, error_fingerprint,
fingerprint_version, code, summary)`. `lookup(stage, unit_id, attempt_key)` hits
only for the same attempt key; `clear(stage, unit_id)` on success.
`error_fingerprint(code, details)` digests a normalized error. Never record a
deadline, open circuit, transport error or backpressure. A corrupt cache or
notes file reads as empty and `report` says why.

## The parallel graph is a third strategy, not a mode of the graph walk

`execution.parallel_graph.ParallelGraphStrategy` is deliberately NOT a
`GraphWalkStrategy`, so `wave.is_graph_strategy` stays false for it and every
sequential-graph guard (`UnsafeGraphParallelismError`, `prepare_run`'s order
and unapplied-predecessor refusals, `graph_block_reason`) keeps its meaning. A
code path that selects units outside `ready_wave` must test
`wave.is_dependency_ordered`, not `is_graph_strategy`, or it bypasses the
parallel graph's dependency and capacity rule; `controller._wave_with_reclaims`
and `workerpack.build_wave_args` do. The in-memory `execution.scheduler` and the
durable wave share one rule (`validate_graph`, `dependents_closure`); change
them together. `scheduler`, `deadlines`, `failure_cache` and `_atomic_json` are
stdlib-only by test (`test_parallel_graph.py`), because a consumer's portable
run code imports them without the rest of the package. They were contributed
from a consumer's tested modules; field names are generalized (`stage`,
`unit_id`, `subject`), so that consumer's existing state files read as empty
(with a `report`) the first time it switches. User docs: `README.md`.

## The durable wait is opt-in and lives in the execution store

A durable wait (`store.request_interrupt`, `InterruptRequested` in the inline
lane) is a waiting state of a unit in the execution store, requested per unit
and per call. It adds no flag, config key or manifest entry, and a consumer that
never asks has no waiting units. It leaves `content_pipeline.roundtrip` and a
consumer's own round trips alone: they are a different shape (a question about
an entity that re-enters as context between runs), and the wait does not
replace or wrap them. Keep it that way when editing either. The wait reaches
`bootstrap_lib` and `llm_scripting_kit` as libraries, inside its verbs, never at
import. Say the lane scope when describing it: the inline lane and a consumer's
own loop can ask, the background lane refuses under an open dispatch, and the
workflow lane has no request surface (do not describe it as refusing). User
docs: `README.md` and step 9 of `building-a-pipeline.md`.
