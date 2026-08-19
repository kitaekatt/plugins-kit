# Session recipients

A **session recipient** is a Claude Code session -- a background session
(`claude --bg`) or, in a later release, a Workflow agent -- acting as one
content-pipeline worker instead of a synchronous `LLMBackend.complete()` call.
It exists because a headless `claude -p` call and a background/workflow
session draw from different capacity pools: routing batch work through a
background session lets a run spend from the larger interactive session pool
instead of the smaller one a `-p` call draws from. This reference is for a
developer wiring a project's own content-pipeline run onto that transport --
what a session recipient is, how to mount the protocol it speaks, what your
`RunAdapter` must declare, the allowlist your worker needs, and the
reconciliation obligation that comes with resuming a run.

## The protocol a session recipient speaks

A worker session never talks to your store directly. It speaks a small,
versioned JSON protocol -- one envelope in, one envelope out -- that you mount
on your own entry point:

```json
{"protocol_version": "1", "verb": "claim", "payload": {"run_id": "...", "unit_id": "...", "worker_id": "..."}}
-> {"ok": true, "result": {...}}
-> {"ok": false, "error": {"type": "...", "message": "..."}}
```

The verbs a worker uses are `claim`, `read`, `submit`, `fail`, and `renew`;
`prepare`, `status`, `pause`, `resume`, and `finalize` are the orchestrator's.
Every failure -- a malformed envelope, an unknown verb, a version mismatch, or
an exception a verb raises -- comes back as a typed `{"ok": false, "error":
...}` reply, never a raw traceback and never a silent no-op. Build your mount
by calling the library's handler-builder with your own already-open store and
adapter, then route incoming envelopes through the library's dispatcher; both
close over your store and adapter, so a mount needs no per-verb wiring beyond
supplying them once.

Mount `claim`/`read`/`submit`/`fail`/`renew` on whatever entry point your
worker actually invokes -- a CLI subcommand, a small script, an MCP tool. That
entry point IS your `WorkerCommand`'s `argv` template (see below): it is the
thing a worker process runs to reach the protocol at all.

**Data flowing through this protocol is untrusted end to end.** A `payload`
is data a worker submits; the library evaluates or stores it, never executes
it, and never lets it select policy (which validators run, which adapter is
mounted). Trusted policy -- which adapter, which validators, which lease
policy -- comes only from your own process's mount-time configuration, never
from anything inside an envelope. Treat a worker session exactly as you would
treat any other untrusted process talking to your service, because that is
what it is: a separate `claude` process, launched with no memory of your
orchestrating session, reachable only through the entry point you exposed.

## What your `RunAdapter` must declare

Your adapter is the one piece of consumer-specific configuration a mount
closes over. Two fields matter specifically for running through background
sessions, beyond the fields every adapter already needs (`unit_for`,
`parse_fn`, `apply`, and so on):

