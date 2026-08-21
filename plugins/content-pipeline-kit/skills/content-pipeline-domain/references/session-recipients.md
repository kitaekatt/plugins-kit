# Session recipients

A **session recipient** is a Claude Code session -- a background session
(`claude --bg`) or a native Workflow tool agent, see `workflow-lane.md` for
that lane's own mount contract -- acting as one content-pipeline worker
instead of a synchronous `LLMBackend.complete()` call.
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
{"protocol_version": "1", "verb": "read", "payload": {"run_id": "...", "unit_id": "..."}}
-> {"ok": true, "result": {...}}
-> {"ok": false, "error": {"type": "...", "message": "..."}}
```

The verbs a worker uses are `read`, `submit`, and `fail`. `claim` is the
DISPATCHER's: it claims each unit before launching that unit's session and
passes the resulting fencing token to the worker in its launch prompt, so a
worker never claims anything and a session left alive by an earlier dispatch
cannot take the claim back after a reclaim. `renew` is the dispatcher's too
-- D5 makes the dispatcher the renewer in the background lane
(`supervise_tick` calls the store's lease-renew method itself, on a schedule,
while a worker session is alive), so a worker session never runs it.
`prepare`, `status`, `pause`, `resume`, `finalize`, `claim`, and `renew` are
all the orchestrator's. Every failure -- a malformed envelope, an
unknown verb, a version mismatch, or an exception a verb raises -- comes back
as a typed `{"ok": false, "error": ...}` reply, never a raw traceback and
never a silent no-op. Build your mount by calling the library's
handler-builder with your own already-open store and adapter, then route
incoming envelopes through the library's dispatcher; both close over your
store and adapter, so a mount needs no per-verb wiring beyond supplying them
once.

Mount `read`/`submit`/`fail` (plus the orchestrator verbs, `claim` among
them, on the same mount or a separate one) on whatever entry point your
worker actually
invokes -- a CLI subcommand, a small script, an MCP tool. That entry point IS
your `WorkerCommand`'s `argv` template (see below): it is the thing a worker
process runs to reach the protocol at all. A worker's own invocation of that
entry point is always `<argv> protocol @<envelope path>` -- the library's
`@<path>` envelope-sourcing form (`cli.run.build_commands`'s `protocol`
command) -- optionally paired with `--text-file=<answer path>` for `submit`,
which splices a separately-written answer file's content into the envelope's
`text` field before dispatch. Neither the small JSON envelope nor the
(possibly large) answer text ever appears in the invocation string itself.

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

  Know what that refusal does and does not buy you now that the dispatcher
  claims. It still refuses every worker verb -- `read`, `submit`, `fail` --
  so a mismatched worker can never get output ACCEPTED, which is the part
  that matters. What it no longer prevents is the SPEND: the unit is claimed
  and the session launched before any worker verb runs, so a mismatched
  worker consumes a session and holds the lease until its `read` is refused.
  Previously the mismatch was caught at the worker's own `claim` and the unit
  stayed pending. The dispatcher settles that dispatch and the unit is
  reclaimable once the lease expires, so nothing is stranded -- but a
  misdeclared environment now costs sessions rather than being free.
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
worker id, answer path, and fencing token, and states the **exact
invocations** it may run
to complete that unit -- never an outcome it is free to satisfy by whatever
means it composes. This is not a style preference: an outcome-phrased
instruction ("write the result to this file") leaves a model free to satisfy
it with a shell redirect, a pipe, or any other construct your allowlist never
saw coming, because nothing enumerated it in advance. State the procedure as
literal command strings instead, and your worker's tool-permission allowlist
can pre-authorize exactly those strings before the worker session ever
starts.

Concretely: your `WorkerCommand` names the argv template for your protocol
mount's entry point, the directory a worker writes its answer file into
(`answer_dir`), and the directory its JSON protocol envelopes live in
(`envelope_dir`, defaulting to `answer_dir` when unset). Given that, a run id,
a unit id, and a worker id, the exact six invocations/Write-tool targets a
worker for that one unit will ever need to run or write are fully
determined -- deterministic in those three values alone, with no unit
content, timestamp, random component, or (critically) fencing token in any of
them. That determinism is what makes a pre-authorized allowlist possible at
all: you can compute and allowlist a unit's six strings before its worker
session launches, because nothing about them depends on what the worker
actually produces.

The dispatcher knows the fencing token before the launch -- it claims the
unit itself -- and still keeps it out of every one of those six strings.
The token reaches the worker in the launch prompt, and travels onward only
as file CONTENT: the envelopes the worker authors, and the fence line of its
answer file. Nothing that has to be allowlisted ahead of time ever varies
with it.

Build your worker's allowlist from those six computed strings, not from a
broader grant (e.g. "any invocation of my protocol mount"). A broad grant
reopens exactly the gap the enumerated-invocation design closes: a worker
that is merely *capable* of running other invocations of your mount is a
worker whose behavior your allowlist no longer bounds.

Two of the six are JSON envelope files the WORKER authors itself (the
`submit` and `fail` envelopes), from templates the library also computes
ahead of time -- every field except the fencing token is fixed text; the
worker's only permitted edit is substituting the literal `<FENCING_TOKEN>`
placeholder for the value its launch prompt names. The remaining envelope
file (`read`) needs no runtime information at all, so the dispatcher
pre-writes it before the worker session ever launches.

### The answer artifact carries its own fence

The answer path is deterministic in `(run_id, unit_id)` -- no worker id, no
generation -- because that is what lets you compute it before the run. The
cost of that is real and is handled explicitly: two successive dispatches of
one unit write the SAME file, so a session left over from an earlier dispatch
can overwrite it while a newer worker is running, and the newer worker's
submit envelope would be entirely valid.

So the artifact declares which claim produced it. Its **first line** is
`content-pipeline-fence:` followed by the fencing token, and the answer text
begins on the next line. The `--text-file=` splice matches that declaration
against the submit envelope's own `fencing_token` before any text reaches the
protocol, and refuses on a mismatch in either direction -- a stale artifact
under a current envelope, or a current artifact under a stale envelope -- as
well as on an artifact with no fence line at all, which is never read as
unfenced-and-fine. Only the first line is interpreted, so answer text that
happens to contain the prefix passes through untouched.

If you write your own worker prompt, carry that rule into it: the fence line
is not decoration, it is the only evidence the submitted text and the claim
authorizing it belong to the same generation of the unit.

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
answer path, fencing token, the exact invocations it may run, and the rule
against composing a shell construct to satisfy a step. That is the constraint
on a worker, and it applies whether or not any agent definition is loaded.

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
