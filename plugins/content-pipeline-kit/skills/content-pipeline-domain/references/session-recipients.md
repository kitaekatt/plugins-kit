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
repeat-safe apply that lets an interrupted run resume.

## The protocol a session recipient speaks

A worker session never talks to your store directly. It speaks a small,
versioned JSON protocol -- one envelope in, one envelope out -- that you mount
on your own entry point:

```json
{"protocol_version": "2", "verb": "read", "payload": {"run_id": "...", "unit_id": "..."}}
-> {"ok": true, "result": {...}}
-> {"ok": false, "error": {"type": "...", "message": "..."}}
```

The verbs a background worker uses are `read`, `submit`, and `fail`. In the
background lane, `claim` is the
DISPATCHER's: it claims each unit before launching that unit's session and
passes the resulting fencing token to the worker in its launch prompt, so a
background worker never claims anything and a session left alive by an earlier dispatch
cannot take the claim back after a reclaim (in the workflow lane the agent
claims its own unit; see `workflow-lane.md`). `renew` is the dispatcher's too
-- the background lane makes the dispatcher the renewer in the background lane
(`supervise_tick` calls the store's lease-renew method itself, on a schedule,
while a worker session is alive), so a worker session never runs it.
In the background lane, `prepare`, `status`, `pause`, `resume`, `finalize`, `claim`, and `renew` are
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
  worker's working directory). Path values compare equal when identical or,
  on Windows, when they differ only in letter case or separator spelling; a
  Git Bash POSIX spelling of a drive path still refuses. This is checked twice: once when the run is
  created, against the orchestrating process's own environment, and once on
  every worker verb except `fail` (see below), against the worker process's actual environment. A
  worker whose environment disagrees with what the run was created against is
  refused outright rather than allowed to resolve against the wrong project
  root silently -- that refusal is deliberate, because a background worker
  runs in a genuinely separate process and has no other way to prove it is
  the same project the run was prepared against.

  Know what that refusal does and does not buy you given that the dispatcher
  claims. It refuses the dispatcher's `claim` and `renew` and the worker verbs
  `read` and `submit`,
  so a mismatched worker can never get output ACCEPTED, which is the part
  that matters. `fail` is exempt: a worker that diagnosed its own environment
  as wrong must still be able to report it, and the run's adapter-version
  check still applies to it. What the refusal does not prevent is the SPEND:
  the unit is claimed and the session launched before any worker verb runs,
  so a mismatched worker consumes a session before its `read` is refused.
  When the worker then reports through `fail` with the envelope's terminal
  flag set, the unit ends FAILED, and a FAILED unit has no reset path: redoing
  it needs a new run. Without a `fail`, the unit is reclaimable once the lease
  expires. A systemic mismatch therefore costs one session per dispatched
  unit until a breaker trips: `dispatch_wave` halts the run
  (`HALT_REPEATED_FAILURE`) once `systemic_failure_halt_threshold` units
  (default 3; `0` or `None` disables) settle `worker_failed` with the code
  `env_mismatch` on their fail envelope, and `resume_run` clears the halt.
  Failures without that code are never counted, whatever their text -- check
  the environment declaration on the first failure.
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

The permission mode does not stand in for that allowlist (live probe, claude
CLI 2.1.284, 2026-09-29). Under `auto`, a `claude --bg` worker ran `rm -rf` on
a directory outside its working directory without asking, so `auto` is not a
safety boundary for an unattended worker. It refused a `git push --force` by
stopping to ask: the session parked in agents state `blocked` until someone
answered, so a refused action costs a hung worker, which `dispatch_wave`
settles as `blocked`. `--permission-mode default` alone is not strict either:
the worker inherits user-level settings, and a broad allow list there let
ordinary commands run unattended. Adding `--setting-sources project` isolates
the worker from user-level settings, and a command outside the allowlist then
parks the session `blocked` (waitingFor "permission prompt"). What ran
unattended: `--permission-mode default --setting-sources project
--allowedTools "Bash(<exact command>)" Write`, one `Bash(...)` entry per
literal invocation. Pass these through `extra_launch_args`; the driver places
`--` between them and the prompt, which matters because `--allowedTools` is
variadic and would otherwise consume the prompt. `acceptEdits` auto-approved
both a Write and a `touch`, so it is looser than `default`.

