# content-pipeline-kit: Claude session recipients -- plan of record

Status: plan of record for adding Claude background-agent and Workflow execution
to content-pipeline-kit. Repository home: `docs/planning/content-pipeline-kit/`.
This is maintainer material; nothing in this document ships under `plugins/`.

Citation convention: paths under `plugins/content-pipeline-kit/lib/content_pipeline/`
are abbreviated to their subpackage (e.g. `pipeline/single_pass.py`). Claims marked
**verified** were checked against source at the cited lines. Claims marked
**platform-dependent** rest on Claude Code documentation or observation and are
inventoried in "Platform assumptions" with the live gate that tests each.

## Summary

content-pipeline-kit executes LLM work units through a synchronous in-process
`LLMBackend.complete()` call (`llm/platform.py:144-161`). This plan adds two
execution mechanisms that cannot fit that call shape: Claude background sessions
(`claude --bg`, spawnable from Python) and Claude Workflow agents (invocable only
by a model in a live session). The motivation is capacity: `claude -p` draws a
small headless allowance that batch work exhausts, after which it falls to metered
credits; background sessions and workflow agents draw the much larger interactive
session pool.

The build is four phases. **A-min** adds, without breaking anything: a durable
SQLite run store with claims, leases, and fencing; a prepare/finalize run
lifecycle; a versioned worker protocol with a consumer `RunAdapter`; and an
additive tracked inline driver over the existing backends; A-min.4 then closes
the library defects that only running it against real consumers exposed. **B**
ships a
background-session driver plus a one-unit worker agent, gated by live platform
qualification. **C** ships one reviewed independent-wave workflow over the same
protocol, gated the same way. **A-cleanup** then performs the legacy halt/resume
corrections, the untracked-loop deprecation, and `cli/` cohesion work -- after the
capability exists, keyed to adoption evidence. Every phase boundary is a release a
consumer can safely sit on.

## Motivation and the capacity premise

The premise: a `claude -p` process draws from a small headless allowance
(observed at roughly 10% of plan capacity), and once that pool is exhausted,
further `-p` calls are metered against credits; `claude --bg` sessions and
workflow `agent()` calls instead draw the interactive session pool (rolling
five-hour and seven-day windows), which is large enough for batch work.

Evidentiary status, stated plainly because the whole effort rests on it:

- The small-headless-pool half rests on the **user's direct operational
  observation**. Public documentation conflicts with it: a Help Center article
  states the planned separate Agent SDK/`-p` monthly credit was paused and `-p`
  still draws subscription limits. The observation is treated as authoritative
  over the docs because it is direct and the docs have a record of trailing the
  rollout, but the exact ratio and whether it is plan- or account-specific are
  unknown.
- The `--bg`-draws-the-session-pool half rests on documentation (`agent-view`:
  background sessions "consume subscription usage like interactive sessions",
  "10 parallel sessions ~= 10x quota usage") read partly from a summarized fetch,
  and has **never been observed live**.
- Workflow `agent()` calls counting as ordinary session usage rests on
  documentation (`workflows`, `costs`), also unobserved.

What would falsify the premise: a live probe showing `--bg` work drawing the same
allowance as `-p`, or the headless pool being large enough that batch work never
exhausts it, or `--bare` becoming `-p`'s default in a way that changes the
comparison. Phase B2 contains a **pass/fail capacity-classification probe**
(see the B2 gate) precisely because the user's observation already contradicted
the documentation once for `-p`; the documented claim about `--bg` gets tested,
not inherited. No phase spends significant implementation effort past the point
where this probe could invalidate it: A-min is justified independently of the
premise (durable runs and status benefit the existing OpenRouter lane), and B1
is the first premise-dependent code, gated by B2 before publishing as working.

**Recorded decision, 2026-08-17: the capacity premise will not be verified.** The
user decided not to run the capacity-classification probe. Phases B and C
proceed on the premise as an **accepted, unverified assumption**: the
`--bg`-draws-the-session-pool half stays as stated above -- documentation, never
observed live -- and B2 item 1 stays specified but unscheduled. The consequence
recorded against P2, that B does not ship if the premise is wrong, is unchanged;
it is the risk this decision knowingly carries.

## Architectural assessment

Carried forward from the code-level analysis; every line citation here was
re-verified against source.

**The seam that fits is a run, not a wider backend.** `LLMBackend` is a coherent
one-call transport protocol: `complete(system, user, *, model, options) ->
LLMResponse` plus `classify_halt(exc)` (`llm/platform.py:144-161`, verified). A
background session or workflow agent does something materially different: it
receives a durable unit, may use tools, submits asynchronously, and is finalized
later. The correct structure is a backend-independent **run plane above**
`LLMBackend`: an inline driver calls `LLMBackend`; background and workflow
drivers are clients of the same durable claim/submit state. The run plane is
`LLMBackend`'s caller, not its peer.

**Three loops disagree about execution and stopping.** `run_single_pass` owns
gates, freshness, generation, and apply, and catches every `Exception` from
`generate` -- including `HaltError` -- reducing it to an error string
(`pipeline/single_pass.py:204-215`, verified), even though its docstring says the
bulk driver decides whether an error class halts
(`pipeline/single_pass.py:149-152`, verified; the type is erased before any
driver sees it). `guarded_sweep` separately catches the typed halt and stops,
computing `remaining = units[index + 1:]` -- a suffix that excludes the
triggering unit from both done and remaining (`cli/budget.py:132-145`, verified),
though `BudgetStop.unit_id` does retain the trigger's identity
(`cli/budget.py:39-55`, verified). `run_bulk` copies the sweep's result fields
into a third type (`cli/bulk.py:29-45`). There is no single truthful halt
contract, and the suffix model cannot represent concurrent completion at all.

**Durability is fragmented, so status is impossible to reconstruct.** Run truth
lives in in-memory result objects (`pipeline/single_pass.py:92-103`;
`cli/budget.py:91-106`; `cli/bulk.py:29-45`) and an in-memory `CostBudget.spent`
(`llm/platform.py:428-449`, verified). The persistent facilities nearby solve
different problems -- `ResponseCache` is cross-run answer reuse with a
non-atomic direct `write_text` (`llm/platform.py:525-546`, verified). Another
process cannot answer "how is this run going" from state the library owns.

**Parallel eligibility is not universal.** `FlatChunkStrategy` declares
independence; `GraphWalkStrategy` permits predecessor-derived context
(`pipeline/workunit.py:92-128`, verified). Drivers therefore execute an
explicitly prepared **ready wave**, never an arbitrary graph walk; a dependent
run repeats prepare/execute/finalize per wave, down to one-unit waves.

**The consumer boundary survives.** The plugin ships no console script; the
consumer owns orchestration, config, prompts, pricing, and persistence
(`plugins/content-pipeline-kit/CLAUDE.md`, "A consumer imports the library").
Both known external consumers wrote their own orchestration loops and adopted
the horizontal subsystems (`llm.platform`, `llm.backends`, freshness, stores,
validation) -- recorded in that CLAUDE.md's "What consumers adopt" section. Two
consequences drive sequencing: the loop helpers whose semantics need correction
are exactly the surface the observed consumers do not use, so those corrections
are not a prerequisite for anything; and workers in fresh processes need a
machine-facing protocol the consumer mounts on its own entry point, because
`cli.scaffold` is human-facing YAML.

## Decisions

Settled calls. An implementer does not relitigate these; changing one requires
reopening this document.

### D1. Submit-time acceptance is authoritative

**Decision.** A submission is adjudicated once, at submit time, by
`evaluate_submission` running the consumer's `parse_fn` and validators (the
logic extracted from `submit_validated`, `llm/platform.py:691-782`). The verdict
is recorded durably with the accepted text. Finalize re-parses accepted text
only to recover the payload object -- calling `parse_fn` mechanically -- and
never re-runs validators or flips a verdict.

**Reason.** Deferred finalization is order-sensitive: unit k's finalize-time
world includes units 1..k-1 already applied, a different world than the worker's
submit-time evaluation saw; validators take a `context` argument and can read
consumer state. A finalize that re-adjudicates could reject a unit that was
terminal-accepted while its worker is gone -- an unrepresentable state. The
synchronous path never had this divergence because it validated and applied in
one breath.

**Consequence for store-dependent validators.** A consumer whose validators
depend on the immediately-applied prior unit must run one-unit waves -- the
prepare/execute/finalize-per-ready-wave machinery exists for exactly this
(`GraphWalkStrategy.context_of` is the structural marker of such dependence,
`pipeline/workunit.py:100-104`). The `RunAdapter` documentation names this rule.

**Adapter contract this implies.** `parse_fn` must be deterministic and
store-independent for tracked runs, because finalize re-runs it on recorded
text. This is stated as a `RunAdapter` requirement, and adapter identity/version
is recorded in the run so an incompatible parser refuses resume rather than
guessing.

