# The workflow lane

A second way to run a content-pipeline unit's worker session, alongside the
background-session lane `session-recipients.md` describes: through the
native Workflow tool's `agent()`/`parallel()` primitives instead of
`claude --bg`. This reference is for a developer wiring their own consumer
onto that lane -- the mount owner, not the session orchestrating a run (that
procedure is `Skill(content-pipeline-kit:workflow-pipeline)`).

Both lanes speak the same protocol described in `session-recipients.md` --
the same `{"protocol_version": "1", "verb": ..., "payload": ...}` envelope
in, the same typed `{"ok": ...}` reply out, the same `WorkerCommand`,
`environment`, and `expected_unit_seconds` adapter fields. Everything in
that reference about the protocol, the allowlist principle, the fence line,
and reconciliation on resume applies here unchanged. What differs is who
claims, who renews, and how a run is assembled.

## How this lane differs from the background lane

**The agent claims its own unit.** In the background lane, the dispatcher
claims each unit before launching that unit's session and hands the worker
a fencing token in its launch prompt -- the worker itself never calls
`claim`, which is exactly why `claim` and `renew` do not appear in
`worker_envelopes_for`'s verb set. This lane is the opposite: there is no
separate dispatcher process, because the native Workflow tool cannot be
invoked from inside a subagent. The top-level session that invokes the
Workflow tool is the only thing that could claim on an agent's behalf, and
it does not -- claiming from the top level would mean either holding a
whole wave of claims open while agents run one at a time (starting every
unit's whole-runtime lease long before its agent is scheduled) or chunking
the wave into one Workflow invocation per unit, which this lane does not
do. So each agent runs the `claim` verb itself, as its first protocol call,
and the resulting fencing token is the sole authority for every step after
it.

**The fencing token comes from the claim reply, not a launch prompt.** A
background worker receives its token already resolved, embedded in its
launch prompt text. A workflow agent has no such prompt-time token: it
learns its token only from the reply to its own `claim` call, at runtime,
and every later step (writing the fence line into its answer file, filling
`<FENCING_TOKEN>` into its submit or fail envelope) uses that reply value,
never a value baked into the pack ahead of time.

**No dispatcher, no renewer, whole-runtime leases.** The background lane's
Python dispatcher renews a live worker's lease on a schedule
(`supervise_tick`), so a unit becomes reclaimable only once a dead worker's
session has actually gone quiet. This lane has no equivalent process
running alongside the agents, so there is no renewal at all: a unit's lease
is sized once, at claim time, to cover the agent's entire expected runtime
(`lease_for`'s `max(300, cost * 2.0)` headroom), and nothing touches it
again until the agent's own `submit`/`fail` call, or until it lapses. A
lapsed lease is reclaimed the same way an abandoned background-lane unit is
-- transparently, on the next candidate selection -- but nothing inside one
Workflow invocation renews a lease mid-flight, ever.

## Lane construction partitions wave positions, not stored ordinals

The stored ordinal on a `UnitRecord` can be holey -- selection, staleness,
and prior completions all remove units from candidacy, so a prepared wave's
ordinals need not be contiguous. This lane's `N`-lane loop divides the
**positions of the ordinal-sorted wave array** into `N` interleaved lanes
(lane `k` takes wave positions `k, k+N, k+2N, ...`), not residue classes of
the stored ordinal values themselves. For a wave holding stored ordinals
`[2, 5, 9]` with `N=2`, the two readings disagree: the position reading
gives lane 0 positions `[0, 2]` (stored ordinals 2 and 9) and lane 1
position `[1]` (stored ordinal 5); a stored-ordinal-residue reading would
try to index units 0, 2, 4... of the *run*, which are not even present in
this wave, and can silently leave a lane empty -- cutting concurrency below
`N` with no error. `N` itself is derived from the wave's own count
(`min(max_agents, 16, pending)`), which is what makes the position reading
the only one consistent with how `N` was chosen in the first place. A
pack's `ordinal` field carries the stored ordinal for reporting only; it
plays no role in lane assignment.

## The args contract

`run-ready-wave.js` receives `args` as a **JSON string**, per the Workflow
tool's runtime, and normalizes it on its first executable line the same way
`compiler.py`'s codegen does: `typeof args === "string" ? JSON.parse(args)
: (args || {})`. Every field after that point is read from the parsed
object. The object's shape:

- `runId`, `batchId` -- both minted by the Python side (`build_wave_args`);
  a workflow script never mints its own identity.
- `maxAgents` -- a positive integer; the script derives its lane count from
  `min(maxAgents, 16, units.length)`.
- `units` -- an ordinal-sorted array of per-unit packs, one entry per unit
  in the wave, each carrying:
  - `unitId`, `ordinal`, `workerId`
  - `claimCmd`, `readCmd`, `submitCmd`, `failCmd` -- the four command
    strings an agent runs verbatim
  - `answerPath`, `writeSubmitPath`, `writeFailPath` -- the three Write
    targets
  - `submitTemplate`, `failTemplate` -- the envelope text an agent authors
    at runtime, with only the literal `<FENCING_TOKEN>` placeholder
    substituted from its claim reply

Every one of those strings is produced by
`content_pipeline.execution.workerpack` for a given `(runId, unitId,
workerId)`: `readCmd`, `submitCmd`, and `failCmd` are byte-identical to what
the background lane's own pack-building helpers produce for the same
inputs (including that `submitCmd` ends with `--text-file=<answerPath>` in
its `=`-joined form), and `claimCmd` is the one string that has no
background-lane counterpart, because the background lane never gives a
worker a claim command at all. `workerId` is derived as
`wf-<batchId>-<unitId>`, sanitized the same way every other path component
in this system is.

Do not hand-compose any of these strings. A consumer wiring this lane calls
`build_wave_args`, gets the whole `args` object back, and passes it to the
Workflow tool unmodified.

## Per-agent and aggregate schemas

Each agent returns exactly one object matching a value-bounded schema --
`additionalProperties: false`, a closed `outcome` enum (`accepted`,
`failed`, `halted`, `claim_unavailable`), a bounded `attempts` integer, and
a closed `error_code` enum. The agent supplies no identity field and no
free-form string anywhere in its return; `unitId`, `ordinal`, and
`workerId` are attached from the pack after the agent returns, never taken
from what the agent reports, so a garbage or duplicated identity claim
inside an agent's own reply cannot leak into the aggregate.

The aggregate the Workflow tool call ultimately returns -- run and batch
ids, wave size, lane count, per-outcome counts, the per-unit array in
position order, and an explicit `advisory: true` marker -- is a summary of
those self-reports, not a read of the store. Bounding the schema's values
closes the channel where an agent could smuggle unbounded content through a
free-form field, but it does not stop an agent from misreporting its own
enum value honestly-shaped but wrong. Treat the aggregate exactly as
advisory as its marker says: reconcile it against the store's real state
before treating any unit as settled. `Skill(content-pipeline-kit:workflow-pipeline)`
owns that reconciliation step for the orchestrating session; this reference
only describes the shape the mount must produce and accept.

## The lease rule, and what a bounded expiry means for a unit

A lease starts the moment an agent's `claim` call succeeds and is sized
once, with no renewal, to cover the agent's entire expected runtime. If an
agent's session dies or stalls before it submits or fails, its unit's lease
simply runs out -- there is no dispatcher watching for that, and nothing
inside the same Workflow invocation notices or reacts. The unit sits
`CLAIMED` with an expired lease until the *next* wave's candidate
selection reclaims it, which is a normal, expected outcome of this lane's
lack of a renewer, not a failure to design around. Bound this lease
generously: if the mount's declared `expected_unit_seconds` under-costs a
genuinely agentic unit, a slow-but-healthy agent's lease can lapse and be
reclaimed by a later wave's agent while the original is still working --
duplicating spend on that unit, never corrupting its stored result, because
a stale agent's eventual submit is refused once its fence is superseded (see
the residual below).

## The allowlist recipe

Build a workflow agent's allowlist from the same three per-unit command
strings and three Write targets `session-recipients.md` describes for a
background worker, with one addition: `claimCmd` must be allowlisted too,
because in this lane the agent runs it, not the orchestrator. All four
command strings and all three Write targets are deterministic in
`(runId, unitId, workerId)` alone -- no unit content, no timestamp, no
fencing token -- so a consumer can compute and pre-authorize a unit's full
set before any agent runs, exactly as `session-recipients.md` describes for
the background lane's six strings. The **orchestrating session's** own
tool-permission allowlist is what actually gates a workflow agent's calls,
because a workflow agent inherits that allowlist and has no permission mode
of its own -- there is no per-agent grant step analogous to a background
session's own launch-time permissions.

## Thin-recipient guarantee

`run-ready-wave.js` mutates nothing on its own. Every state change --
`claim`, `submit`, `fail` -- crosses the protocol seam through the mount
into `dispatch`, fenced by the store exactly as any other verb call is.
The script's own job is wave partitioning, invoking agents, and shaping the
advisory aggregate; `prepare`, reap, `status`, `finalize`, and
`pause`/`resume` all stay the invoking session's responsibility through the
same mount, never something the script does for itself. This mirrors the
background lane's own division of labor between the driver (dispatch
mechanics) and the orchestrating skill (prepare/finalize).

## The self-claim residual -- stated honestly, not as solved

Self-claim is a deliberate departure from the background lane's anti-zombie
property: `worker_envelopes_for` withholds a `claim` envelope specifically
because that is "what stops a session left alive by an earlier dispatch
from re-claiming a unit." This lane gives that property up on purpose, and
what replaces it is bounded, not eliminated.

The claim envelope this lane writes is **worker-scoped** -- keyed on
`(runId, unitId, workerId)`, not just `(runId, unitId)` -- so a later
wave's fresh worker id never collides with, or overwrites, an earlier
stalled agent's own claim envelope. That closes identity confusion between
concurrent or overlapping generations of the same unit.

It does not close the timing gap. A workflow agent that stalls after
claiming -- blocked, slow, or otherwise not making progress -- still holds
a syntactically valid, worker-scoped claim command it could still run.
If it fires *after* a later wave's lease on the same unit has since
expired, the store's `claim` handling transparently reclaims the expired
lease and issues a new fence to whichever agent claims next; a stale
agent's eventual `submit` then lands as a fence mismatch and is refused,
not accepted. The bound this gives you is real -- **duplicated spend on a
unit, never a duplicated or corrupted side effect** -- but it is a bound on
the blast radius, not a proof that only one agent ever holds a live claim
for a unit at a time.

The instruction a worker follows -- "on any typed refusal reply, return
immediately; never run the claim invocation again" -- is exactly that: an
instruction to the agent, stated in its brief, not a mechanism this lane
enforces independently of the agent choosing to follow it. Do not describe
one-claim-per-agent as an invariant this lane guarantees. A consumer that
needs to know whether it actually held in a given run should inspect real
attempt rows for exactly one claim per worker id, rather than trusting the
aggregate or the brief's instruction alone.