- **`environment`** -- a declaration of which environment variables and
  working directory a worker process must see to behave correctly (required
  variables, forbidden variables, and variables that should carry the
  worker's working directory). This is checked twice: once when the run is
  created, against the orchestrating process's own environment, and once on
  every worker verb, against the worker process's actual environment. A
  worker whose environment disagrees with what the run was created against is
  refused outright rather than allowed to resolve against the wrong project
  root silently -- that refusal is deliberate, because a background worker
  runs in a genuinely separate process and has no other way to prove it is
  the same project the run was prepared against.
- **`expected_unit_seconds`** (or a per-unit variant) -- your best estimate of
  how long one unit's worker session runs. This sizes the lease the
  dispatcher renews while a worker is active. Declaring nothing is safe --
  you get a conservative default -- but declaring an honest estimate for a
  genuinely agentic unit (a worker that reasons, retries against feedback,
  and may take minutes rather than seconds) matters: a lease sized for a
  one-shot API call expires mid-flight under a slower workload, and a unit
  that is reclaimed while its original worker is still healthily working
  gets duplicated and, eventually, permanently failed once its reclaim budget
  is exhausted. If your units are agentic, declare a realistic cost rather
  than leaving this at its default.

## The allowlist your worker needs

A worker session is launched with a prompt that names its run id, unit id,
worker id, and answer path, and states the **exact invocations** it may run
to complete that unit -- never an outcome it is free to satisfy by whatever
means it composes. This is not a style preference: an outcome-phrased
instruction ("write the result to this file") leaves a model free to satisfy
it with a shell redirect, a pipe, or any other construct your allowlist never
saw coming, because nothing enumerated it in advance. State the procedure as
literal command strings instead, and your worker's tool-permission allowlist
can pre-authorize exactly those strings before the worker session ever
starts.

Concretely: your `WorkerCommand` names the argv template for your protocol
mount's entry point and the directory a worker writes its answer file into.
Given that, a run id, a unit id, and a worker id, the exact five invocations a
worker for that one unit will ever need to run are fully determined --
deterministic in those three values alone, with no unit content, timestamp, or
random component in any of them. That determinism is what makes a
pre-authorized allowlist possible at all: you can compute and allowlist a
unit's five invocation strings before its worker session launches, because
nothing about them depends on what the worker actually produces.

Build your worker's allowlist from those five computed strings, not from a
broader grant (e.g. "any invocation of my protocol mount"). A broad grant
reopens exactly the gap the enumerated-invocation design closes: a worker
that is merely *capable* of running other invocations of your mount is a
worker whose behavior your allowlist no longer bounds.

## Resuming a halted or interrupted run

A run can be interrupted between recording that a unit's apply started and
recording that it succeeded -- a crash mid-finalize, a killed dispatcher
process. Resuming a run with any unit left in that in-between state is
refused by default: the library will not silently re-apply a unit whose apply
may already have landed, because doing so risks a duplicate external side
effect (a duplicate file write, a duplicate changelist edit) with no way to
tell afterward that it happened twice.

To resume past that state safely, your adapter must supply a reconciliation
hook: given a unit id, answer "did this unit's apply already land." Whether
that question is answerable cheaply depends on your write shape, not on
which version-control system you use. A deterministic read-modify-write of a
single keyed record (a CSV row, a database row) is easy to reconcile: compare
the record's current values against what the payload would have written, and
you have your answer with no version-control query at all. A whole-file
rewrite generally has no such marker and should stay fail-closed -- supply no
reconciliation hook, and accept that a run interrupted mid-apply needs a
human to look at the affected units before it can resume.

Do not treat your version-control system's own state (a file open for edit,
a pending changelist) as a reconciliation signal by itself: an open-for-edit
file can carry stale or partial content, so its mere existence tells you
nothing about whether the write that mattered actually completed. Build
reconciliation from your data's own shape, not from VCS bookkeeping.

## Which worker your dispatch runs

Every worker the dispatcher launches is governed by its **launch prompt**,
which is built for it and stands on its own: run id, unit id, worker id,
answer path, the exact invocations it may run, and the rule against composing
a shell construct to satisfy a step. That is the constraint on a worker, and
it applies whether or not any agent definition is loaded.

On top of that, `dispatch_wave` takes `extra_launch_args` -- a sequence of
`claude` flags forwarded verbatim to the launch, ahead of the prompt. That is
where you select an agent definition: this plugin's shipped
`agents/pipeline-worker.md` (the worker procedure written out as behavioral
discipline), or one you write yourself. The default is empty, so the
dispatcher selects no agent unless you ask for one.

Know one thing before you rely on it: whether agent-selecting flags compose
with a background launch, rather than being accepted and dropped, has not
been established. The launcher exits 0 either way, so the only way to tell is
to observe what a worker actually does. Treat an agent definition as a way to
strengthen a worker's discipline, and the launch prompt as the constraint you
can count on.

## Rules to carry into your own worker prompt or agent definition

If you write your own worker agent rather than selecting this plugin's
shipped one, carry these rules forward -- they are the ones that determined
the shipped design and the failures that motivated each:

- State the procedure as exact invocations, never as an outcome. (See "The
  allowlist your worker needs" above.)
- Never put unit content in a command line. A worker learns its run id and
  unit id from its launch prompt only; it fetches actual unit content through
  the `read` verb at runtime, and that content stays out of every subsequent
  invocation's arguments.
- On a rejected submission, revise from the feedback and resubmit through the
  same allowlisted `submit` invocation -- never invent a different invocation
  to route around a rejection.
- On exhaustion (feedback you cannot address, or a unit you conclude is not
  answerable), report failure through the `fail` verb and stop. Do not
  fabricate an answer to close the unit out. A worker that correctly refuses
  to fabricate output when blocked is behaving as designed, not failing to
  find a workaround.
