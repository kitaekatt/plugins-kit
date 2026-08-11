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

## Backend selection is process-wide

Backend selection is process-global (`CONTENT_PIPELINE_LLM_BACKEND`, with
`CONTENT_PIPELINE_LLM_MODEL` overriding the requested model). The consequence
to state to a consumer: two pipelines that need different backends cannot share
a process, and nothing at a call site signals that one of them got the other's
backend, so a changed environment variable can move output quality with no
local signal.

A supplied `mock` wins unconditionally in `route()`, checked before
`active_backend_name()` is even read -- a test never has to call
`set_active_backend("mock")` first to keep a routed call off a live transport.
(This was not always true: `route()` used to consult the active name first and
only honor a supplied instance for the name already active, so
`route(mock=FakeBackend())` with `CONTENT_PIPELINE_LLM_BACKEND` unset silently
reached a live `OpenRouterBackend`. Fixed; see
`tests/content-pipeline-kit/test_llm_backends.py::test_routing_injected_mock_wins_without_active_backend_set`.)
