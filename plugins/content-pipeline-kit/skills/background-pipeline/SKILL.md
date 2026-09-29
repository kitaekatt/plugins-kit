---
_schema_version: 1
name: background-pipeline
author: christina
skill-type: technique-skill
description: Use when orchestrating a content-pipeline run through Claude background sessions. Do NOT use for a worker's own unit (execute-work-unit) or the inline driver.
---

# Background Pipeline

The orchestration procedure for driving a content-pipeline run through Claude
background sessions (`claude --bg`), one Claude Code session at a time acting
as the dispatcher. It drives
`content_pipeline.execution.drivers.claude_bg.dispatch_wave` over a prepared
wave, and never runs unit content through its own context -- only ids,
outcomes, and status digests, the same invariant the driver itself upholds
(`DispatchReport` and `compute_status` are both content-free by construction).

## Contract

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Drive one content-pipeline run through Claude background sessions from a single dispatching Claude Code session, without reading unit content.
  scope:
    covers:
      - preparing a wave through the consumer's protocol mount
      - dispatching the wave with dispatch_wave and reading its DispatchReport
      - reading status digests at batch boundaries
      - finalizing accepted units and resuming a halted run
    excludes:
      - a worker's own one-unit procedure (use execute-work-unit)
      - the synchronous inline driver
      - the Workflow-tool lane (use workflow-pipeline)
  techniques:
    - id: run_background_wave
      name: Run a prepared wave through background sessions
      keywords: [background pipeline, dispatch_wave, claude --bg, finalize_run, DispatchReport, batch boundary, halted run]
      goal: Every unit of the prepared wave is settled and every accepted unit's output is applied through the adapter.
      steps:
        - n: 1
          action: Prepare the wave through the consumer's own protocol mount; the consumer's policy decides which units are ready and stale.
          tool: execution.protocol.build_handlers (prepare verb) or execution.controller.prepare_run
        - n: 2
          action: Dispatch the wave in one call that runs the full bounded loop (preflight, dispatcher election, launches up to max_agents, lease renewal, reclaim of dead workers).
          tool: content_pipeline.execution.drivers.claude_bg.dispatch_wave
          input: "store, run_id, wave, adapter, worker_command=..., max_agents=..., batch_size=..."
          expected: The call returns when the wave is exhausted or the run halts, with a DispatchReport.
        - n: 3
          action: Read DispatchReport.status_digests at batch boundaries; a digest carries counts and outcomes only.
          tool: DispatchReport.status_digests
        - n: 4
          action: Once dispatch settles, finalize so every accepted unit's output lands through the adapter's apply.
          tool: content_pipeline.execution.controller.finalize_run
          input: "store, run_id, adapter"
          on_failure: "A halted run parks at step 2. dispatch_wave does not clear a halt: once the halt condition has cleared, call controller.resume_run(store, run_id), then prepare and dispatch again."
      checklist:
        - "Wave prepared through the consumer's mount"
        - "dispatch_wave returned; aborted_reason read"
        - "Status digests read, no unit content ingested"
        - "finalize_run applied the accepted units"
      gotchas:
        - Never read a unit's prompt, a worker's answer text, or a validator's full feedback into the orchestrating session; only ids, outcomes, and status digests.
        - DispatchReport.accepted reflects store state; a unit settled as blocked or session_lingering can still be accepted and is finalized. Read settled for how the session ended.
        - An abort (aborted_reason set) is not a halt. A halt parks until resume_run clears it; an abort means this call stopped, and the reason says whether to investigate the environment or call again.
        - Do not build a pre-emptive quota gate that parses rate-limits.json to decide whether to dispatch; the reactive halt path is the contract.
        - Flags passed through extra_launch_args may or may not compose with a background launch; the launcher exits 0 either way, so observe what a worker actually does.
        - Storage engine, fresh-per-unit contexts, and single-dispatcher election are correctness decisions with no setting.
