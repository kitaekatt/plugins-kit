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
additive tracked inline driver over the existing backends. **B** ships a
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

### Phase B -- Claude background sessions

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
`state`; `pid` and `status` belong to interactive records and must never be
required of a worker. Settled workers are visible only under `--all`, and
`startedAt` is epoch milliseconds. Lease renewal per D5; typed failure mapping
reusing `classify_halt_text` markers; pause/resume. Two corrections that follow
from the 2026-08-17 probe and are load-bearing rather than cosmetic: command
construction uses the **top-level** verbs `claude stop|logs|rm|respawn <id>`,
never `claude agents <verb> <id>`, which exits 0 having done nothing (P3), so the
preflight asserts each verb behaves rather than trusting an undocumented surface;
and `claude logs <id>` is a **live-daemon-only** channel (it fails with
`connect ENOENT \\.\pipe\cc-daemon-*-control` once the daemon has exited), so
halt-text classification for a settled unit reads the session transcript or the
per-job state, not `claude logs`. Per P11 the launcher's exit code and banner are
never evidence a worker started: every dispatch is confirmed by polling
`agents --json` for a transition out of the initial state, and a `failed` within
the first seconds is classified as launch misconfiguration rather than work
failure. `~/.claude/jobs/<id>/state.json` (carrying `needs`, `detail`,
`output.result`, `tokens`) and `~/.claude/daemon.log` are richer than
`agents --json` but are undocumented internal state; they may be read as
**optional enrichment behind a tolerant parse**, never as the required surface.
Worker assets: `agents/pipeline-worker.md` and
`skills/execute-work-unit/SKILL.md` --
one agent claims one unit, reads its prepared request through the consumer
command, produces an answer, submits through the same command, revises from
compact rejection feedback until accepted or exhausted, and exits. The launch
prompt carries only run and unit identifiers; unit content never appears in
command lines or the orchestrator's context. An orchestration skill
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
tolerance, and missing-required-field loudness; session-ID-not-PID identity;
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

1. **Capacity classification probe (make-or-break). Open, and the only item
   still needing a quota spend.** Confirm live that `--bg` work draws the
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
2. **Permission-mode behavior (make-or-break, with fallback). Half settled.** A
   `--bg` session is interactive-supervised, not `-p --permission-mode
   bypassPermissions`. Settled 2026-08-17 (P5): `--permission-mode` exists with
   its choice list and composes with `--bg`, and `claude agents` carries
   permission and settings-source flags as dispatch defaults, so the fallback
   below is buildable. Still open, and cheap: one `--bg` session launched with
   the project's `settings.json` in force and **without** any bypass flag, given a
   prompt running exactly one consumer-shaped command covered by
   `permissions.allow`; pass = it completes, fail = `agents --json` shows
   `state: "blocked"` with a permission question in the job's `needs`. P12 makes
   the stakes concrete: a blocked session has been observed sitting 19 days with
   nothing timing it out, so a stalled worker is held only by our own lease. This
   is the highest information per unit of quota available and should run first.
   **Fallback design, shipped as documented consumer setup:** a pre-authorized
   allowlist covering the consumer command invocations the worker performs,
   verified by that single-unit probe before any batch.
3. **Worker registration. Settled, reduced to a composition check.** Both halves
   confirmed 2026-08-17 (P6): background sessions load plugin skills, and
   `--append-system-prompt-file` / `--system-prompt-file` / `--agent` / `--agents`
   exist. What remains is verifying those flags actually compose with `--bg`
   rather than being silently dropped -- and per P11 that must be judged by
   observing the worker's behavior, not the launcher's exit code. Fold into the
   item-2 single-unit probe. Still to be recorded either way: what exactly is in
   the launch prompt.
4. **Verbatim `agents --json` schema. Settled** as of 2026-08-17 (P4): the
   `kind` discriminator, the two field sets, and the presence of interactive
   records are read verbatim, and the reconciler's required-field list is fixed
   to `kind`, `id`, `sessionId`, `state` behind a `kind == "background"` filter.
   The residue is not a schema question but a stability one: the lifecycle verbs
   (P3) are hidden from `--help`, which probing cannot settle, so B1's preflight
   carries a version-drift check instead.
5. Authentication-failure behavior, concurrency enforcement, and a full
   lifecycle: two live units, one validation rejection/revision, one deliberate
   worker kill and reclaim, pause/resume, and a status query from a fresh
   orchestrator context.

**Exit criterion.** A main agent supervises via occasional bounded digests,
identifies a failure pattern, alters N or a batch, and halts safely without
ingesting unit content. Items 1 and 2 pass, or the phase stops and reports.

