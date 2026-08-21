---
_schema_version: 1
name: workflow-pipeline
author: christina
skill-type: technique-skill
description: Use when orchestrating a content-pipeline run via the Workflow tool. Do NOT use for a worker's own unit (execute-work-unit) or driver (background-pipeline).
---

# Workflow Pipeline

The orchestration procedure for driving a content-pipeline run's worker units
through the native Workflow tool, with `plugins/content-pipeline-kit/workflows/run-ready-wave.js`
as the compiled script and the Workflow tool's own `agent()`/`parallel()`
primitives providing concurrency. There is no separate dispatcher process in
this lane: the Workflow tool cannot be invoked from inside a subagent, so the
top-level Claude Code session that is reading this skill IS the orchestrator,
for every wave, start to finish.

This is the workflow lane's counterpart to `background-pipeline`'s four
stages, but the shapes differ enough that they are not interchangeable
procedures for the same loop -- read `references/workflow-lane.md` (in
`content-pipeline-domain`) for why: the agent claims its own unit rather than
the orchestrator claiming on its behalf, there is no renewer and no
dispatcher lease, and re-entry after an interruption is always a fresh
invocation rather than a resume.

## Preconditions -- verify all four before spending a single agent

1. **The run exists and is prepared.** Build the wave through the consumer's
   own protocol mount (the `prepare` verb of `execution.protocol.build_handlers`,
   or the consumer's own call into `execution.controller.prepare_run`) exactly
   as `background-pipeline` stage 1 describes. This skill does not second-guess
   that policy.
2. **The pack builder's lease refusal has already passed.** `build_wave_args`
   (`content_pipeline.execution.workerpack`) refuses to emit a wave when the
   mount declares neither a finite-positive explicit `lease_seconds` nor a
   positive `resolve_expected_unit_seconds` for every selected unit. If it
   raises, that is the wave telling you it cannot be sized safely -- fix the
   mount's declaration (see `session-recipients.md`'s `expected_unit_seconds`
   guidance) rather than working around the refusal.
3. **The launching session's own allowlist covers every per-unit string the
   wave is about to use.** Each unit in the pack carries four command strings
   (`claimCmd`, `readCmd`, `submitCmd`, `failCmd`) and three Write targets
   (the answer file, the submit envelope, the fail envelope). Every one of
   them must already be pre-authorized in the launching session's own
   tool-permission settings before the Workflow tool runs, because a workflow
   agent inherits that allowlist and has no auto permission mode of its own.
   **The background lane's clean unattended run is not evidence this is
   covered** -- that lane's dispatcher launches separate `claude --bg`
   sessions with their own permission surface; a workflow agent runs inside
   this session's grant instead, so an allowlist gap here blocks the agent
   directly rather than producing a `blocked` dispatch report you can
   diagnose afterward.
4. **The environment preflight has returned `{"ok": true}`.** Run the first
   unit's `readCmd` verbatim through the mount, by hand, before invoking the
   Workflow tool at all. This mirrors the `read`/`submit`/`fail` environment
   check every worker verb performs (`session-recipients.md`'s `environment`
   field), but the workflow lane has no `compose_worker_environment` step of
   its own to catch a mismatch early -- `_require_compatible_run` fires on
   every worker verb including the wave's own `claim` calls, so without this
   preflight a mismatch surfaces only after agents have already been spawned
   and paid for. A `WorkerEnvironmentMismatchError` (or an `{"ok": false}`
   reply naming it) stops the invocation here; do not proceed to wave
   assembly.

## Assembling the wave

Call `build_wave_args` (`content_pipeline.execution.workerpack`) with the
store, run id, adapter, `WorkerCommand`, and `max_agents`. It performs
reap-first candidate selection (expired-lease reclaim ahead of pending-unit
selection), mints a fresh Python-side `batchId`, pre-writes the `read` and
worker-scoped `claim` envelopes, and returns the JSON-serializable `args`
object `run-ready-wave.js` expects. This skill does not restate that
function's logic or hand-compose a wave pack -- point at
`build_wave_args` and use its output as-is. If it returns no units (an empty
wave), that means the run has nothing left to assemble a wave for; go
straight to finalize rather than invoking the Workflow tool with an empty
`units` array (the script throws on that input rather than returning a
silent no-op aggregate).

## Invocation

Invoke the native Workflow tool, from the top-level session only, against
`plugins/content-pipeline-kit/workflows/run-ready-wave.js` with the `args`
object `build_wave_args` produced. Nothing about this step happens inside a
subagent -- the Workflow tool is unavailable there, which is the same reason
this skill exists as a top-level-session procedure rather than a background
one.

## After the Workflow tool returns

The returned aggregate (`counts`, `units`, `advisory: true`) is
**advisory, not authoritative.** It is built from what each agent
self-reported, and a self-report can lie or drift from what the store
actually recorded -- see `references/workflow-lane.md` for the channels that
stay open even after the per-agent schema is value-bounded. Reconcile against
the run's real state through the mount (the `status` verb, or the
consumer's own equivalent) before treating any unit as settled; never act on
the returned counts alone.

Once reconciled, finalize exactly as `background-pipeline` stage 4 does --
`execution.controller.finalize_run(store, run_id, adapter)` -- so that every
accepted unit's output actually lands. A halted or partially-settled wave
means going around again: re-run the preconditions above, call
`build_wave_args` again (it reaps first, so an abandoned unit from the
previous wave is a normal candidate), and invoke a fresh wave. There is no
lighter-weight "continue where it left off" step in this lane.

**Never ingest unit content into the orchestrating session.** Nothing in
this procedure needs a unit's prompt, a worker's answer text, or a
validator's feedback read into the top-level session's own context. The
`args` object going in and the aggregate coming out are both built to
exclude it, the same invariant `background-pipeline` upholds for its own
`DispatchReport`/`compute_status` digests. If a debugging need seems to
require reading unit content, that belongs in a consumer-side tool reading
the store directly and offline -- not in this invocation loop.

## Native Workflow resume is not supported for pipeline continuation

Do not call `resumeFromRunId` to continue a content-pipeline run, even
though the Workflow tool exposes it generically. `resumeFromRunId` replays a
**cached agent self-report**, not a live re-execution: the underlying
contract states plainly that resume "caches metadata, not files" and returns
a cached node result without restoring anything the node wrote. That is
fatal to this lane specifically, because the store's authority can change
underneath a cached result in ways resume has no way to see:

- A cached `halted` outcome, replayed after the run's halt condition has
  since cleared, skips a claim that would now succeed.
- A cached `claim_unavailable` outcome, replayed after the lease that caused
  it has since expired, skips a claim that would now succeed.
- A cached `accepted` outcome can simply disagree with a store that later
  shows the same unit superseded by a different worker's fence.

Re-entry after any interruption -- a halt, a crash, an exhausted wave -- is
always a **fresh invocation**: a new `batchId` minted by `build_wave_args`,
a wave reassembled from current store state through that same reap-first
selection, and fresh worker ids so fencing distinguishes the new generation
of agents from whatever ran before. There is no supported shortcut around
that reassembly.