```

## The four stages

1. **Prepare** -- build the wave through the consumer's own protocol mount
   (the `prepare` verb of `execution.protocol.build_handlers`, or the
   consumer's own equivalent call into `execution.controller.prepare_run`).
   This step decides which units are ready and stale; it is entirely the
   consumer's policy (strategy, gates, freshness) and this skill does not
   second-guess it.
2. **Dispatch** -- call `dispatch_wave(store, run_id, wave, adapter,
   worker_command=..., max_agents=..., batch_size=...)`. This is a single
   call that runs the full bounded loop: capability/auth preflight, dispatcher
   election, launching workers into free slots up to `max_agents`, polling and
   renewing leases each tick, and reclaiming units whose worker died. It
   returns when the wave is exhausted or the run halts.
   The dispatcher records terminal worker failure as `worker_failed` and
   preserves its FAIL attempt detail. `DispatchReport.recovered` lists units
   adopted from durable open dispatch rows without a second launch.
3. **Status at batch boundaries** -- `dispatch_wave` itself emits a status
   digest (`compute_status`'s dict form) every `batch_size` dispatches, and
   again on exit; read `DispatchReport.status_digests` rather than polling the
   store directly. A digest carries counts and outcomes only -- never a
   prompt, a unit payload, or a full output (the same invariant the protocol's
   `status` verb upholds).
4. **Finalize** -- once dispatch settles (accepted, halted, or exhausted),
   call `execution.controller.finalize_run(store, run_id, adapter)` to apply
   every accepted unit through the adapter's `apply`. Finalize is the only
   place a unit's output actually lands; nothing before it writes a
   consumer-visible side effect.

A halted run (rate-limit, auth, or an operator pause) stops cleanly at stage 2
and parks. `dispatch_wave` never clears a halt: its next claim on a halted run
raises `RunHaltedError` and the wave returns `halted` at once. Once the halt
condition has cleared (the quota window reopened, the credential fixed), call
`execution.controller.resume_run(store, run_id)` (the mount's `resume` verb)
and then run stages 1 and 2 again -- prepare selects the units still pending,
including the one the halt returned to `PENDING`. Read the run's remaining
work with `execution.controller.unfinished_units(store, run_id)`; a unit left
CLAIMED by a dead session becomes reclaimable when its lease expires, and
`dispatch_wave` reclaims it.
A halt stops new claims immediately, but a submission that arrives after the
halt with a valid fencing token is still accepted exactly as if no halt had
happened -- a stale fence is rejected regardless. Lease renewal also differs
by lane during a halt: in the background lane the Python dispatcher renews a
worker's lease while its session is live, so a unit only becomes reclaimable
once a dead session's lease actually expires.

## Prerequisite: the launch directory must be a trusted workspace

`dispatch_wave` launches each worker with `claude --bg` in a working
directory: its `cwd` argument when given, else the working directory the
run's adapter environment records, else the dispatcher process's own. That
directory must already be a trusted workspace for the Claude CLI. Observed on
claude CLI 2.1.284: `claude --bg <prompt>` launched from a directory that is
not trusted exits 1 with the stderr `Workspace not trusted. Run `claude` in
<dir> once and accept the trust prompt, then retry.` and starts no session.

`preflight` does not check trust, so it passes. The failure surfaces at the
first launch. During launch confirmation, the dispatcher renews its run lease;
if another dispatcher takes it, the wave stops with
`aborted_reason == "dispatcher_lease_lost"` and does not attach the session.
Otherwise, the dispatcher finds no session within `launch_confirm_seconds`, releases the unit's claim, settles
the dispatch as `launch_failed`, and stops the wave with
`aborted_reason == "launch_misconfiguration"` (`LaunchMisconfigurationError`
inside `dispatch_unit`). Nothing is dispatched. The launcher's own words reach
you: `DispatchReport.launch_stderr` holds a one-line excerpt of its stderr (at
most 300 characters) and `DispatchReport.launch_rc` its exit code; the same two
values are `launch_stderr` / `launch_rc` on the raised error. Neither carries
unit content. On that abort, read `launch_stderr` and check the launch
directory's trust first.

Remedy: run `claude` once in the launch directory, accept the trust prompt,
then call `dispatch_wave` again.

## Configurable: `max_agents` and `batch_size`

These are the only two settings this skill treats as a genuine power-user
preference, because protecting interactive session-pool quota against
maximizing throughput is a real tradeoff a consumer is entitled to make
differently from run to run:

- `max_agents` (default 4) -- how many worker background sessions may be open
  at once. Raising it finishes a wave faster at the cost of spending quota
  faster; lowering it conserves quota at the cost of wall-clock time.
- `batch_size` (default 25) -- how many dispatches elapse between status
  digests (and the natural point to check quota headroom before continuing;
  see below).

Pass both directly to `dispatch_wave(..., max_agents=N, batch_size=M)`.

## Timing settings, and when to move them

Both timing settings have defaults that suit an ordinary run; move them only for the reasons
below, and never as a way to make a hanging wave finish sooner.

- `terminal_exit_grace_seconds` (default 300) -- how long a worker whose unit
  is already finished (it submitted and was accepted) may keep its session
  open before the dispatcher ends it with `stop` and `rm` and frees the slot.
  Nothing else in the system can close such a session, so this bound is what
  keeps a lingering worker from holding a slot indefinitely. Raise it if your
  workers legitimately do cleanup work after submitting; lower it to reclaim
  slots faster on a quota-tight run.
- `stall_timeout_seconds` (default 900) -- how long the wave may observe *no
  progress at all* before it gives up. Progress is anything that moved: a
  launch, a lease renewal, a settlement, a dropped slot, a terminal failure.
  A long-running but healthy unit renews its lease every tick and so re-arms
  this bound continuously -- it is never cut off by it. Raise this only if
  your workers can be genuinely silent for longer than the default between
  ticks.

## The repeated-failure breaker

`systemic_failure_halt_threshold` (default 3; `0` or `None` disables it; a
negative or non-integer value raises `ValueError`) halts the run when that many
units settled as `worker_failed` carry a systemic failure code. A worker
environment that disagrees with the run's makes every session fail the same
way, and a terminal failure has no reset, so without the breaker each remaining
unit costs a session.

A worker attaches the code itself: its fail envelope may carry an optional
payload member `"code"` from a closed set (`model.SYSTEMIC_FAILURE_CODES`), whose only member is `"env_mismatch"` (its
verb was refused by the environment check). The protocol refuses any other
value and accepts an absent code, and the code is recorded in the attempt's
error text as `{"code": ..., "detail": ...}`. Failures without a systemic code
never count, however alike their text; units adopted from an earlier
dispatcher's open rows count too. The halt kind is `repeated_failure`, the
report's `halted` shows it, and undispatched units stay `PENDING`. Fix the
cause, call `resume_run`, then prepare and dispatch again; the units already
failed stay failed.

## Selecting a worker agent

`dispatch_wave` also takes `extra_launch_args` -- a sequence of `claude`
flags forwarded verbatim to each worker launch, ahead of the launch prompt.
It defaults to empty, so the dispatcher launches a plain background session
and selects no agent unless you ask for one.

That is the seam for pointing a worker at an agent definition: this plugin's
shipped `agents/pipeline-worker.md`, or one you write yourself. Two things to
know before you rely on it. First, a worker is governed by its launch prompt
regardless: the prompt built for each unit names the run id, unit id, worker
id, and answer path, enumerates the exact invocations the worker may run, and
states the rule against composing a shell construct to satisfy a step.
Second, whether agent-selecting flags compose with a background launch rather
than being accepted and dropped has not been established -- the launcher
exits 0 either way, so the only way to tell is to observe what a worker
actually does. Treat an agent definition as extra discipline on top of the
launch prompt, not as a substitute for it.

## Reading the report

`DispatchReport.accepted` lists every unit this call dispatched or recovered
whose STORE state is accepted when its dispatch ended -- whatever `settled`
says. A worker that accepts and then blocks or lingers settles as `blocked` or
`session_lingering` and is still listed in `accepted`, because `settled`
describes the session and the store holds the truth about the unit. Read
`accepted` for what finalize will apply; read `settled` for how each session
ended.

`DispatchReport.leaked_sessions` lists the short ids of sessions the
dispatcher tried to end and whose `rm` failed or raised. They may still be
running; stop and remove them by hand (`claude stop <id>`, `claude rm <id>`).

`DispatchReport.settled` maps a unit id to how its dispatch ended. The
vocabulary a consumer will actually see:

- `accepted` -- the happy path: the worker submitted, the submission was
  accepted, and its session exited.
- `done_unaccepted` -- the session ended without an accepted submission.
- `blocked` -- the session is waiting on something (commonly a permission
  prompt) with nothing timing it out. The dispatcher stops renewing
  immediately, so the unit becomes reclaimable once its lease expires. If the
  unit was already accepted, it is also listed in `accepted` and is not
  reclaimed.
- `missing` -- the session vanished from the session listing.
- `session_lingering` -- the unit was accepted but the worker's session
  outstayed `terminal_exit_grace_seconds`; the dispatcher ended it with
  `stop` and `rm` and freed the slot. The unit's work is fine; only the
  session overstayed.
- `claim_failed` -- the dispatcher could not claim the unit, so nothing was
  launched for it. Routine: between candidate selection and the claim the
  unit went terminal or was claimed by someone else (a still-live earlier
  worker settling its own unit). The wave skips that unit and carries on
  with the rest; a halted run reports `claim_failed` here too, alongside
  `halted`.
- `already_terminal` -- an exhausted unit the dispatcher went to fail was
  already terminal when it tried, so the end state it wanted holds and
  nothing was done. Not a failure, and deliberately absent from
  `failed_exhausted`, which records only units this wave itself failed.
- `claim_refused` -- an exhausted unit the dispatcher went to fail had been
  re-claimed with a live lease, so it is no longer abandoned and not this
  dispatcher's to fail. Skipped for the rest of this wave.
- `worker_failed` -- the worker reported terminal failure; the unit is
  terminally FAILED and its FAIL attempt stays available for handoff.
- `wave_exit` -- the wave stopped while this dispatch was still open, so the
  dispatcher closed it on the way out. Every dispatch this call opened is
  settled before `dispatch_wave` returns, including on an abort: a dispatch
  left open would make its unit permanently unreclaimable in later waves.

`DispatchReport.halted` is a halt kind: `rate_limit`, `auth`, an operator
`pause`, or `repeated_failure` (the breaker above). `launch_stderr` and
`launch_rc` are set only on a `launch_misconfiguration` abort.

`DispatchReport.aborted_reason` is set when the loop stopped early rather
than exhausting the wave: `launch_misconfiguration` (a launch never reached
an observed running state -- its own dispatch is recorded as `launch_failed`,
and the wave stops rather than repeating a launch every later unit would fail
the same way), `dispatcher_lease_lost` (another
dispatcher took the run; the lease is re-acquired before every launch, so slow
launches alone do not lose it), `dispatcher_lease_held_by_another_dispatcher` (this
call never started, and launched nothing), or `wave_stalled` (nothing
progressed for `stall_timeout_seconds`). An abort is not a halt: a halted run
parks until `resume_run` clears the halt, while an abort means this call stopped and its reason
tells you whether to investigate the environment or simply call again.

## Not configurable, and why

Storage engine, fresh-per-unit contexts, and single-dispatcher election carry
no setting. Each is a correctness decision, not a preference: the store's
fencing and lease semantics are what make concurrent dispatch and reclaim safe
at all; a worker session with unit content from a prior unit still in its
context is exactly the failure mode fresh-per-unit-context launches exist to
prevent; and a second concurrent dispatcher racing the first against the same
run's claims is a bug, which is why `dispatch_wave` acquires a run-level
dispatcher lease and a second caller exits without launching anything rather
than racing. None of the three has a scenario where a consumer would
reasonably want the alternative and still want this driver.

## How to think about quota -- and what not to build

There is no programmatic API for remaining session-pool capacity. Two
observables exist for the ORCHESTRATING session (the one running this skill,
not a worker) to consult at a batch boundary, alongside the status digest
`dispatch_wave` already returns:

- `/usage`, run interactively in the orchestrating session; or
- the status-line rate-limit snapshot claude-ui-kit mirrors to
  `~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` -- a
  documented file contract, not an import edge into that plugin, and one that
  only exists when the `claude-ui-kit` plugin is installed (this plugin
  depends only on `bootstrap`) with statusline rate-limit reporting not
  disabled (`STATUSLINE_RATE_LIMIT_SNAPSHOT` unset to 0). Where present, it is
  whole-percent granular and refreshes only while an interactive session
  renders a statusline, so treat it as coarse foresight, never as a precise
  meter to steer dispatch by.

**This is an optimization, not the contract.** The guaranteed-correct path is
reactive: when a worker failure classifies as a rate-limit or auth halt
(`classify_settled_failure`, folded into `dispatch_wave`'s own halt handling),
the run halts, in-flight work settles as described above (a valid-fenced
submission still lands, a stale one does not), and the run parks durably as
"resume when replenished." Do not build a pre-emptive quota gate that tries to
predict exhaustion and stop dispatch early instead of relying on this halt
path -- the observables above are for a human operator deciding whether to
kick off a large wave right now, not a signal this skill's procedure needs to
branch on programmatically. If you find yourself writing code that parses
`rate-limits.json` to decide whether to call `dispatch_wave` at all, stop:
call it, and let the halt path do its job.

## Never ingest unit content into the supervising session

Nothing in this procedure ever needs to read a unit's prompt, a worker's
answer text, or a validator's full feedback into the orchestrating session's
own context. `DispatchReport`, `TickResult`, and `compute_status`'s digest are
all built to exclude it. If a debugging need ever seems to require reading
unit content from the orchestrating session, that need belongs in a
consumer-side tool reading the store directly and offline -- not in this
skill's live dispatch loop.