**Rejected alternative.** Finalize-time re-evaluation ("finalize re-evaluates
accepted submissions"). Rejected because it leaves the winning verdict
unspecified and creates the flipped-verdict state above.

### D2. Untracked behavior is frozen; corrected semantics are opt-in-by-store

**Decision.** The existing untracked entry points -- `run_single_pass`,
`guarded_sweep`, `run_bulk` called without a run store -- keep today's behavior
byte-for-byte until their eventual removal, including the `HaltError`-swallowed-
to-`UnitOutcome.ERROR` behavior (`pipeline/single_pass.py:204-215`) and suffix
`remaining`. The corrected semantics (typed halt stops claiming; set-based
unfinished including the trigger) exist only on the tracked path. **`call_llm`
and `submit_validated` remain first-class, fully supported untracked surfaces
indefinitely** -- they are the dominant adopted consumer surface and are not
loop helpers; no deprecation language applies to them.

**Reason.** A behavior change inside a deprecation window makes "retained for
one release" false as a compatibility promise; freezing converts the scariest
breaking change into an opt-in. And "untracked execution deprecated", read
broadly, would appear to deprecate the very functions both known consumers
build on -- the deprecation scope is exactly the three loop helpers, nothing
else.

**Rejected alternative.** Correcting `HaltError` handling in place with a
one-release warning. Rejected: the two known consumers own their loops, so the
in-place correction has low real-world payoff and high breakage optics, and it
cannot be verified against consumers with no telemetry.

### D3. Cache-key stability across the migration

**Decision.** The tracked inline driver presents to `build_cache_key` a
`backend` string identical to the one the same request produces today --
`build_cache_key` hashes `backend` into the key (`llm/platform.py:457-487`,
verified), so the driver passes through the underlying `LLMBackend.name`
(`openrouter`, `claude-cli`, `codex-cli`, `mock`) unchanged. The new lanes get
new labels, `claude-bg` and `claude-workflow`: their system-prompt semantics
differ from the one-call transports, so they must not silently share cached
answers. A regression test pins the inline-driver key equality byte-for-byte.

**Reason.** A changed `backend` string silently invalidates every consumer's
response cache on upgrade, and the corpus re-spends. Silent full re-spend is the
exact failure the cache exists to prevent.

**Rejected alternative.** A distinct `inline` label for tracked runs (cleaner
provenance). Rejected: provenance belongs in the run ledger, which records
driver and backend explicitly; the cache key is a compatibility surface.

### D4. Halt blocks claims, never valid-fence submissions

**Decision.** Setting a run halt (rate-limit, auth, operator pause) stops new
claims immediately. A submission arriving after the halt with a **valid**
fencing token is accepted and recorded exactly as if no halt existed; apply is
deferred to finalize anyway, so acceptance is always safe. A submission with a
**stale** fence is rejected regardless of halt state. Finalize decides what to
do with post-halt accepted work (normally: apply it).

**Reason.** A post-halt valid-fence submission is completed, paid-for work --
for a rate-limit halt, rejecting it wastes a full session per in-flight worker,
which is the most expensive unit of waste in the system.

**Rejected alternative.** Halt rejects all subsequent submissions ("simpler
invariant"). Rejected for the cost above; the stale-vs-valid distinction is
cheap because fencing exists anyway.

### D5. Lease semantics differ by lane, and the difference is contractual

**Decision.**

- **Inline lane:** the executing process holds the claim; leases exist but
  expiry is only a crash detector.
- **Background lane (B):** the Python dispatcher is the renewer. It renews a
  worker's lease while `claude agents --json` reports the session `working` or
  `blocked`; a `failed`/`stopped`/missing session's unit becomes reclaimable
  only after lease expiry. Identity is the Claude session ID, never the PID.
  Per P13 the reading must come from `claude agents --json` and never from
  `~/.claude/jobs/<id>/state.json`, whose own `state` field was observed
  reporting `working` for a session `agents --json` reported as `blocked` --
  which would renew a stalled worker's lease indefinitely.
- **Workflow lane (C):** there is **no renewer** -- nothing occupies the
  dispatcher role, and asking a reasoning agent to heartbeat is unreliable. A
  workflow-lane lease is therefore a whole-runtime timeout: sized to a full
  worst-case agent runtime (consumer-configured per run, since only the
  consumer knows its unit cost), with **no mid-flight reclaim inside a single
  workflow run** -- reaping runs after the workflow returns, not during it.
  Repeated expiry of the same unit **fails the unit** after a bounded attempt
  count (default 2 lease-expiry reclaims); it never loops, because a
  systematically slow unit would otherwise duplicate a full agent-session's
  spend indefinitely.

**Cost acknowledged.** A worst-case-sized lease with no renewer means a
genuinely dead workflow agent occupies its slot for the whole window before
reclaim. That is the accepted price of having no liveness channel in that lane;
the background lane, which has one, does not pay it.

**Later empirical finding, 2026-08-17 -- evidence, not a change to this
decision.** The requirement above that a workflow-lane lease be
consumer-configured per run is confirmed by measurement, and the shipped default
is shown to be actively wrong rather than merely unopinionated. One agentic unit
driven through the protocol by a background session consumed 213s of the 300s
`DEFAULT_LEASE_SECONDS` (`execution/store.py:87`) -- 71% on the cheap case, no
retry and no contention -- while that consumer's own agent timeout is 900s
(`agent_io.py:395`). Composed with this decision's workflow lane, a
healthy-but-slow unit on defaults loses its lease mid-flight, is reclaimed and
duplicated, and is then failed by the bounded reclaim count while working
correctly. The remedy is the default, tracked as A-min.4 item 2; the lane
semantics settled here stand unaltered.

**Remedy chosen, 2026-08-18: the lease is derived from the adapter, and this
decision stays GUIDANCE.** Two alternatives were rejected. Raising
`DEFAULT_LEASE_SECONDS` outright was rejected because one number cannot be right
for both lanes: the background lane has a renewer, so its lease need only outlast
a renew interval, while this lane's must cover a whole worst-case runtime.
Refusing to start a workflow-lane run without an explicit lease was rejected
because it would harden "consumer-configured per run" from the guidance it is
here into ENFORCEMENT -- a change to the substance of this decision, and not one
an implementer may make.

What ships instead: a `RunAdapter` may declare its measured per-unit COST, never
a lease, and the lane derives the lease from it (`store.lease_for`, headroom
factor a named module constant). Declaring nothing yields exactly today's 300s
with no warning -- not knowing your unit cost is not an error -- and a
declaration may only RAISE a lease, never shorten it below the current default.
An explicit mount-configured `lease_seconds` still wins outright, so the
consumer-configures-per-run path settled above remains the authoritative one.

The headroom factor rests on a single measurement, and the plan records that
rather than implying more: 213s is explicitly the cheap case, so the worst case
is unmeasured and strictly above it, and the error is asymmetric -- an undersized
lease destroys a healthy unit after two reclaims. The consumer's own 900s ceiling
for the same operation bounds it from above. A second measurement moves one
constant.

**Rejected alternative.** Worker-emitted heartbeats through the protocol's
`renew` verb. Rejected for the workflow lane (unreliable from a reasoning
agent); retained as the mechanism the *dispatcher* uses in the background lane.

### D6. `apply_unknown` fails closed; the library ships reconciliation for its own deliver modes

**Decision.** Finalize records `apply_started` before and `apply_succeeded`
after each unit's apply. A crash between them leaves the unit `apply_unknown`.
Resume with any `apply_unknown` unit **refuses to proceed** unless the adapter
provides a reconciliation hook that answers "did this apply land". The library
ships default reconciliation for its own `deliver` modes, whose marker-protected
writes make landing mechanically checkable (`deliver/inplace.py:170-391` --
**inferred** from the marker design; confirming that every shipped deliver mode
supports it is an A-min.3 implementation task, and any mode that cannot stays
fail-closed).

**Reason.** Exactly-once external side effects across a crash are unattainable
without consumer idempotency; the synchronous path has the same crash window
with zero recording. The tri-state narrows and names the window; a warning that
lets resume proceed would let a duplicate apply happen silently.

**Rejected alternative.** Warn-and-continue. Rejected: it converts a named
window into a silent one.

**Later empirical finding, 2026-08-17 -- evidence, not a change to this
decision.** A read-only scoping pass over two real consumers shows the
fail-closed floor and the "where the design supports it" hedge are both load
bearing, and that cheap reconciliation is a property of the **write shape**
rather than of the VCS involved. One consumer's apply is a deterministic
read-modify-write of a single keyed CSV row, so "did this apply land" is
answerable by comparing that row's values against the payload's expected values
-- no version control query at all, and that consumer therefore need not fail
closed. The localization consumer's apply is a whole-file rewrite and has no such
marker, so it does fail closed. Noted against the same finding: version control
open-for-edit state is **not** a usable marker, since a file can be open with
stale or partial content.

### D7. Deprecation is keyed to evidence, not to a release count

**Decision.** The untracked loop helpers are removed only after either a
confirmed migration by both known consumers or a calendar window of at least six
months from the A-cleanup deprecation release, whichever comes first --
whichever *evidence* arrives first, not whichever release ships first.

**Reason.** Releases are not adoption: the consumers are external repos with no
telemetry, and nothing establishes they run the warning release even once before
a breaking one. Under this plan's sequencing the question loses urgency --
deprecation sits in A-cleanup and gates nothing.

## Invariants

One-line contracts, enforced by A-min tests and restated in the protocol
reference:

1. `renew` and `submit` succeed only with the fencing token of the current
   claim; a stale token is rejected with a typed error.
2. Halt blocks new claims; it never rejects a submission carrying a valid
   fencing token.
3. Finalize is idempotent: given `apply_started`/`apply_succeeded` records,
   re-running finalize applies each unit at most once and refuses on
   `apply_unknown` absent reconciliation.
4. Lease expiry of a still-live worker can duplicate spend, never side effects:
   apply happens only inside serial finalize, and a fenced-out late submission
   is recorded as superseded, not applied.
5. Submit-time acceptance is authoritative; finalize never re-adjudicates a
   verdict.
6. The status digest never contains prompts, unit payloads, or full outputs.
7. A tracked inline run produces cache keys byte-identical to the untracked
   path for the same request (D3).
8. A worker verb refuses when the process environment disagrees with the
   environment recorded on the run at creation. Comparison is exact string
   equality against the run's snapshot; a declared variable that resolves to
   the same location in a different path flavour is still a refusal, and the
   error says so. An adapter that declares no environment is unaffected.

Invariant 8 is enforced at `_require_compatible_run`, the single call site every
worker verb already passes through -- the same seam as D1's adapter-version
refusal, and for the same reason: the failure it prevents is silent. A worker
whose environment differs from the one its run was created in does not error, it
resolves against the wrong root and produces plausible output. The environment
is snapshotted at `create-run`, in the orchestrator's own process, because an
adapter cannot certify its own environment: the consumer's mount constructs the
adapter from config that itself resolves through that environment, so a
worker-side self-check compares a value against something derived from it and
always passes.

## Plan

Sequencing: **A-min -> B -> C -> A-cleanup.** Every phase boundary is a
publishable release a consumer can safely sit on; no phase delivers breaking
churn ahead of the capability that motivates it. B precedes C in release order
so the protocol is exercised by the simpler process model first. Each published
sub-phase bumps `plugins/content-pipeline-kit/.claude-plugin/plugin.json` and
`pyproject.toml` together; the marketplace manifest stays generated and release
goes through `scripts/publish.py` (root `CLAUDE.md`, publish flow). No
maintainer material lands under `plugins/`.

### Phase A-min -- the run plane, additive only

No existing public surface changes behavior. A consumer that upgrades and
touches nothing observes nothing.

#### A-min.1 -- Durable run store and bounded status

**What ships.** `content_pipeline.execution` with SQLite-backed run, unit,
attempt/event, and lease records; transactional migrations; atomic claims with
expiry and monotonically increasing fencing tokens; explicit
driver/backend/model/adapter-version identity on the run; nullable usage (never
zero-for-unknown); and a bounded `RunStatus` digest -- counts by state, elapsed
time, oldest in-flight ages, expired leases, fixed-window throughput, capped
recent failure groups, and pause/halt state. It excludes prompts, payloads, and
outputs (invariant 6).

**SQLite operational posture** (settled here, not during implementation): WAL
journal mode; `busy_timeout` set on every connection (default 5000 ms);
single-writer discipline -- the dispatcher is the only long-lived writer, worker
processes write only through short claim/submit transactions; connections opened
per verb and closed, because the contention profile is many short-lived CLI
processes; and a **loud warning at store-open when the database path resolves to
a network filesystem** (UNC path or a drive whose filesystem reports remote),
since WAL on a network share is a known corruption vector. Warning rather than
refusal: path detection has false positives and the consumer chooses the path;
the warning names the risk and the fix. Retention and deletion stay outside the
library.

**Files.** Add `execution/{__init__,model,store,status}.py` and `cli/run.py`
(command adapters only -- the execution logic lives under `execution/`,
honoring the placement rule so `cli/` cohesion does not worsen). Update package
exports and `tests/content-pipeline-kit/test_package_imports.py`.

**Tests.** `test_execution_store.py`, `test_execution_status.py`,
`test_run_cli.py`: reopen/migration, legal and illegal transitions, thread and
process claim contention, stale fencing, expiry, nullable metrics, bounded
digest with failure caps, WAL/busy-timeout in effect, network-path warning, and
proof the digest never contains prompts or results.

**Exit criterion.** One fixture process holds a deliberately blocked claim while
a second process obtains a bounded digest; reopening the database preserves run
truth.

**Shippable:** yes, independently; additive. SQLite is stdlib, so no
`bootstrap.json` dependency change -- but the release still needs the version
bump, since any consumer-visible change does.

#### A-min.2 -- Prepare/finalize lifecycle and the additive inline driver

**What ships.** `prepare_run` evaluates gates and freshness, records terminal
skips, and materializes a ready wave (flat: all pending; graph: the
dependency-ready set, down to one unit). `finalize_run` recovers payloads from
recorded accepted text via the adapter's `parse_fn` (D1) and applies serially,
recording `apply_started`/`apply_succeeded` (D6). Pause/halt per D4. Unfinished
is a set -- every unit without a terminal state, holes included -- with original
ordinal retained for deterministic reporting. An **additive** inline driver
executes a prepared run at concurrency one through the store, calling the
consumer's generator or `LLMBackend`; cache keys per D3. `run_single_pass`,
`guarded_sweep`, and `run_bulk` are **not modified** -- no facades, no
deprecation, no halt-semantics change in this phase.

**Files.** Add `execution/controller.py`, `execution/wave.py`,
`execution/drivers/inline.py`. Update the build guide and domain skill
references to present the tracked API as the run-level entry point for new
pipelines.

**Tests.** Typed halt stops claiming (tracked path); trigger included in
unfinished; holes in the unfinished set; resume without replaying accepted
units; stable finalize order; one-unit dependent waves; flat readiness; refusal
of unsafe graph parallelism; D3 key-equality regression; D4 post-halt
valid-fence submission accepted, stale-fence rejected; finalize idempotence and
`apply_unknown` refusal. All existing legacy-loop tests remain untouched and
green.

**Exit criterion.** Equivalent three-unit legacy and tracked inline runs produce
equivalent applied content and identical cache keys; a forced halt leaves an
inspectable full unfinished set; resume completes it without replaying accepted
units.

**Shippable:** yes, after A-min.1; additive.

#### A-min.3 -- Worker protocol, RunAdapter, pure evaluation, cache hardening

**What ships.** A versioned JSON protocol -- `run prepare | claim | read |
submit | fail | renew | status | pause | resume | finalize` -- as mountable
handlers a consumer wires onto its own entry point (the no-console-script
boundary holds; `cli.scaffold.dispatch` remains the human-facing helper). A
`RunAdapter` protocol: reconstruct unit by ID, build a prepared request,
provide the `ValidationSpec`, apply a payload, and optionally reconcile an
`apply_unknown` (D6); adapter identity/version recorded in the run, incompatible
resume refused. `evaluate_submission(text, spec)` extracted from
`submit_validated`, which now calls it -- byte-compatible feedback and rejection
ordering, verified by existing tests. `ResponseCache.store` becomes atomic
(same-directory temp file + `os.replace`), replacing the direct `write_text`
(`llm/platform.py:542-545`). Default reconciliation for the shipped `deliver`
modes where the marker design supports it (D6).

**Security posture.** Trusted policy (adapter import path, database path) is
local configuration supplied by the consumer's own entry point; unit content is
untrusted data and never carries instructions the worker must obey. Protocol
instructions shipped in worker-facing assets must be true, checkable, and
non-suppressive (root `CLAUDE.md`, "Instructions we ship to Claude must be
checkable") -- their authority is the run descriptor and the consumer command,
never a claim of prior consent.

**Files.** Add `execution/adapter.py`, `execution/protocol.py`; extend
`cli/run.py`; change `llm/platform.py` (extraction + atomic cache); update
domain references.

**Tests.** Every verb; malformed envelopes; version incompatibility refusal;
claim fencing across subprocesses; validation/feedback parity with
`submit_validated`; Windows paths; concurrent cache writers; interrupted temp
writes; deliver-mode reconciliation; subprocess tests proving fresh invocations
share only durable state.

**Exit criterion.** Several short-lived local processes claim, read, submit,
inspect status, and finalize one run with no process-local continuity; all
existing validation and cache tests stay green.

**Shippable:** yes; A-min.2 and A-min.3 may land in one release.

**A-min live gate (before B).** A real consumer runs a small tracked OpenRouter
job end to end. Code cannot prove that a consumer's adapter reconstructs
prompts correctly, that its apply is idempotent enough for its side effects, or
that its chosen database path survives its deployment model. This gate spends
no Claude platform quota and does not depend on the capacity premise.

**Cleared 2026-08-17**, read from the run store rather than from a report. The
localization consumer mounted `glossary translate` on the run plane (a
`RunAdapter` plus a protocol mount plus a subprocess harness) and drove three
atoms through `prepare -> claim -> read -> submit -> status -> finalize` across
**19 separate OS processes** sharing only the SQLite file; translations landed;
total spend $0.00076; a second `finalize` returned an empty applied list, so
invariant 3 was observed rather than assumed. What the pass does **not**
establish, and must not be reported as establishing: apply idempotency beyond
that no-op -- this consumer's apply is a keyed read-modify-write of a sandboxed
side-copy -- and nothing at all about the Claude-session transports, which
A-min.4's light tests address separately.

#### A-min.4 -- corrections carried from the 2026-08-17 live gate and light tests

Library-level defects found by exercising A-min's shipped surfaces against two
real consumers. They are A-min work rather than B or C work because each sits in
an A-min surface (the protocol's invocation carrier, the run store's lease
default, `create-run`, the adapter contract), but items 1, 2 and 5 are
prerequisites for the B and C drivers, which spawn worker processes and would
each rediscover them.

**Status 2026-08-18.** Items 1, 3 and 4 are RESOLVED in `f114930`; items 2
and 5 remain OPEN and are the required remainder of this sub-phase. Both are
B and C driver prerequisites, so neither may be carried into B1 or C1 as an
assumption: a driver written before item 2 ships a lease default that fails a
healthy unit, and a driver written before item 5 ships a worker whose
environment is unspecified. Close both here, in A-min surfaces, rather than
twice over in each lane.

1. **The envelope's carrier, not the envelope.** **RESOLVED `f114930`.** Passing the protocol envelope
   as an argv string is unsafe on Windows when the payload contains both escaped
   quotes and a `|`: through a `.bat` wrapper, cmd.exe un-quotes the pipe and the
   wrapper dies at exit 255. YAML block scalars use `|`, so a realistically
   structured payload (~84KB observed) trips it while a one-line mapping never
   can. `one envelope in, one out` is sound; argv is the wrong carrier. Ship a
   carrier that does not re-parse: accept the envelope on **stdin** (the shape
   `cli.scaffold.dispatch` could adopt), or by `@file` path, with the argv form
   documented as unsafe behind a shell wrapper. B1 and C1 both compose worker
   command lines and both depend on this; it also interacts badly with P5's
   requirement that an allowlist cover exact command strings, since an unstable
   quoting is an unstable string.
   *Shipped:* the envelope is read from stdin (no positional argument, or an
   explicit `-`) or from `@<path>`, both decoded UTF-8 explicitly rather than
   by platform default; the argv form still works and is documented as
   discouraged. *Still unproven:* the tests call the handler in-process and
   never cross a shell, so they establish that the stdin path preserves the
   content that corrupts argv, not that the cmd.exe failure is fixed end to
   end. That needs a `.bat`-wrapped invocation carrying a block-scalar
   envelope on stdin against a consumer resolving the published shared lib --
   i.e. after a release.
2. **The default lease is wrong for an agentic unit.** **OPEN -- required.** `DEFAULT_LEASE_SECONDS`
   is 300s (`execution/store.py:87`). Measured 2026-08-17: one first-pass-dialog
   unit driven by a background session consumed 213s of it -- 71%, on the cheap
   case (no retry, no contention, a healthy session), with a 10.8KB system and
   66KB user prompt. That consumer's own agent timeout is 900s
   (`agent_io.py:395`). Under D5's workflow lane there is no renewer and repeated
   expiry fails the unit after a bounded count, so a healthy-but-slow unit on
   defaults is reclaimed, duplicated, and then failed while working correctly.
   D5's substance is unchanged -- the lease is consumer-configured per run -- but
   the shipped **default** is actively wrong for the agentic case rather than
   merely unopinionated. Resolve by one of: raising the default, refusing to
   start a workflow-lane run without an explicit lease, or deriving it from
   the adapter. Which option is chosen is not purely mechanical: refusing to
   start without an explicit lease would harden D5's `consumer-configured per
   run` from guidance into enforcement, which is decision-adjacent and wants
   the author's call rather than an implementer's. Raising the default and
   deriving from the adapter are both additive and leave D5's substance
   untouched. Whichever is chosen, the change is consumer-visible and must be
   named at publish.
3. **`create-run` accepts an unclaimable `adapter_version`.** **RESOLVED `f114930`.** It validates
   nothing against the mounted adapter's own reported identity, so a run can be
   created in a permanently unclaimable state; every later verb then fails with
   an adapter-version mismatch whose message talks about resuming, for a run that
   never ran. The D1 guard is right to refuse rather than guess; the defect is
   creatability. Compounding it, `create-run`'s stdout echoes id, driver, backend
   and model but **not** `adapter_version`, so a caller cannot see what was
   stored. Fix both: default or validate the value at creation, and echo it.
   *Shipped:* create-run validates against the mounted adapter and refuses a
   mismatch, defaults the value from the adapter when omitted, and echoes it.
   This is a real behavior change, not purely additive -- a consumer that
   deliberately passed a version its adapter does not report now gets a
   refusal -- and must be named at publish.
4. **Empty text with spent tokens is indistinguishable from no response.** **RESOLVED `f114930`.** A
   reasoning model can consume its whole output budget before emitting text and
   return `text=""` with nonzero `output_tokens`. The adapter hard-rejects it
   correctly (D1 as designed), but nothing in `LLMResponse` separates that from a
   provider returning nothing, so a caller who set `max_tokens` too low sees what
   looks like a validation bug. Surface a hint when `text == ""` and
   `output_tokens > 0`. This bites harder on the session-pool lanes, where budget
   behavior is less visible than an OpenRouter token count.
   *Shipped:* `LLMResponse` gains a defaulted `likely_reasoning_exhausted`
   field plus a predicate and a description helper, so an adapter branches on
   a value rather than parsing a log line. It is computed in `call_llm`, so a
   caller reaching past it to a backend must apply the predicate itself.
5. **A worker needs the consumer's environment, not just its database path.** **OPEN -- required.** A
   consumer can resolve its project root from an environment variable rather than
   from cwd, and on Windows may require a native-style path there; a driver that
   propagates only `--db` produces a worker that silently resolves against the
   wrong root. This is the A-min gate's database-path question one level out. The
   `RunAdapter` contract must state what environment a worker is guaranteed, and
   B1 and C1 driver design must decide it explicitly rather than letting each
   consumer discover it. Cheap to get wrong and silent when wrong.

**Shippable:** yes; additive plus one carrier addition, with the `create-run`
refusal in item 3 the one behavior change to name. Items 2 and 5 land before
the B and C drivers.

### Phase B -- Claude background sessions

**Framing correction, 2026-08-17: the transport is established before the
driver exists.** This phase was written on the assumption that the background
transport is first exercised by B1's driver and first judged at the B2 gate. It
was instead exercised directly, because A-min.3's protocol is mountable and
process-agnostic: a real `claude --bg` session claimed a unit, read the prepared
request, produced the answer **as its own agent output** -- the Claude agent was
the model, with no external API call -- and submitted it, across separate
subprocess invocations of a consumer's protocol mount, unattended (P14). Two
consumers were driven this way, one of them a genuinely agentic unit whose
freehand output parsed and validated through a parser written against a
different harness.

The consequences for this phase, stated exactly:

- **B1 is a convenience layer over a proven transport**, not the step at which
  the transport is found out about. Dispatch, slot bounding, reconciliation,
  lease renewal and classification are still entirely unbuilt and unexercised.
- **B2's gate is narrower than written, not discharged.** These were
  **transport-only** light tests: no cell exercised an apply with a real VCS side
  effect, the first-pass-dialog cells were scoped to claim/read/submit with no
  `apply` and no `finalize`, and the localization cells applied only to a
  sandboxed side-copy. **Apply idempotency against a real external side effect
  remains untested and is the one question no light test answered.** Nothing was
  established about concurrency at N > 1, reclaim after expiry, fenced late
  submission, authentication failure, halt classification from a settled session,
  or permission posture beyond the single machine's `auto` default (P5).
- **The gate consumer choice is settled by evidence.** first-pass-dialog was
  excluded from the A-min gate for spending Claude quota by construction; it is
  the natural B gate consumer, since a background session is its transport
  anyway, and it is the one that stresses apply idempotency against a version
  control side effect.
- **A-min.4 items 1, 2 and 5 are prerequisites**: the driver composes worker
  command lines (item 1), sizes leases for agentic units (item 2), and decides a
  worker's environment (item 5).

#### B1 -- Background driver and one-unit worker

**What ships.** `execution/drivers/claude_bg.py`: capability and auth preflight
(fails closed when API/cloud credential variables are active, since those would
route billing away from the subscription pool); bounded dispatch with at most N
occupied slots; launcher election via a run-level lease so an accidental second
dispatcher exits without launching; session-ID reconciliation from
`claude agents --json` built **schema-tolerant** -- unknown fields ignored,
required fields validated loudly. Per P4 the reconciler **filters
`kind == "background"` first** (the listing includes interactive sessions,
including the orchestrator's own) and then requires `kind`, `id`, `sessionId`,
`state`. `pid` and `status` must never be required of a worker, and -- correcting
the first probe's reading -- neither may serve as a background-versus-interactive
tell: a background record observed on 2026-08-17 carried `pid`, `status`, and
`waitingFor` alongside `id` and `state` (P4). `kind` is the only discriminator.
`waitingFor` is read where present, because `waitingFor: "permission prompt"` is
what separates a permission stall (P5) from the question stall of P12; it is
treated as an optional field, since it and `status` were each seen on a single
record. Settled workers are visible only under `--all`, and
`startedAt` is epoch milliseconds. Lease renewal per D5; typed failure mapping
reusing `classify_halt_text` markers; pause/resume. Two corrections that follow
from the first 2026-08-17 probe and are load-bearing rather than cosmetic: command
construction uses the **top-level** verbs `claude stop|logs|rm|respawn <id>`,
never `claude agents <verb> <id>`, which exits 0 having done nothing (P3), so the
preflight asserts each verb behaves rather than trusting an undocumented surface;
and `claude logs <id>` is a **live-daemon-only** channel (it fails with
`connect ENOENT \\.\pipe\cc-daemon-*-control` once the daemon has exited), so
halt-text classification for a settled unit reads the session transcript or the
per-job state -- the latter for its text fields only, per P13 -- and not
`claude logs`. Per P11 the launcher's exit code and banner are
never evidence a worker started: every dispatch is confirmed by polling
`agents --json` for a transition out of the initial state, and a `failed` within
the first seconds is classified as launch misconfiguration rather than work
failure. `~/.claude/jobs/<id>/state.json` (carrying `needs`, `detail`,
`output.result`, `tokens`) and `~/.claude/daemon.log` are richer than
`agents --json` but are undocumented internal state; they may be read as
**optional enrichment behind a tolerant parse**, never as the required surface.
For status specifically the constraint is stronger than that, and it is a
correction rather than a caution: per P13, `state.json`'s own `state` field was
observed reporting `working` for a session `agents --json` reported as
`blocked`, so **no decision about whether a worker is progressing may read
`state.json`**. Status detection keys on `agents --json` only. `state.json`
remains readable for the `needs` prompt text, which `agents --json` never
exposes at all, and for nothing that drives lease renewal, reclaim, or halt.
Worker assets: `agents/pipeline-worker.md` and
`skills/execute-work-unit/SKILL.md` --
one agent claims one unit, reads its prepared request through the consumer
command, produces an answer, submits through the same command, revises from
compact rejection feedback until accepted or exhausted, and exits. The launch
prompt carries only run and unit identifiers; unit content never appears in
command lines or the orchestrator's context. **The launch prompt also names the
exact invocations the worker may run**, and in particular constrains result
submission to one exact pre-allowlisted consumer-command invocation rather than
describing a desired outcome and leaving the worker latitude in how to reach it.
This is a B1 prompt-design requirement, not consumer setup advice: the
2026-08-17 probe stalled on a shell redirect (`echo ... > file`) that the worker
composed itself to satisfy an instruction phrased as an outcome, and no
allowlist author would have enumerated it (P5). An orchestration skill
(`skills/background-pipeline/SKILL.md`) drives prepare, dispatch, status at
batch boundaries, and finalize. `max_agents` (default 4) and `batch_size`
(default 25) are configurable -- both pass the plugin-opinion razor: protecting
interactive quota versus maximizing throughput is a genuine power-user
preference. Storage engine, fresh-per-unit contexts, and single-dispatcher
election are correctness decisions and earn no settings.

**How the supervisor learns quota** (settled; there is no programmatic API for
session-pool remaining capacity, but per P9 there is an observable): the
batch-boundary decision is made from (a) the bounded status digest, and (b)
whatever the live orchestrating session can observe -- `/usage`, or the
status-line rate-limit snapshot claude-ui-kit mirrors to
`~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json`, which is a
documented file contract rather than an import edge. That snapshot is
whole-percent granular and refreshes only while an interactive session renders a
statusline, so it is coarse foresight, not a meter a driver can steer by. The
**fallback, which is also the guaranteed-correct path, is reactive**: a
rate-limit halt classified from a worker failure sets
the run halt, in-flight work settles under D4, and the run parks durably as
"resume when replenished". Quota foresight is an optimization; the halt path is
the contract.

**Files.** Driver, worker agent, two skills, a recipient reference under
`skills/content-pipeline-domain/references/`, exports, docs. llm-scripting-kit
changes only if B2 disproves its billing comments.

**Tests.** Fake `claude` executable: command construction, including that the
lifecycle verbs are emitted top-level and never as `claude agents <verb>`; launch
parsing; a launcher that exits 0 on a doomed dispatch, proving the driver waits
for an observed state transition; agents-JSON reconciliation including
`kind`-filtering an interactive record out of the listing, unknown-field
tolerance, missing-required-field loudness, and a background record carrying
`pid`/`status`/`waitingFor` still reconciling as a worker; a fixture in which
`~/.claude/jobs/<id>/state.json` reports `working` while `agents --json` reports
`blocked`, proving the driver classifies the worker as stalled and stops
renewing its lease; session-ID-not-PID identity;
strict N; duplicate-launch suppression; blocked/missing/stopped/done-without-submit
states; expiry and reclaim; fenced late submission recorded as superseded;
post-halt valid-fence acceptance; failure classification; no unit content in
argv. No automated test consumes live quota.

**Exit criterion.** At N=2, two units run concurrently against the fake;
killing one worker causes reclaim after expiry; its late submission is fenced;
a hard halt quiesces per policy; resume completes only unfinished work; the
A-min finalizer applies all accepted output.

**Shippable:** the code may be committed and version-bumped, but is **not
published as working before B2 passes**.

#### B2 -- Live platform qualification (release gate)

No planned architecture; narrowly scoped fixes and recorded public constraints
only. Named pass/fail items:

1. **Capacity classification probe (make-or-break). Specified but not
   scheduled, by the recorded 2026-08-17 decision not to verify P2.** Phases B
   and C proceed on the capacity premise as an accepted, unverified assumption,
   so this item no longer gates the release and the quota spend below is not
   being asked for. The specification is kept intact, unexecuted, because it is
   what a later decision to verify would run. Confirm live that `--bg` work draws the
   session pool and not the headless allowance. A measurement method exists as
   of the 2026-08-17 probe (P9) and did not before: read
   `~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` from an
   attended interactive session, run the batch, read again. The instrument is
   whole-percent granular, so the batch must be large enough to move the
   five-hour pool by several percentage points to clear granularity and ordinary
   drift -- that is the cost, and there is no cheaper version of it. The
   supervising interactive session must stay alive and active throughout,
   because only a rendering statusline refreshes the file, and the comparison arm
   (the same work under `-p`) needs a separate five-hour window with no other
   heavy work in it, or the delta is not attributable. The cheaper alternative
   formulation -- exhaust the headless allowance under `-p`, then run one `--bg`
   unit and observe it proceed -- spends less but inherits P1, which rests only
   on the user's observation. **The ask this item carries to the user is
   authorization to spend a measurable batch across two clean windows.** Fail =
   the premise is wrong and B does not ship; the plan returns to the user with
   the evidence.
2. **Permission-mode behavior (make-or-break, with fallback). Executed
   2026-08-17; verdict PARTIAL, and the fail criterion recorded here was
   incomplete.** A `--bg` session is interactive-supervised, not `-p
   --permission-mode bypassPermissions`. The flag surface was settled by the
   first probe (P5). The behavioral half was then run as two worker-shaped `--bg`
   sessions, both stopped and removed afterwards, with these results:
   - **Settings inheritance is observed, not inferred.** Under `--permission-mode
     manual`, allowlisted `Read` and `Bash(ls:*)` ran unattended inside the `--bg`
     session while a non-allowlisted write blocked -- a clean discrimination.
     The entries that proved it are **user-scope**; project-scope
     `permissions.allow` inheritance remains **inferred** from that result,
     because exercising a project entry would have mutated the repository.
   - **The stall is real.** Under `manual` the session stalled with nothing
     timing it out.
   - **The pass arm is not load-bearing on its own.** The flagless arm completed
     an unallowlisted Bash write unattended, but this machine's user
     `settings.json` carries `"defaultMode": "auto"`, and the command was
     harmless. `auto`'s behavior on a command it classifies as dangerous is
     **untested**; `auto` is plausibly a risk classifier rather than a blanket
     allow, and a worker running arbitrary consumer commands will eventually
     cross that threshold, where the stall would look exactly like the `manual`
     arm. `auto` is not a general solution and the plan does not rely on it.
   - **The fail criterion this item previously recorded was incomplete.** It read
     "`agents --json` shows `state: "blocked"` with a permission question in the
     job's `needs`", but `agents --json` exposes no `needs` field anywhere in
     `--all` output, so a fail cannot be stated in terms of it. The corrected
     criterion: fail = `agents --json` reports the
     background record as `state: "blocked"` with `status: "waiting"` and
     `waitingFor: "permission prompt"`. The prompt text is available only from
     `~/.claude/jobs/<id>/state.json`, which per P13 may be read for that text
     and for nothing that determines progress.
   P12 makes the stakes concrete and was re-confirmed live by the second probe:
   the session blocked since 2026-07-29 is still blocked, with nothing timing it
   out, so a stalled worker is held only by our own lease.
   **Fallback design, shipped as documented consumer setup:** a pre-authorized
   allowlist covering the consumer command invocations the worker performs. The
   mechanism is confirmed sound -- allowlist entries genuinely are honored inside
   `--bg` -- but the fallback as previously written was incomplete on two points:
   - The allowlist must cover the worker's actual command **strings**, not command
     families. The 2026-08-17 stall was on a redirect the worker composed itself.
     The mitigation is therefore in B1's prompt design (see B1), not in setup
     documentation alone.
   - "Verified by that single-unit probe before any batch" does not generalize:
     unit 1 exercises only unit 1's commands. Re-framed as **verification against
     the full enumerated invocation set**, or -- the cheaper option, since the
     lease design already exists -- accept a mid-run permission stall as an
     expected event and define its reclaim path.
3. **Worker registration. Settled, reduced to a composition check.** Both halves
   confirmed 2026-08-17 (P6): background sessions load plugin skills, and
   `--append-system-prompt-file` / `--system-prompt-file` / `--agent` / `--agents`
   exist. What remains is verifying those flags actually compose with `--bg`
   rather than being silently dropped -- and per P11 that must be judged by
   observing the worker's behavior, not the launcher's exit code. This was to
   fold into the item-2 single-unit probe; the 2026-08-17 probe that executed
   item 2 launched neither flag, so the composition check is still **open** and
   needs its own single-unit launch. Still to be recorded either way: what
   exactly is in the launch prompt.
4. **Verbatim `agents --json` schema. Settled** as of 2026-08-17 (P4): the
   `kind` discriminator, the two field sets, and the presence of interactive
   records are read verbatim, and the reconciler's required-field list is fixed
   to `kind`, `id`, `sessionId`, `state` behind a `kind == "background"` filter.
   Amended 2026-08-17 by a second probe: a background record was read carrying
   `pid`, `status`, and `waitingFor` alongside `id` and `state`, refuting the
   field-set exclusivity recorded from the first probe, so `kind` is the sole
   discriminator and the optional `waitingFor` is what tells a permission stall
   from a question stall. The same probe fixes the **channel**, which is a
   contract term and not merely a schema one: `agents --json` is the authoritative
   status surface and `~/.claude/jobs/<id>/state.json` is not (P13). The residue
   is not a schema question but a stability one: the lifecycle verbs
   (P3) are hidden from `--help`, which probing cannot settle, so B1's preflight
   carries a version-drift check instead.
5. Authentication-failure behavior, concurrency enforcement, and a full
   lifecycle: two live units, one validation rejection/revision, one deliberate
   worker kill and reclaim, pause/resume, and a status query from a fresh
   orchestrator context.
6. **Apply through finalize, against a consumer with a real external side
   effect.** Added 2026-08-17 because the light tests of P14 covered the
   transport only: no cell exercised an apply with a version control side
   effect, so finalize's `apply_started`/`apply_succeeded` recording, D6's
   fail-closed behavior, and reconciliation have never been observed against
   anything but a sandboxed side-copy. This is the gate item the transport
   evidence does not touch, and it is why first-pass-dialog is the B gate
   consumer.

**Exit criterion.** A main agent supervises via occasional bounded digests,
identifies a failure pattern, alters N or a batch, and halts safely without
ingesting unit content. Item 2 passes on its corrected criterion, or the phase
stops and reports. Item 1 no longer gates the exit, by the recorded 2026-08-17
decision not to verify P2; the premise it would have tested is carried
unverified instead. Item 6 is the substantive remainder: what P14 established is
that a background session can carry a unit through the protocol, and the gate
must now prove the parts around that -- bounded concurrent dispatch, reclaim,
halt, and an apply whose side effect leaves the world outside the run store.

### Phase C -- Workflows

**Framing correction, 2026-08-17: this transport is established too, and two
constraints on C1 arrived with it.** A real Workflow agent claimed a unit, read
the prepared request, produced the answer as its own output, and submitted it;
for the localization consumer `finalize` then applied it. Both consumers were
driven this way (P14). The same boundary applies as in Phase B: these were
**transport-only** light tests, no cell exercised an apply with a real VCS side
effect, and apply idempotency against such a side effect remains untested.

Three findings bind C1's design rather than C2's gate:

- **The Workflow tool is invocable only from a top-level session** (P15). A
  workflow-lane driver cannot be invoked from inside a worker or a nested agent,
  so whatever orchestrates a wave is the top-level session itself. C1 must not be
  designed around a driver that spawns its own workflow.
- **Workflow agents have no `auto` permission mode** (P16): they run fixed at
  `acceptEdits` and inherit the launching session's allowlist. The background
  lane's clean unattended run is therefore not evidence about this lane, even
  though one workflow agent did in fact run the consumer protocol commands
  unattended. The invocation-string discipline P5 imposes on B1's prompt design
  applies here with equal force.
- **The default lease is wrong for this lane specifically** (A-min.4 item 2):
  the workflow lane is the one with no renewer, so a 300s default against a
  measured 213s agentic unit is the composition D5 already warns about.

#### C1 -- One reviewed independent-wave workflow

**What ships.** One static workflow (`workflows/run-ready-wave.js`) that
executes an already-prepared **independent** ready wave through the A-min
protocol, plus its invocation skill. Lane construction is deterministic and
restated here as normative: with `N = min(max_agents, 16, pending)`, lane k
sequentially processes ordinals k, k+N, k+2N, ... of the wave; lanes run
concurrently, so at most N fresh agent contexts exist, no long-lived worker
accumulates unit content, and resume replay -- which is ordering-sensitive --
sees a deterministic structure. Waves larger than N are handled by exactly this
lane loop; there is no other loop shape. The script uses no `Date.now()` or
`Math.random()`; batch identity comes from Python. Each agent claims one unit,
works through the consumer command, and returns a schema-validated
acknowledgement; the workflow returns one aggregate object. It owns no
persistence, validation, merging, or finalization. Leases per D5's workflow
lane: worst-case-runtime sizing, no mid-flight reclaim, repeated expiry fails
the unit. Native workflow resume is never pipeline durability -- SQLite remains
the cross-session source of truth.

**Files.** `workflows/run-ready-wave.js`, invocation skill, workflow-facing
reference, `tests/content-pipeline-kit/test_workflow_contract.py`. Nothing
moves from or to workflow-kit: plugin boundaries are hard boundaries.

**Tests (static + harness).** Metadata/args shape; deterministic lanes;
forbidden nondeterminism; concurrency bound; one claim per agent; aggregate-only
return; no unit content in workflow variables; every state mutation through the
protocol.

**Exit criterion.** The workflow is a thin recipient: deleting it leaves run
truth intact; an inline or background process can inspect, resume, and finalize
the same run.

**Shippable:** after A-min, independent of B in code; B stays first in release
order. Not published as working before C2.

#### C2 -- Live workflow qualification (release gate)

Two live units; a validation revision; an expired claim failing the unit per
D5 after the bounded reclaim count; pause/resume across orchestrator
exit/re-entry proving SQLite authority; aggregate return shape; concurrency
matching the bound; confirmation that workflow `agent()` calls draw the session
pool (the C-side capacity check); comparison of worker instruction fidelity
against the background lane. Added 2026-08-17: an apply through `finalize`
against a side effect outside the run store, for the same reason as B2 item 6 --
P14's light tests were transport-only and no cell exercised an apply with a real
VCS side effect. Exit: observed behavior matches the durable protocol and the
workflow returns no accumulated content. What P14 already establishes -- that a
workflow agent can carry one unit through the protocol -- is no longer part of
what this gate must prove; the lane construction, the concurrency bound, the
lease-expiry path and the apply are.

### Phase A-cleanup -- legacy corrections, after the capability

Ships only after B (and normally C) are live-qualified.

**What ships.** (1) Tracked-mode halt correction formalized as the documented
contract; untracked loop helpers gain deprecation warnings pointing at the
tracked API, with behavior frozen per D2. (2) Migration notes naming the exact
`[trigger] + remaining` resume workaround: `BudgetStop.unit_id` carries the
trigger today (`cli/budget.py:39-55`), so a consumer resuming with
`[trigger] + remaining` gets complete coverage under the old model but will
**double-process the trigger** if pointed at the set-based unfinished view,
which already includes it. (3) `cli/` cohesion: legacy run-control logic
relocates under `execution/` with tested aliases preserved. (4) Documentation
drift corrections as ordinary maintenance: `llm/__init__.py` and the build
guide advertising three transports while Codex is routed, and the
overconfident flat-zero billing comments in both plugins' Claude-CLI backends.
(5) Removal of the untracked loop helpers only on D7's evidence key. (6)
`Gate`/`run_gates` relocation: `pipeline/single_pass.py` is the module this
phase's halt/resume corrections and untracked-loop deprecation (items 1 and 5)
churn hardest, and it is also where `Gate`/`run_gates` live today, imported
directly (not re-derived) by `execution/controller.py:147` -- verified the
only importer outside `single_pass.py` itself. The direct-import reasoning in
`controller.py`'s "The gate seam" docstring stays correct as stated: it argues
against redefining an equivalent local shape, not against relocating the one
true shape. So `Gate` and `run_gates` move to a leaf module,
`pipeline/gate.py`, with `single_pass.py` re-exporting both names as aliases
so `controller.py`'s import and any consumer's existing gate list keep
resolving unchanged; the aliases are tested, per the compatibility-alias
convention above. Not done earlier: the seam is correct as-is under A-min/B/C,
and moving it before this phase's churn begins would be relocation for its
own sake.

**Shippable:** each item independently; none gates B or C.

## Platform assumptions

Every Claude Code behavioral claim this plan depends on. None was verified by
reading platform source; "docs" means code.claude.com documentation, in some
cases read through a summarized fetch rather than verbatim.

The `Established by` column distinguishes **direct observation** (a named command
run on a named date, or a file read on this machine) from **documentation** and
from **inference over an observation**. That distinction is the point of the
table: several rows below were doc-derived, read as settled, and were refuted the
first time anything was run. A read-only CLI probe on 2026-08-17 against Claude
Code 2.1.233 (the plan was written against 2.1.232) exercised P3-P6 and P9 and
surfaced P11-P12; it modified no repository file, and its three accidental
trivial background sessions were stopped and removed. A second probe on
2026-08-17, against the same version, executed B2 item 2: it launched two
worker-shaped `--bg` sessions, stopped and removed both, left the repository
unmodified, settled the behavioral half of P5, corrected P4, and surfaced P13.
P2, the load-bearing premise, was left untested by the first probe and, by a
recorded user decision of 2026-08-17, is not to be tested at all.

Four **transport light tests** on 2026-08-17 -- two consumers crossed with the
background-session and Workflow transports, each result read from the run store
rather than from an agent's report -- added P14-P16. They were scoped
**transport-only**: no cell exercised an apply with a real version control side
effect, the first-pass-dialog cells stopped at submit with no `apply` and no
`finalize`, and the localization cells applied only to a sandboxed side-copy, so
apply idempotency against a real external side effect stays untested and is the
one question none of them answered. They also touched P2 not at all: no capacity
attribution is observable from them, and none was sought.

| # | Assumption | Established by | Tested by | If wrong |
|---|---|---|---|---|
| P1 | `-p` draws a small headless allowance, then metered credits | User's direct observation; docs conflict (Help Center says the separate credit was paused) | Not directly tested; it is the premise. B2 item 1 would test its actionable half, but that item is unscheduled per the recorded 2026-08-17 decision on P2 | The effort loses its motivation; A-min still stands on its own |
| P2 | `--bg` sessions draw the interactive session pool | Docs (`agent-view`, partly summarized fetch); **never observed**. The 2026-08-17 probe neither supported nor contradicted it: no batch was run, and a bg transcript's `usage` blocks carry no pool-attribution field (regex sweep of a 976-record bg transcript for `rate\|limit\|quota` found only tool parameters) | **Not to be tested. Recorded user decision, 2026-08-17: P2 will not be verified**, and Phases B and C proceed on the capacity premise as an accepted, unverified assumption. B2 item 1 stays specified and a measurement instrument exists as of 2026-08-17 (P9), but neither is scheduled and exercising the probe would cost a measurable batch. This row therefore stays **unverified** for the life of the plan, and the consequence in the next column is the risk knowingly carried | B does not ship; escalate with evidence |
| P3 | `claude --bg` surface: positional prompt, prints short session ID, rejects `-p`; `claude agents --json` exists; the lifecycle verbs are **top-level** `claude stop\|logs\|rm\|respawn\|attach <id>`, hidden from `claude --help` | Observed 2026-08-17 on Claude Code 2.1.233: `claude --help` (positional prompt, `--bg`), `claude --bg -p "..."` (refused with a verbatim conflict message, no spawn), a `--bg` launch banner (`backgrounded * a47add3f`, 8-hex id), `claude agents --help` (no `Commands:` section), and `claude <verb> --help` for each verb. The earlier `claude agents <verb>` shape was doc-derived and is **refuted**: `claude agents stop\|logs\|rm\|respawn\|list --help` each print the plain `agents` help and **exit 0**, ignoring the positional | B2 items 3-5; B1 preflight asserts the verbs behave, since they are undocumented and carry no stability guarantee | Driver command construction changes; contained in B1. The refuted form fails **silently at exit 0**, so the preflight is not optional |
| P4 | `agents --json` records are discriminated by `kind`: `kind: "background"` carries `id`, `cwd`, `kind`, `startedAt` (epoch ms), `sessionId`, `name`, `state`, and may also carry `pid`, `status`, and `waitingFor`; `kind: "interactive"` carries `pid`, `cwd`, `kind`, `startedAt`, `sessionId`, `name`, `status`. `kind` is the only discriminator; the field sets are not mutually exclusive. `--all` adds settled background sessions | Observed 2026-08-17: `claude agents --json --all` output read verbatim, one record of each kind. **Refutes** the previously recorded five-field shape (`id`, session id, `pid`, `state`, `status`), which came from a summarized doc fetch and did not record the `kind` discriminator at all. `state` values seen: `blocked`, `failed`, `done`, `stopped`. A second probe on 2026-08-17 **refutes this row's own earlier exclusivity claim** ("No record carries both `id`/`state` and `pid`/`status`"): a background record was read verbatim carrying `pid`, `id`, `status: "waiting"`, `waitingFor: "permission prompt"`, and `state: "blocked"` together. `status` and `waitingFor` were absent from the first probe's recorded field set; `waitingFor: "permission prompt"` is the discriminator that separates a permission stall (P5) from a question stall (P12). Each was seen on **one record**, so that they are stable schema fields is **inferred, not observed**. Also observed: no `needs` field appears anywhere in `--all` output (a grep over the full output returned zero matches) | B2 item 4; reconciler is schema-tolerant from the start and filters on `kind` first | Reconciler required-field list adjusts; loud validation prevents silent misreads. Omitting the `kind` filter makes the orchestrator's own interactive session look like a worker, and discriminating on the presence of `pid`/`status` instead of on `kind` misclassifies a background record |
| P5 | `--bg` permission behavior (flag or settings inheritance); workers can avoid unattended permission prompts | Flags observed 2026-08-17: `--permission-mode <mode>` (choices `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`) composes with `--bg` (two sessions spawned), `claude agents` exposes `--permission-mode`/`--dangerously-skip-permissions`/`--allow-dangerously-skip-permissions`/`--settings`/`--setting-sources` as dispatch defaults, and a real bg session recorded `--permission-mode auto` in its respawn flags. **Behavioral half executed 2026-08-17 by a second probe; verdict PARTIAL.** OBSERVED: under `--permission-mode manual`, allowlisted `Read` and `Bash(ls:*)` ran unattended inside a `--bg` session while a non-allowlisted Bash write blocked, so **user-scope `permissions.allow` inheritance is observed, not inferred**; the blocked session stalled with nothing timing it out; a flagless `--bg` session completed the same non-allowlisted Bash write unattended on a machine whose user `settings.json` sets `"defaultMode": "auto"`. INFERRED, and flagged as such: that **project-scope** `permissions.allow` is inherited (only user-scope entries were exercised, since a project entry would have mutated the repository); that the flagless pass is caused by `defaultMode: auto` rather than by `--bg` never prompting. NOT TESTED: `acceptEdits`, `dontAsk`, `bypassPermissions`, `plan`, and -- the most consequential gap -- whether `auto` stalls on a command it classifies as dangerous. The probe's write was harmless, `auto` is plausibly a risk classifier rather than a blanket allow, and such a stall would be indistinguishable from the `manual` arm, so **`auto` is not established as a general solution**. Amended 2026-08-17 by the transport light tests (P14): a `--bg` session ran a consumer's real protocol invocations unattended with no stall, which widens the evidence from the probe's synthetic commands to actual worker commands but **not** past the `auto` case -- that session also recorded `permission-mode: auto`, this machine's default, so a consumer whose default is stricter is still untested, and so is whether `auto` itself stalls on a command it classifies as dangerous | **B2 item 2, executed; the fallback carries the lane.** Two revisions follow: the allowlist must cover actual command strings (the stall was on a redirect the worker composed itself, which becomes a B1 prompt-design requirement), and single-unit verification does not generalize to a batch | Lane stalls `blocked`; fallback is documented consumer allowlist setup plus a constrained worker prompt, and the flags that fallback needs are confirmed to exist and compose. Detection of the stall must key on `agents --json` (P13) |
| P6 | Background sessions load plugin agents/skills; a per-unit trusted system channel exists (`--append-system-prompt-file` or equivalent) | **Confirmed** 2026-08-17 on both halves. Channel: `--append-system-prompt-file` and `--system-prompt-file` parse-probed (each errored with `argument missing`, so the option exists); both are hidden from the `--help` option list but named in `--bare`'s help text. Also present: top-level `--agent <agent>`, `--agents <json>`, and `claude agents --plugin-dir`. Loading: a background-session transcript (system records carrying `"sessionKind": "bg"`) used the `Skill` tool with plugin-namespaced skills (`awesome-kit:task`, `bootstrap:bootstrap`, `git-kit:git-code-review`, several `skills-kit:*`). That these flags compose with `--bg` is **inferred from `--permission-mode`'s behavior, not observed** | B2 item 3, reduced to preflighting composition -- and per P11 that must be judged from worker behavior, not the launcher's exit code | Worker persona delivery redesigned inside B1 |
| P7 | Workflow limits: 16 concurrent agents, 1000/run; in-session-only resume with ordering-based cache invalidation; `agent()` accepts a JSON schema; no per-agent wall-clock timeout | Docs (`workflows`) | C2 | Lane construction constants and lease sizing change; contained in C1 |
| P8 | Live-session workflow `agent()` calls count as ordinary session usage | Docs (`workflows`, `costs`) | C2 capacity check | C does not ship |
| P9 | No programmatic *API* exposes remaining session-pool capacity, but a supported *observable* does: `.rate_limits` in the statusline hook payload, which claude-ui-kit mirrors to a documented file contract at `~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` | **Refutes** the earlier "search of docs found none", whose counter-evidence was inside this repository. Observed 2026-08-17: `plugins/claude-ui-kit/scripts/statusline.sh` reads `.rate_limits.five_hour.used_percentage`, `.seven_day.used_percentage`, and both `resets_at`, and states in comment that the statusline hook payload is the only place Claude Code surfaces `.rate_limits`; `claude-ui-kit/skills/statusline/references/components.md` documents the fields; the live snapshot file on this machine held `five_hour.used_percentage: 10`, `seven_day.used_percentage: 8`, `captured_at` 2026-08-17. Two observed caveats: granularity is whole percent, and only an interactive session rendering a statusline refreshes the file (whether a `--bg` session refreshes it is unverified) | Instrument for B2 item 1; B1's reactive-halt fallback stays the contract either way | Quota foresight improves; nothing breaks. If the observable proves too coarse or too stale, B2 item 1 falls back to the exhaust-the-headless-allowance formulation |
| P10 | A saved workflow under `claude -p /name` has unproven capacity classification | Docs silent | Not tested; the path is rejected (see "Explicitly not doing") | Irrelevant unless the path is revisited |
| P11 | `claude --bg` backgrounds the session **before validating its own flags**: it prints the success banner and exits 0, and a bad flag surfaces only asynchronously as `state: "failed"` in `agents --json` and as a settle line in `~/.claude/daemon.log` | Observed 2026-08-17: `claude --bg --permission-mode bogus "x"` printed `backgrounded * d7217a10` and exited 0; `~/.claude/daemon.log` then recorded `bg settled d7217a10 (crashed): exit 1 before init -- error: option '--permission-mode <mode>' argument 'bogus' is invalid`; `claude agents --json` later showed that id as `state: "failed"`. Second instance of the hazard the `codex_dispatch_is_silent_on_failure` insight records for codex dispatch -- judge a dispatch by its observable outcome, never by `$?` | B1 startup preflight and B2 item 5: every launch is confirmed by polling `agents --json` out of the initial state, and a `failed` inside the first seconds is classified as launch misconfiguration, not work failure | If some launches do validate synchronously after all, the conservative reading costs only an extra poll per launch. Trusting the exit code instead would make a misconfigured batch indistinguishable from a running one |
| P12 | A `--bg` session can sit in `state: "blocked"` indefinitely with nobody attached; no platform lease, deadline, or auto-fail was observed | Observed 2026-08-17: `claude agents --json --all` listed a background session in `state: "blocked"` since 2026-07-29 -- 19 days -- and its `~/.claude/jobs/<id>/state.json` carried an unanswered question in `needs`. That block was a question rather than a permission prompt, so it demonstrates the failure geometry, not the permission-prompt case itself (which is P5). Re-confirmed live by a second probe on 2026-08-17: the same session is still `blocked`, still with nothing timing it out. The permission-prompt case was separately observed by that probe and stalled likewise (P5) | Standing; it is why D5 leases and reclaim exist. B2 item 5 exercises reclaim live | Makes the lease-and-reclaim design **required**, not defensive. If a platform-side timeout does exist and was merely not observed, the lease is redundant but harmless |
| P13 | `claude agents --json` is the authoritative status channel for a background session; `~/.claude/jobs/<id>/state.json` can disagree with it and under-report a stall, and must never drive a status decision | Observed 2026-08-17: for one background session, `agents --json` reported `state: "blocked"`, `status: "waiting"`, `waitingFor: "permission prompt"` while `~/.claude/jobs/<id>/state.json` simultaneously and persistently reported `state: "working"` with a non-null `needs`, still reading `"working"` at stop time. Separately observed on the same probe: `agents --json` exposes no `needs` field anywhere in `--all` output, so the prompt text exists only in the internal file. Whether the disagreement is a lag, a different field meaning, or a defect is **not established** -- only that the two channels disagreed about one session at one time | Standing constraint on B1's reconciler and on D5 lease renewal; B2 item 4 records the channel choice alongside the schema. B1's tests carry a disagreeing-channel fixture | A reconciler keying on `state.json`'s `state` scores a permanently stalled worker as healthily progressing and renews its lease forever -- silent, and the worst failure mode available. Reading `state.json` for the `needs` text only, behind a tolerant parse, stays safe |
| P14 | Both session-pool transports can carry a prepared unit through the A-min worker protocol unattended, in fresh processes sharing only the run store, with the Claude agent itself acting as the model (it reads the prepared request and produces the output, with no external API call) | Observed 2026-08-17 across four light tests -- two consumers (a one-line glossary mapping and a genuinely agentic dialog unit with a 10.8KB system and 66KB user prompt) crossed with `claude --bg` and the Workflow tool -- each verified from the run store, not from an agent's report. Established with them: the fencing token survives a round trip through a separate session; agent-authored freehand output passed a `parse_fn` written against a different harness; `claude --bg -p` is a hard usage error, the prompt goes positionally (re-confirming P3). **Scope is transport-only**: no cell exercised an apply with a real version control side effect, the dialog cells stopped at submit with no `apply` and no `finalize`, and the glossary cells applied only to a sandboxed side-copy. Nothing was observed about concurrency above one worker, reclaim, halt, or authentication failure | B2 items 5-6 and C2, which now test the machinery **around** the transport rather than the transport | Observed, so the exposure inverts: what is unproven is the driver machinery and the apply, not the transport. If the result fails to generalize past one worker, B1's bounded dispatch and C1's lane construction are where it would show, and both are gated |
| P15 | The Workflow tool is invocable only from a top-level session -- not from a subagent | Observed 2026-08-17 by a light test that tried and could not: `Workflow` is absent from a subagent's live tool list and from the full deferred-tool index. The test refused to substitute an ordinary agent call and label it a workflow | C1 design (normative, not a gate item); C2 exercises the resulting shape | A workflow-lane driver cannot spawn its own workflow; whatever orchestrates a wave must be the top-level session. Designing C around a self-spawning driver would fail at integration |
| P16 | Workflow agents run fixed at `acceptEdits` and inherit the launching session's tool allowlist; there is no `auto` permission mode for them | Docs (`workflows`), plus one 2026-08-17 observation that cuts the other way: a workflow agent ran the consumer's protocol invocations to completion with no prompt and no human input. That run failed at the adapter-version guard before submit, so only claim and read were exercised, and a different consumer machine's allowlist posture is untested. **Inferred, not observed**, that the clean run generalizes | C2, alongside the instruction-fidelity comparison | The background lane's unattended result is not evidence about this lane: the two lanes have structurally different permission postures. The mitigation is the same as P5's -- pre-allowlist the exact invocation strings and constrain the worker prompt to them -- and, if D5's lease sizing assumed an unattended posture for this lane, that assumption needs rechecking |

Phase order guarantees no expensive unwind: A-min depends on none of P1-P16;
B1 code depends on P3-P6 and P11-P14 but is not published as working until B2
tests them; C1 depends on P7-P8 and P14-P16 and is gated by C2. P2 remains the
one unvalidated premise the whole of B rests on, and by the recorded 2026-08-17
decision it stays that way: it is carried, not tested. P14-P16 are the first
rows established by running the shipped protocol rather than by probing the
platform, and P14 in particular moves risk out of the gates and into the driver
machinery around the transport -- it does not remove risk from either gate.

## Breaking changes and migration

Under this sequencing, **A-min, B, and C contain no breaking changes.** The
breaking surface is confined to A-cleanup, and within it:

- **Untracked loop helpers** (`run_single_pass`, `guarded_sweep`, `run_bulk`
  without a store): behavior frozen (D2), deprecation warnings added in
  A-cleanup, removal keyed to D7's evidence rule. `call_llm` and
  `submit_validated` are not deprecated and remain first-class.
- **Halt semantics:** corrected only on the tracked path -- a typed
  `HaltError` stops claiming and appears in run status with an unfinished set;
  it is never reduced to `UnitOutcome.ERROR` there. Consumers opting into
  tracked runs must read halt metadata instead of counting halt-as-error.
- **Resume model:** tracked unfinished is a set including the triggering unit.
  Migration note, verbatim requirement: a consumer resuming today with
  `[trigger] + remaining` (reconstructed from `BudgetStop.unit_id` +
  `remaining`) must drop the manual trigger prepend when moving to the tracked
  view, or the trigger double-processes.
- **Tracked-run requirements:** stable, non-empty, run-unique unit IDs
  (preparation fails before side effects, reporting all collisions, with a
  documented ID-derivation recipe); explicit driver/backend/model on the run
  rather than the process-global `CONTENT_PIPELINE_LLM_BACKEND` router, which
  remains the compatibility default for direct calls.
- **Versioned contracts:** the JSON envelope and adapter identity are public
  compatibility surfaces; incompatible resume is refused, never guessed.
- Every consumer-visible change ships with the coupled plugin manifest and
  package version bump through `scripts/publish.py`; compatibility aliases are
  tested, not merely documented.

## Explicitly not doing

- **No widening of `LLMBackend`** into prepare/poll/lease/apply territory, and
  no `complete_batch()`. The one-call protocol stays the inline driver's leaf.
- **No `claude -p /saved-workflow` recipient.** Mechanically real, but its
  capacity classification is unproven (P10) and its entry process is headless.
- **No in-session background-subagent recipient.** Degenerate relative to the
  workflow lane; same queue, weaker observability, no separate agent-view row.
- **No MCP server, no standalone daemon, no console script.** The mountable
  command group preserves the documented library boundary.
- **No Agent SDK transport.** It reopens the headless capacity ambiguity, and
  the docs steer SDK products to API-key billing.
- **No automatic parallelization of graph walks.** Parallelism requires a
  prepared independent wave (`pipeline/workunit.py:92-128`).
- **No universal executor.** Claims, readiness, halt, and finalization are
  cross-cutting; scheduling stays driver-specific.
- **No merging of ResponseCache, the run store, and consumer content ledgers.**
  Different keys, retention, and authority; atomic cache writes suffice.
- **No cross-plugin relocation** (workflow helpers into workflow-kit, the
  dispatcher into llm-scripting-kit). Plugin boundaries are hard; the one-way
  dependency stands.
- **No dynamic production workflow generation.** Unreviewed scheduling code and
  resume-breaking nondeterminism; may remain an explicit experiment.
- **In A-min, no tracked facades over the legacy loops.** The observed
  consumers own their loops; the tracked API plus a documented wrap-your-own-
  loop recipe serves them better than facades over helpers nobody was observed
  using end to end. Facades may be reconsidered in A-cleanup if evidence of
  demand appears.
- **Deferred until real consumers demand them:** llm-scripting-kit type
  deduplication, dependency-antichain automation, per-unit token telemetry
  beyond nullable usage, and a pricing observer.

## Where I departed from the review

No finding was rejected; the resequencing, all five settlement decisions, and
the gate extensions are adopted as specified. Four findings, written out in
full, proved softer than their review form implied; each is flagged here so the
user can weigh it, and none changes a decision:

1. **"Every worker will hit a permission prompt" is conditional, not
   established.** The blocked-worker risk (B2 item 2) is real and correctly
   make-or-break, but its premise -- that `--bg` sessions cannot be
   pre-authorized -- is itself one of the unverified platform behaviors. If
   `--bg` inherits project `settings.json` allowlists, the "fallback" is simply
   the design, and the gate item resolves to documented setup rather than a
   platform blocker. The gate is kept pass/fail either way. Settled in part on
   2026-08-17 (P5): user-scope `permissions.allow` inheritance by a `--bg`
   session is observed, so for enumerated invocations the fallback is indeed the
   design; project-scope inheritance remains inferred, and what stays open is
   the fallback's completeness -- a worker left latitude in how to run a step
   composes invocations no allowlist author enumerated.
2. **Default reconciliation for deliver modes is a design intention, not a
   verified capability.** The claim that marker-protected writes make "did this
   land" mechanically checkable was not verified against
   `deliver/inplace.py:170-391` mechanics in detail, and the projection/
   changeset modes may not support it. D6 therefore promises reconciliation
   only where the marker design supports it, with fail-closed as the floor --
   which weakens "most consumers never write the hook" to an aim.
3. **"Finalize re-parses mechanically" leans on an unstated property.**
   `parse_fn` is arbitrary consumer code; the re-parse is mechanical only if
   `parse_fn` is deterministic and store-independent. The plan converts that
   silent assumption into an explicit `RunAdapter` contract term (D1), which is
   a strengthening the review implied but did not state.
4. **Worst-case workflow leases have a cost the review did not price.** With no
   renewer, a lease sized to worst-case runtime means a genuinely dead agent
   holds its slot for the full window. Accepted as the price of a lane with no
   liveness channel, and stated in D5 rather than left implicit.

## Open questions

Genuinely open -- not settled by this plan, and none blocks A-min:

1. Should accepted output eventually be stored as a versioned consumer result
   codec instead of raw text plus deterministic re-parse? D1 makes re-parse the
   contract; a codec would relax the `parse_fn` determinism requirement and can
   be added compatibly later.
2. What idempotency guarantee can the first real consumer provide for apply,
   and does its deliver mode fall inside D6's shipped reconciliation? Partly
   answered 2026-08-17 for two consumers (see the finding recorded under D6):
   the answer tracks the write shape, cheap for a keyed per-row write and absent
   for a whole-file rewrite. Still open in general, and still unexercised
   against a real external side effect -- B2 item 6.
3. Who owns the default database location convention and a cleanup command --
   the library documents a recipe today; does a consumer pattern justify more?
4. Does status need cost aggregation beyond nullable per-attempt usage, and
   what would a pricing observer for session-pool lanes even measure, given no
   per-call cost is observable there?
5. The exact size and rollout rules of the headless allowance (P1) -- B2 item 1
   answers the actionable half; the underlying accounting remains unexplained
   by public documentation.