### Phase C -- Workflows

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
against the background lane. Exit: observed behavior matches the durable
protocol and the workflow returns no accumulated content.

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
trivial background sessions were stopped and removed. P2, the load-bearing
premise, was deliberately left untested by that probe.

| # | Assumption | Established by | Tested by | If wrong |
|---|---|---|---|---|
| P1 | `-p` draws a small headless allowance, then metered credits | User's direct observation; docs conflict (Help Center says the separate credit was paused) | Not directly tested; it is the premise. B2 item 1 tests its actionable half | The effort loses its motivation; A-min still stands on its own |
| P2 | `--bg` sessions draw the interactive session pool | Docs (`agent-view`, partly summarized fetch); **never observed**. The 2026-08-17 probe neither supported nor contradicted it: no batch was run, and a bg transcript's `usage` blocks carry no pool-attribution field (regex sweep of a 976-record bg transcript for `rate\|limit\|quota` found only tool parameters) | **B2 item 1, pass/fail -- still untested.** A measurement instrument exists as of 2026-08-17 (P9), but exercising it costs a measurable batch | B does not ship; escalate with evidence |
| P3 | `claude --bg` surface: positional prompt, prints short session ID, rejects `-p`; `claude agents --json` exists; the lifecycle verbs are **top-level** `claude stop\|logs\|rm\|respawn\|attach <id>`, hidden from `claude --help` | Observed 2026-08-17 on Claude Code 2.1.233: `claude --help` (positional prompt, `--bg`), `claude --bg -p "..."` (refused with a verbatim conflict message, no spawn), a `--bg` launch banner (`backgrounded * a47add3f`, 8-hex id), `claude agents --help` (no `Commands:` section), and `claude <verb> --help` for each verb. The earlier `claude agents <verb>` shape was doc-derived and is **refuted**: `claude agents stop\|logs\|rm\|respawn\|list --help` each print the plain `agents` help and **exit 0**, ignoring the positional | B2 items 3-5; B1 preflight asserts the verbs behave, since they are undocumented and carry no stability guarantee | Driver command construction changes; contained in B1. The refuted form fails **silently at exit 0**, so the preflight is not optional |
| P4 | `agents --json` records are discriminated by `kind`: `kind: "background"` carries `id`, `cwd`, `kind`, `startedAt` (epoch ms), `sessionId`, `name`, `state`; `kind: "interactive"` carries `pid`, `cwd`, `kind`, `startedAt`, `sessionId`, `name`, `status`. No record carries both `id`/`state` and `pid`/`status`. `--all` adds settled background sessions | Observed 2026-08-17: `claude agents --json --all` output read verbatim, one record of each kind. **Refutes** the previously recorded five-field shape (`id`, session id, `pid`, `state`, `status`), which came from a summarized doc fetch and did not record the `kind` discriminator at all. `state` values seen: `blocked`, `failed`, `done`, `stopped` | B2 item 4; reconciler is schema-tolerant from the start and filters on `kind` first | Reconciler required-field list adjusts; loud validation prevents silent misreads. Omitting the `kind` filter makes the orchestrator's own interactive session look like a worker |
| P5 | `--bg` permission behavior (flag or settings inheritance); workers can avoid unattended permission prompts | Flags observed 2026-08-17: `--permission-mode <mode>` (choices `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`) composes with `--bg` (two sessions spawned), `claude agents` exposes `--permission-mode`/`--dangerously-skip-permissions`/`--allow-dangerously-skip-permissions`/`--settings`/`--setting-sources` as dispatch defaults, and a real bg session recorded `--permission-mode auto` in its respawn flags. **The behavioral half is untested**: no probe ran an allowlisted consumer command unprompted inside a `--bg` session. That background sessions inherit project `settings.json` allowlists by the ordinary mechanism is inferred, not observed | **B2 item 2, pass/fail, with allowlist fallback** | Lane stalls `blocked`; fallback is documented consumer allowlist setup, and the flags that fallback needs are confirmed to exist and compose |
| P6 | Background sessions load plugin agents/skills; a per-unit trusted system channel exists (`--append-system-prompt-file` or equivalent) | **Confirmed** 2026-08-17 on both halves. Channel: `--append-system-prompt-file` and `--system-prompt-file` parse-probed (each errored with `argument missing`, so the option exists); both are hidden from the `--help` option list but named in `--bare`'s help text. Also present: top-level `--agent <agent>`, `--agents <json>`, and `claude agents --plugin-dir`. Loading: a background-session transcript (system records carrying `"sessionKind": "bg"`) used the `Skill` tool with plugin-namespaced skills (`awesome-kit:task`, `bootstrap:bootstrap`, `git-kit:git-code-review`, several `skills-kit:*`). That these flags compose with `--bg` is **inferred from `--permission-mode`'s behavior, not observed** | B2 item 3, reduced to preflighting composition -- and per P11 that must be judged from worker behavior, not the launcher's exit code | Worker persona delivery redesigned inside B1 |
| P7 | Workflow limits: 16 concurrent agents, 1000/run; in-session-only resume with ordering-based cache invalidation; `agent()` accepts a JSON schema; no per-agent wall-clock timeout | Docs (`workflows`) | C2 | Lane construction constants and lease sizing change; contained in C1 |
| P8 | Live-session workflow `agent()` calls count as ordinary session usage | Docs (`workflows`, `costs`) | C2 capacity check | C does not ship |
| P9 | No programmatic *API* exposes remaining session-pool capacity, but a supported *observable* does: `.rate_limits` in the statusline hook payload, which claude-ui-kit mirrors to a documented file contract at `~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` | **Refutes** the earlier "search of docs found none", whose counter-evidence was inside this repository. Observed 2026-08-17: `plugins/claude-ui-kit/scripts/statusline.sh` reads `.rate_limits.five_hour.used_percentage`, `.seven_day.used_percentage`, and both `resets_at`, and states in comment that the statusline hook payload is the only place Claude Code surfaces `.rate_limits`; `claude-ui-kit/skills/statusline/references/components.md` documents the fields; the live snapshot file on this machine held `five_hour.used_percentage: 10`, `seven_day.used_percentage: 8`, `captured_at` 2026-08-17. Two observed caveats: granularity is whole percent, and only an interactive session rendering a statusline refreshes the file (whether a `--bg` session refreshes it is unverified) | Instrument for B2 item 1; B1's reactive-halt fallback stays the contract either way | Quota foresight improves; nothing breaks. If the observable proves too coarse or too stale, B2 item 1 falls back to the exhaust-the-headless-allowance formulation |
| P10 | A saved workflow under `claude -p /name` has unproven capacity classification | Docs silent | Not tested; the path is rejected (see "Explicitly not doing") | Irrelevant unless the path is revisited |
| P11 | `claude --bg` backgrounds the session **before validating its own flags**: it prints the success banner and exits 0, and a bad flag surfaces only asynchronously as `state: "failed"` in `agents --json` and as a settle line in `~/.claude/daemon.log` | Observed 2026-08-17: `claude --bg --permission-mode bogus "x"` printed `backgrounded * d7217a10` and exited 0; `~/.claude/daemon.log` then recorded `bg settled d7217a10 (crashed): exit 1 before init -- error: option '--permission-mode <mode>' argument 'bogus' is invalid`; `claude agents --json` later showed that id as `state: "failed"`. Second instance of the hazard the `codex_dispatch_is_silent_on_failure` insight records for codex dispatch -- judge a dispatch by its observable outcome, never by `$?` | B1 startup preflight and B2 item 5: every launch is confirmed by polling `agents --json` out of the initial state, and a `failed` inside the first seconds is classified as launch misconfiguration, not work failure | If some launches do validate synchronously after all, the conservative reading costs only an extra poll per launch. Trusting the exit code instead would make a misconfigured batch indistinguishable from a running one |
| P12 | A `--bg` session can sit in `state: "blocked"` indefinitely with nobody attached; no platform lease, deadline, or auto-fail was observed | Observed 2026-08-17: `claude agents --json --all` listed a background session in `state: "blocked"` since 2026-07-29 -- 19 days -- and its `~/.claude/jobs/<id>/state.json` carried an unanswered question in `needs`. That block was a question rather than a permission prompt, so it demonstrates the failure geometry, not the permission-prompt case itself (which is P5) | Standing; it is why D5 leases and reclaim exist. B2 item 5 exercises reclaim live | Makes the lease-and-reclaim design **required**, not defensive. If a platform-side timeout does exist and was merely not observed, the lease is redundant but harmless |

Phase order guarantees no expensive unwind: A-min depends on none of P1-P12;
B1 code depends on P3-P6 and P11-P12 but is not published as working until B2
tests them; C1 depends on P7-P8 and is gated by C2. P2 remains the one
unvalidated premise the whole of B rests on.

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
   platform blocker. The gate is kept pass/fail either way.
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
   and does its deliver mode fall inside D6's shipped reconciliation?
3. Who owns the default database location convention and a cleanup command --
   the library documents a recipe today; does a consumer pattern justify more?
4. Does status need cost aggregation beyond nullable per-attempt usage, and
   what would a pricing observer for session-pool lanes even measure, given no
   per-call cost is observable there?
5. The exact size and rollout rules of the headless allowance (P1) -- B2 item 1
   answers the actionable half; the underlying accounting remains unexplained
   by public documentation.