`--setting-sources project` and the shipped `pipeline-worker` agent do not
combine when the plugin is enabled only in user settings (live probe, claude
CLI 2.1.284, 2026-09-29): `--agent content-pipeline-kit:pipeline-worker` printed
`warning: no agent named 'content-pipeline-kit:pipeline-worker' -- spawning with
default template`, and the launcher exited 0, but the session then failed
without running (see the isolation probes below). The warning goes to
the launcher's output, which `dispatch_wave` reports as `launch_stderr` only on
a failed launch, so do not expect to see it. Isolation probes (live, claude CLI 2.1.284,
2026-09-29, launch folder trusted for `--bg`): plain `--bg` works;
`--agent content-pipeline-kit:pipeline-worker` alone resolves the agent and the
session sees only Bash and Write; `--setting-sources project` alone works. The
combination drops the agent: the launch printed the `no agent named` warning,
`claude agents --json --all` reported state `failed`, no transcript was written,
and `claude logs` failed with `connect ENOENT \\.\pipe\cc-daemon-*-control`. The
earlier `logs` ENOENT and `failed` observations came only from launches using the
combination (one also with `--plugin-dir`, two with the plugin enabled in the
folder's project settings); no launch without the combination was observed
failing.

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

A halted run (rate limit, auth, operator pause) resumes only through
`execution.controller.resume_run(store, run_id)` (the mount's `resume` verb),
once the halt condition has cleared. Dispatching again does not clear it: the
next claim on a halted run is refused. After `resume_run`, prepare and
dispatch again in either lane.


A run can be interrupted between recording that a unit's apply started and
recording that it succeeded -- a crash mid-finalize, a killed dispatcher
process. The status digest lists such units in `apply_started_unit_ids`.
Recovery is to run finalize again: it applies every accepted unit whose last
apply outcome is neither succeeded nor rejected, so the interrupted unit is
applied a second time.

That is safe only because your adapter's `apply` must be repeat-safe. For the
same run, unit, and payload it sets the complete desired end state; it never
appends:

- Upsert keyed data. A CSV or database row is written by its stable key (a
  line id), so a second write of the same payload leaves the same row.
- Rewrite a whole file from the payload, writing only when the content
  differs. `deliver.inplace.apply_inplace` already works this way.
- Find or create any external container, such as a changelist, by a tag that
  carries the run id, so a repeat reuses the container the first attempt
  created instead of opening a second one. `deliver.inplace.deliver_changeset`
  mints a new changeset when it is given none, so under an adapter pass it
  the found-or-created run-tagged changeset through `changeset=`.

Raise `execution.model.ApplyRejected` only when you know no side effect
occurred; that outcome is terminal and finalize does not retry it. Any other
exception leaves the unit retryable by the next finalize. There is no
reconciliation hook: `RunAdapter.reconcile` was removed in 0.28.0. An adapter that still
passes `reconcile=` fails at construction with `TypeError`; make its `apply`
repeat-safe and drop the argument.

## What bounds a worker's verbs

A worker writes the body of an envelope file, and the dispatcher fixes the
file name. When a `protocol @<path>` file has a worker-shaped name
(`*.claim.json`, the dispatcher's claim in the background lane and the
agent's own in the workflow lane; `*.read.json`, `*.submit.json`,
`*.fail.json`), the body's
verb and ids must agree with that name, or the call is refused with
`EnvelopeIdentityError`. A body naming another verb (`finalize`, `resume`,
`pause`, `prepare`) or another unit therefore cannot run from a worker file.
Files with any other name are not checked; the allowlist still limits which
paths a worker may invoke.

## The launch directory must be trusted

`dispatch_wave` runs `claude --bg` in its `cwd` argument, else the directory
the run's adapter environment records, else the dispatcher's own working
directory. Observed on claude CLI 2.1.284: when that directory is not a
trusted workspace, the launch exits 1 with `Workspace not trusted. Run
`claude` in <dir> once and accept the trust prompt, then retry.` and spawns no
session.

`preflight` does not check trust. During confirmation, the dispatcher renews
its run lease; if that lease is lost, it stops and removes the identified
session and ends the wave with `aborted_reason == "dispatcher_lease_lost"`
without attaching it. Otherwise, `dispatch_unit` finds no session within
`launch_confirm_seconds`, releases the claim, settles the dispatch as
`launch_failed`, and raises `LaunchMisconfigurationError`; `dispatch_wave` then
stops with `aborted_reason == "launch_misconfiguration"` and launches nothing.
The launcher's stderr excerpt (which carries the trust message) and exit code
reach the caller in the exception message ("launcher said (rc=...): ...") and
in the report's `launch_stderr` and `launch_rc`. Remedy: run `claude` once in that directory,
accept the trust prompt, and dispatch again.

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

Know one thing before you rely on it: an agent-selecting flag can be accepted
and dropped. It composed in one probe (live probe, claude CLI 2.1.238,
2026-08-21: `--agent content-pipeline-kit:pipeline-worker` with
`--append-system-prompt-file` on `claude --bg`; the worker reported only the
agent's tools, Bash and Write, and echoed a marker from the appended file). It
was dropped, and the session failed, when combined with `--setting-sources
project` (see the paragraph above).
The launcher exits 0 either way, so for your CLI version and settings, observe
what a worker actually does. Treat an agent definition as a way to strengthen
a worker's discipline, and the launch prompt as the constraint you can count
on.

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
