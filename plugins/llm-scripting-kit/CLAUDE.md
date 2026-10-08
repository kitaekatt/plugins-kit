# CLAUDE.md -- llm-scripting-kit plugin

Guidance for an AI agent working in this plugin. llm-scripting-kit is the
fleet's LLM ACCESS layer: it answers which endpoint, which model, which key,
which transport, and how a configured local model server starts. `README.md` is
the human-facing
description of the same surface; `skills/openrouter-account/` owns key
management.

## Local model server entry points

`scripts/model-server.sh` is the canonical Claude-first launcher. Invoke it as
`"${LLM_SCRIPTING_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/model-server.sh" qwen36|qwen38|qwen38l`; this
works from the plugin root Claude actually loaded and does not assume a plugin
`bin/` directory is on PATH. `bin/qwen36-server`, `bin/qwen38-server`, and
`bin/qwen38l-server` are thin shell adapters for interactive environments that
deliberately put them on PATH.

For lifecycle operations, use `qwen-switch start qwen36|qwen38|qwen38l` to
replace the managed server on the selected profile's port and wait for the
profile's exact OpenAI model id. Use `qwen-switch status` to report the managed
resident profile. It composes `model-server.sh`; it does not duplicate any
profile arguments. Before launch it runs the plugin-relative sibling
`bin/llm-scripting-kit swapper terminate-listener --port PORT --accept-replace`
(by path, never from PATH, since an older installed shim may lack the verb) and
aborts the start on a non-zero exit. The script itself sends no signals; `lsof`
and `ps` are used only for readiness and `status`.

Each profile preserves its measured GPU settings while keeping paths and ports
overridable through environment variables. `qwen36` resolves NInfer from
`NINFER_ROOT` or conventional dev roots and uses INT8 KV plus MTP3. `qwen38`
serves the same model through NInfer's NVFP4 artifact with FP8 KV and MTP3, the
profile the artifact's model card measures for an RTX 5090. `qwen38l` is the
llama.cpp GGUF path for the same model, kept as a comparable second backend --
it resolves llama.cpp and its GGUF from their existing environment overrides and
uses full GPU offload plus Q8 KV. The trailing `l` is for llama.cpp; an
unsuffixed name always means the NInfer path. All bind localhost by default;
broader network exposure is an explicit host override. They all default to port
8080, so only one can serve at a time.

## `swapper`: operator lifecycle over a llama-swap swapper

`llm-scripting-kit swapper` (`lib/llm_scripting_kit/swapper.py`, process
mechanics in `_swapper_process.py`) is a **CLI operator tool, not a client
API.** A pipeline or script that wants a model already resolves it through
the registry and the completion seam ("Scope: one call, made correctly"
below); this group is for the person (or the launcher script) managing the
swapper process itself. `--help` on any subcommand is the detailed
reference; this section states only what a reader would otherwise have to
reverse-engineer from the source.

`running`, `terminate` and `unload` take exactly one of `--endpoint NAME` (a
configured model-endpoints registry entry) or `--url URL`; `strays` scans the
local process table and takes no target. Every verb except `guard-launch`
takes `--format json|text` (default `json`); `guard-launch`'s only argument
is `--caller-pid`. Exit codes are shared across the group: `0` success (or, for `strays`,
none found); `1` an operation failure, a safety refusal, a missing resident
model, or (for `strays`) strays found; `2` a usage/configuration error,
including aiming `terminate` or `unload` at a non-loopback target; `3`
reserved for `guard-launch`'s explicit `LaunchRefused` only -- never reused by
another verb, because the launcher that calls it must tell an unambiguous
refusal apart from an uncaught exception, which exits `1` like any other; `5`
the inspection itself could not run to a verdict (access denied, or `psutil`
unavailable), never conflated with "not found" or "unreachable".

`terminate` and `unload` verify the target through the local process table
before signalling anything (loopback-only, exact same-user `llama-swap`
listener, direct child, port match, `create_time` re-checked immediately
before the signal) -- see the six numbered safety rules in `swapper.py`'s
module docstring. Signals never target a PID by name or command-line pattern.

`unload` is **operator-only and never a client action**: it stops every
resident model with no drain of in-flight requests, which is why the CLI
requires both `--all` and `--accept-no-drain` even for one call -- neither
flag has a default that lets the operation proceed silently.

`strays` reports recognized model-server processes (`ninfer-serve`,
`llama-server`; `mlx_lm.server` by argv only, since its process name is
Python) with no swapper ancestor -- residency that bypassed the launcher
guard below, or survived a swapper that exited. It detects and refuses; it
does not kill anything.

`terminate-listener --port PORT --accept-replace` is the one explicit
replacement verb (`swapper.terminate_listener`), used by `qwen-switch start`.
It is not for a llama-swap child and not automatic reaping: private service
wrappers own pre-start reaping, `strays` stays detect-only, and clients and
probes never kill. Rules a reader would otherwise reverse-engineer: records
whose KNOWN owner differs from the caller are dropped before any listener
inspection (same shape as `find_strays`), so root or another user cannot block
it through unreadable sockets, while an unknown-owner or unreadable same-owner
candidate refuses as indeterminate (exit `5`; on macOS, owner-`None`
AccessDenied records can therefore make it refuse, and a zombie process reads
the same way). "Free" needs BOTH the bind check and a scan of same-user listener sockets
that finds none on any address or family, because a bind check alone can pass
beside a listener bound to one specific address or only to IPv6 (macOS,
Windows); an unreadable scan is never free. A free port is a no-op; an occupied port with no recognized
same-owner listener, an unrecognized or second listener, or a llama-swap
descendant refuses before any signal. Recognition is `classify_server`, the
`strays` set. It snapshots PID and create time, rechecks, sends SIGTERM (grace
default 20 s, longer than `terminate` because model servers unload weights), and
sends SIGKILL only when the same PID and create time still exist. Success
requires the same two-part free test (`_swapper_process.port_is_free`: loopback
connect plus IPv4 and IPv6 binds, and an empty listener scan); otherwise it
refuses without signalling again. Another user's listener is invisible to the
scan, so only the bind check covers it. Tests never signal a real
process: the library tests use a fake inspector and an injected `port_free`,
and the `qwen-switch` integration tests run in a temporary plugin-shaped tree
with a stub sibling CLI.

`guard-launch` is the facade `model-server.sh` calls immediately before
`exec`, so a manual launch cannot bypass an active same-user swapper. It is
**fail-open by contract**: only `LaunchRefused` (exit `3`) blocks the launch.
Every other outcome -- no Python interpreter found, the guard script missing,
`psutil` unavailable (`InspectionIndeterminate`, exit `5`), any other nonzero
exit, or an uncaught exception (exit `1`, indistinguishable on purpose from a
refusal the guard never issued) -- makes `model-server.sh` warn on stderr and
proceed with the launch. A broken guard must never stop llama-swap's own
children from starting; the asymmetry is why `LaunchRefused` alone carries
exit `3` while every sibling `SwapperError` maps to `1` or `2`.
`LLM_SCRIPTING_KIT_LAUNCH_GUARD=off` skips the guard entirely (no interpreter
call, no warning) for a team that wants a manual launch beside an active
swapper on purpose; default and every other value is on.

## `acceptance`: proofs that must be able to go red

`llm-scripting-kit acceptance swapper|frontdoor` (run it by the CLI contract path
in README.md, "Invoking the CLI")
(`swapper_acceptance.py`, `frontdoor/acceptance.py`) asserts a deployment
through its HTTP surface only (`/v1/models`, `/running`, `/health/backends`,
`/v1/chat/completions`, the `x-frontdoor-deployment` header), never through
host process access. Exit `0` passed, `1` an assertion failed (an unreachable
target included), `2` usage or configuration.

- **Never-evict needs a constructed overlap.** Alternating rounds cannot test
  greediness: each waits for the previous one. The overlap check runs a long
  request, demands the other model mid-flight, and fails both when the second
  model becomes resident early and when the overlap did not occur (the busy
  model was never observed resident, finished before the demand, or was never
  sampled while both were pending). A pass without the second condition would
  be vacuous.
- **Paid rule.** A tier is paid unless the registry declares
  `billing.mode: unmetered`; an undeclared tier is paid (fail closed), because
  `key_env` and hostnames do not distinguish owner-funded from metered. Without
  `--paid`, the spill-group fill burst is sized to the capped capacity of
  unpaid tiers preceding the first paid tier, and the paid spill leg is
  reported as skipped; under `--quick` it and the queue-overfull leg are not run
  at all. The run plans no
  request onto a paid tier and fails if one answered, but concurrent front-door
  traffic from other callers can still spill a burst request onto an uncapped
  paid tier; run it when the front door is otherwise idle. The queue-overfull leg sends one request past the queue group's
  total cap; that request queues on the group's capped tiers and does not
  spill. Every leg is refused, sending nothing, when a tier it needs is not
  `reachable` in `/health/backends`, and both queue legs are refused without
  `--paid` when any queue-group tier is paid (an uncapped queue tier is a
  configuration error, exit 2).
- **Tier shape comes from data.** Order, cap and billing come from the supplied
  registry through `load_endpoint_registry`; deployment ids must equal
  `/health/backends` for the group. Nothing fleet-specific is a literal in this
  library. `--expect-tier1-cap` swaps only the first spill-group level's
  expected count, so a wrong value must go red.
- Tests use stdlib fake servers. Each of
  `test_swapper_acceptance_constructed_overlap_detects_eviction`,
  `test_frontdoor_acceptance_wrong_tier1_cap_exits_one` and
  `test_frontdoor_paid_spill_requires_paid_flag` was shown to fail with its
  assertion removed; keep that discipline when editing them.

## Reachability is not configuration, and it is never a completion

`endpoints` lists what is CONFIGURED and is pure static data -- always
instant, never touches the network. `endpoints --verify` and the `probe` verb
answer a different question, whether a configured endpoint is ACTUALLY USABLE
right now, through one shared code path (`reachability.py`) that a consumer
can also call directly. Both surfaces are opt-in / explicit-target only: a
bare `endpoints` call must never start paying for a network or subprocess
call silently.

Verification costs zero LLM calls, on either endpoint kind. A **transport**
entry (the `openrouter` adapter, including a self-hosted OpenAI-compatible
server) gets a `GET {base_url}/models` metadata probe -- proof the server
answers HTTP, not that a completion would succeed, which is why a passing
verdict is `status: "reachable"` rather than `available` or `healthy`. A
**harness** entry (claude-cli, codex-cli, opencode-cli) gets a
CLI-resolution-plus-`--version` check -- proof the CLI is invocable, weaker
still, since a real completion would spawn an agent and cost real time/quota
and is therefore never attempted. Do not add a probe path that runs
`backend.complete(...)`, even with `max_tokens=1` -- that was the original
design and was reversed once it reached review: a caller reaching for a
liveness check explicitly does not want to spend a token finding out.

**Status is a three-way string, `reachable` / `unreachable` / `unknown`, never
a bool.** `unknown` means the check itself could not be run to a verdict (an
optional dependency was unavailable, or the check machinery raised
unexpectedly) -- and it is NEVER reported as `unreachable`. "I could not
check" and "I checked and it is down" are different facts, and a bool
collapses them: a consumer gating on `reachable is False` would silently skip
a perfectly usable endpoint whose check never ran. `check_entry` wraps every
dispatch in a catch-all that maps an unanticipated exception to `unknown`
rather than letting it escape or misreporting it, so this invariant holds even
for a failure mode nobody has hit yet. `probe` propagates the same three-way
split to its exit codes: `0` reachable, `1` unreachable, `5` unknown --
`EXIT_INDETERMINATE`, deliberately a different code AND a different axis from
`EXIT_USAGE` (2), which means the endpoint *name* never resolved to
configuration at all, decided before any check is attempted. See
`EXIT_INDETERMINATE`'s docstring in `cli.py` for the full mapping.

**`codex-cli` prefers `bootstrap_lib.detect_codex` and falls back, it does not
report the import failure as a verdict.** `bootstrap_lib` is this plugin's own
OPTIONAL shared-lib dependency (see "Its consumers must declare `bootstrap_lib`
themselves" below) -- its absence says nothing about whether codex ITSELF is
installed and working on the machine. Reporting an `ImportError` there as
`unreachable` was a live false negative (codex-cli 0.150.1 installed and in
active use, reported unreachable purely because this plugin's own optional
import failed) fixed by falling back to the identical PATH + `--version` check
claude-cli and opencode-cli already use. `detect_codex` stays the preferred
path when importable -- it is cached and reads a structured version -- but its
absence must degrade to the ordinary check, never to a wrong answer. Any
future per-harness check that has a "preferred path, PATH-based fallback"
shape should follow this same rule: an optional dependency failing to import
is `unknown`-shaped information about the CHECK, not `unreachable`-shaped
information about the TARGET, unless (as here) a real fallback check exists to
produce an actual verdict.

## Usage pacing is a third axis, and it fails open

`conserve_usage` (`usage_budget.py`) answers a question neither `endpoints` nor
`reachability` can: an endpoint that is configured AND answering may still be
one whose subscription quota is being burned faster than the clock.

**Two thresholds, two different consequences, and collapsing them is the
mistake to avoid.** With `r` the fraction of quota remaining and `t` the
fraction of the window remaining: `r <= 0` is OUT OF QUOTA and DISABLES the
model -- the pool is spent, a call would fail, so it leaves selection
entirely. `r < t` is UNDER QUOTA and only DE-PRIORITIZES it -- it is being
spent quickly but still has capability, so it stays usable and merely loses to
an equally-suitable peer that is not behind pace. Withholding a model for the
second reason costs the caller something it still had.

**A pool is DECLARED or defaulted, never derived FROM THE MODEL.** fable draws
on a per-model weekly bucket (`model_scoped`, selected by `display_name`); opus
draws on the all-model weekly window (`seven_day`). A bare `conserve_usage:
true` takes `seven_day`, which `read_codex_pool` resolves to codex's own
`primary` -- a harness remap, not a per-model one. The derivation that is
refused is "use the model's own bucket when one exists": it would hand opus
`seven_day_opus`, the opposite of the all-model pacing an opus entry means.

**It reads files harnesses already wrote, and reads no credential.** claude's
numbers come from claude-ui-kit's statusline snapshot at
`~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` -- a FILE
CONTRACT, optional on both sides, neither plugin depending on the other --
because the statusline hook payload is the only surface on which Claude Code
emits `rate_limits` at all. codex's come from the newest session rollout under
`~/.codex/sessions/`. Anthropic's `/api/oauth/usage` would answer more
completely, including the per-model bucket, and is deliberately NOT called: it
needs the user's OAuth access token, and a pacing check is not a good reason to
put a subscription credential into this code path. Where a CLI does not expose
usage, the honest outcome is `status: "no-data"`, not a token read.

**No data never withholds a model.** A missing snapshot, an absent pool, or a
window that has already reset yields `no-data` and the endpoint stays usable --
the same rule reachability applies one axis over ("I could not check" is never
"it is down"), with a second concrete reason here: the per-model bucket is
emitted by the server for some accounts only, so failing closed would make
fable permanently unreachable on every machine whose payload omits it.

**An exhausted codex account reports no window at all**, so the window rule
alone would read it as `no-data` and keep the seat. `read_codex_pool` treats
null `primary` AND `secondary` plus `credits.has_credits: false` (not
unlimited) as OUT OF QUOTA. `has_credits: false` alone is not the signal: a
healthy plan account reports it on every reading, because it describes
purchased extra credits. The verdict ends at the `usage_limit_exceeded`
error's "try again at" time, or 5 hours after the reading when that text is
absent. Without the bound, a dropped seat launches no new codex session, so a
stale reading would never be replaced.

**A verdict is pinned for the session** (`CLAUDE_CODE_SESSION_ID`); the stance
and its rationale are the register entry in `plugins/CLAUDE.md`.

`discover_seats` applies both consequences: an out-of-quota seat moves to
`SeatsResult.out_of_quota` (reported, not dropped, so "no seat above me" stays
distinguishable from "the seat above me is spent"), while an under-quota seat
stays in `seats` and sorts after any peer of the same relation that is not
behind pace. Quota rank sits BELOW relation and ABOVE tier -- a seat's relation
says whether it can do the job at all, which outranks how much budget is left.

`declaration.py` is the consumer-facing half and the one API over a model
declaration. `describe` classifies every declared id, keeps usable,
out-of-quota and unreachable entries in the render, hides the rest SILENTLY,
orders the render by pace (`order_by_pace`), marks the default, and emits
the choice and re-selection rule text (`Ranking.rule`). It raises
`NoUsableRoutingTarget`, which itemises every declared id, when nothing
usable remains. It RANKS and does not dispatch, which is what lets job-kit
pass its own requirements, capabilities, factory, exclusions and run-scoped
reachability cache into one selection it still owns. `run` is the dispatcher
for callers with no loop of their own. Five things are easy to break:

- **RENDER, SKIP and FLOOR are separate surfaces.** Nothing may name a hidden
  id outside the floor, including the render header. A notice, warning or log
  line for a skipped id reverses the owner's silent-skip ruling (directions
  13, 16 and 17 in the repo's declaration-format design).
- **Status is pinned, pace is fresh.** `usable` and `[default]` come from
  `pinned_evaluate`. Only the pace number comes from an unpinned read, so a
  pinned AVAILABLE verdict still never flips on a re-read.
- **`max_attempts` is not the floor.** A halt that uses up the last attempt
  returns `attempt-limit`. It never raises `NoUsableRoutingTarget`, because
  the pool was not empty.
- **`run(..., observer=)` reports execution events and refuses without their
  module.** The keyword-only `observer` (`ExecutionObserver`, an `emit` method
  keyword-compatible with `bootstrap_lib.execution_event.Emitter.emit`) gets
  `dispatch-selected`, `call-started`, `usage` and `result` per attempt
  (`attempt_id` is the attempt number as a string) and one `terminal` per call
  (`unroutable` before the floor is raised). Usage goes only through
  `usage_payload`, so null means unknown and is never written as 0. An observer
  exception propagates. Bootstrap >= 0.135.0 is required, and its absence
  raises `DeclarationSupportError` before any dispatch. Detail: the
  `declaration.run` docstring.
- **A session caller hides transports unless it says it can run them.** A
  transport entry has no agent loop, so `caller="session"` classifies it
  unroutable by default. A session caller that reaches transports through a
  runner of its own passes `dispatchable=("transport",)` (CLI `--dispatchable
  transport`); the code-review skills do, for every reviewer lane the lane
  runner binds to a transport.

- **A session caller records its own quota halt.** `run()` and job-kit call
  `record_observed_halt` for halts they observe. An agent driving the harness
  itself observes the halt instead, so `RULE_TRIGGER_SESSION` tells it to run
  the `record-halt` CLI verb before re-selecting; without that, the pinned
  AVAILABLE verdict keeps the spent entry `[default]` for the session (drill
  finding F1, `tests/llm-scripting-kit/test_risk_drill.py`).

- **A halt spends the whole pool.** Every caller passes its registry as
  `record_observed_halt(..., entries=...)`, which writes OUT-OF-QUOTA for
  every entry sharing the halted entry's `quota_pool_key` (same harness
  account, same pool) -- the same set `describe` labels "shares <pool>
  with". Without it, each sibling costs one failed dispatch (drill finding
  F4). A new caller that omits `entries` reintroduces that gap.

The `usage` and `describe` verbs are the inspection surfaces for a check that
is otherwise invisible.

## Scope: one call, made correctly

This layer owns everything a SINGLE completion needs -- endpoint resolution,
the model registry, credential lookup with source attribution, prompt-cache
message shaping, and a shared halt taxonomy (`classify_halt_text`, `HaltError`
in `completion/halt.py`) so a persistent failure classifies identically
whichever transport produced it. Four transports sit behind one `complete()`:
`OpenRouterBackend` over HTTP, `ClaudeCliBackend` driving the local
`claude -p` CLI, `CodexCliBackend` driving `codex exec`, and
`OpencodeCliBackend` driving `opencode run`.

This layer classifies; it does not halt. Every backend exposes
`classify_halt(exc)` and the transports raise ordinary errors carrying
classifiable text -- nothing here raises `HaltError` itself. Converting a
classification into a stop is the caller's, because only the caller knows
whether it is mid-sweep with hundreds of items left or running a one-shot
script that should simply die. Exporting the type instead of raising it is what
lets a new consumer inherit the taxonomy rather than invent one.

**Backpressure is a halt that clears itself, and the rule for what counts is
narrow.** `HALT_BACKPRESSURE` (`"backpressure"`, `completion/halt.py`) marks
transient endpoint overload: retry after a wait, do not stop the run.
`classify_backpressure(exc)` returns `Backpressure(status, retry_after_s)` or
`None`, and `classify_openai_exception` checks it FIRST, so
`OpenRouterBackend.classify_halt` reports it. A 503 counts only with
queue/overload/busy/timeout wording or a server code such as
`request_queue_timeout`; a bare 503 is an outage, not a halt. A 429 counts only
with a positive signal (a `Retry-After` header, a "try again in N s" hint,
queue/overload wording, or a code such as `rate_limit_exceeded`); a bare 429
keeps `rate_limit`. Quota or credit wording (`insufficient_quota`, billing,
credit, usage limit) is NEVER backpressure and keeps `rate_limit` or
`insufficient_credit`. `retry_after_s` comes from a numeric `Retry-After`
header, else a body hint (seconds or ms); an HTTP-date header yields `None`.
`halt_payload(kind, exc)` builds the envelope object (`{"kind": "backpressure",
"retry_after_s": 3.0}`, other kinds `{"kind": kind}`), and `HaltError` carries
`retry_after_s`. A single-call `complete` halt adds the same `halt` object with
`error.code` and exit `3` unchanged; there is no separate backpressure exit.

The Codex and OpenCode transports carry additional rules their siblings do not
need.

**Its consumers must declare `bootstrap_lib` themselves.** `CodexCliBackend`
builds argv exclusively via `bootstrap_lib.codex.build_codex_exec_argv`, and
imports it LAZILY so this package stays stdlib-only at import. A consumer venv
that links `llm_scripting_kit` but not `bootstrap_lib` therefore imports the
codex backend cleanly and dies on the first dispatch, not at load. A shared lib
does not carry its own shared-lib edges to a consumer -- each venv declares what
it needs (`plugins/CLAUDE.md`, "Why shared libs rather than published
packages"). content-pipeline-kit declares both.

**Skill context is a REQUIRED edge into `bootstrap_lib.skill_material`, with a
floor and a refusal at the call.** `completion/skill_context.py` imports the
module lazily, so `import llm_scripting_kit` never needs it (the records live in
the stdlib-only leaf `completion/skill_context_types.py`). `requires_bootstrap`
in `bootstrap.json` is 0.138.0, the version that ships the calls the seam
makes (`SKILL_MATERIAL_BOOTSTRAP`, pinned with the manifest by
`tests/llm-scripting-kit/test_bootstrap_manifest_floor.py`). A floor gates
provisioning, not the code that is already active, so `_skill_material()`
still probes every call it makes and `materialize_skill_context` raises
`SkillContextSupportError` in three states, each with its own message:
`absent` (`bootstrap_lib` not importable; install bootstrap), `too-old` (the
linked copy lacks a call, or a report shape is unknown; update bootstrap, a
copy left by an uninstall reads the same) and `no-pyyaml` (the interpreter has
no PyYAML). No state falls back to another reader, because a request that names
skills and is sent without them would be read as answered with them.

**Model-authored text must stay out of exception messages.** Codex writes its
transcript to BOTH stdout and stderr, and `halt` classifies by substring-matching
an exception's message -- so `CodexRunError` keeps the transcript on attributes
and out of the message. Inlining a channel into that message makes a healthy run
that merely discusses a rate limit classify as a persistent halt and abort the
caller's whole run.

**OpenCode is workspace-guarded by policy.** Its required `--auto` flag
approves permission prompts, so `OpencodeCliBackend` injects a highest-
precedence `OPENCODE_CONFIG_CONTENT` policy that denies `external_directory`
and `task` globally and on the explicitly selected `build` agent for every
unattended run. The adapter also passes `--pure` to disable external plugins.
`--dir` selects the intended workspace. Shell work remains available, so this
is a practical OpenCode guardrail rather than an OS sandbox guarantee.

## The seam is uniform; the transports are not

The useful mental model is that you write the prompt pair once and choose the
executor separately -- but the separation is incomplete, and a caller who
believes it is clean will get burned. These things travel with the executor:

**Codex contributes a harness, not transport, and a harness is warranted only
when what the unit needs is not knowable when the prompt is written.** A fully
supplied transformation is a completions call and stays one. The overhead
figure, the full rule, endpoint compatibility (wire, tool schema, keyless
auth), and the dispatch traps are all owned by the orchestrate skill's
`references/codex-dispatch.md` in awesome-kit -- read it there rather than
relying on a figure restated here.

**The `model` id is not portable.** An OpenRouter slug means nothing to
`claude -p`, and codex requires fully-qualified ids. Nothing here translates
them, so choosing a backend is really choosing a backend AND a model id.
content-pipeline-kit's `routed_model()` is the in-fleet compensation for this,
and its existence is the evidence: a genuinely uniform seam would not need it.

**`BackendOptions` is a union, not a neutral description of the work.**
`user_cache_prefix` is OpenRouter-only; `allowed_tools`, `disallowed_tools` and
`system_prompt_mode` are claude-cli-only; `effort` reaches claude-cli, codex,
and opencode, and reaches OpenRouter only on a transport entry that resolves
an `effort_style`; `cwd`
reaches the three CLI backends and not OpenRouter, which is why `cwd` is not a
core param of this seam. `temperature` and `max_tokens` are accepted and then
dropped by all CLI backends -- dropped and REPORTED, not ignored.
`skill_context` is read by all four and delivered by OpenRouter alone, as the
leading block of its system message; the three CLI backends refuse it before
dispatch, because a harness loads skills itself and the seam cannot see what
it loaded.

**Effort on a transport entry is conditional, and one module owns its
vocabulary.** `llm_scripting_kit.effort` defines the styles (`top-level`,
`ninfer`, `chat_template_kwargs`, `unsupported`), the ninfer `high` -> `xhigh`
remap, and `plan_effort`, the precedence a direct call follows: an effort the
caller put in `extras` wins verbatim (top-level over nested when both are
present; an explicit null means "send none"), then `BackendOptions.effort` is
placed in the endpoint's style, else it is dropped. The front door's
`_normalize_body` uses the same `extract_effort` / `place_effort`, so the two
paths cannot disagree about a style. Style resolution for a DIRECT call is
`model_endpoints.resolve_effort_style`: entry `effort_style` > `frontdoor:
true` (top-level) > a DECLARED `routing.effort_style` > none; a
declared-invalid style resolves to none rather than falling through. The front
door's own choice is `deployment_effort_style`, where an omitted routing style
still means top-level. The openrouter family record keeps `effort` in
`dropped_params` and lists it in `conditional_params`;
`completion.endpoint_profile.endpoint_capabilities` specializes the record per
endpoint, and `OpenRouterBackend.params_report` derives the per-call report
from the effort plan -- the CLI failure envelope calls it too, so a failed call
never reports an overridden or suppressed effort as delivered. The factory
(`BackendSelection.effort`), the `complete` verb and `declaration.run` fill the
registry `reasoning_effort` default into `opts.effort`. `resolve` reports
`effort` (the delivered value, null when undeliverable), `declared_effort` and
`effort_delivery`; `endpoints` reports `reasoning_effort` and `effort_delivery`
per transport entry.

Registry loading rejects a transport entry that declares `reasoning_effort`
without a deliverable `effort_style`; invalid styles and conflicting declared
styles remain registry notes.

**`effort_menu` and `lower_effort` are how a caller retries a length stop.**
`effort.effort_menu(endpoint)` returns the efforts a registry endpoint accepts,
low to high, from its resolved style (`style_effort_menu` is the same lookup by
style): `ninfer` gives `("none", "low", "medium", "xhigh")` and rejects `high`,
`top-level` and `chat_template_kwargs` give `("low", "medium", "high")`, and an
endpoint that delivers no effort gives `()`. `lower_effort(endpoint, effort)`
returns the next value below `effort` (remapping `high` to `xhigh` on ninfer
first) and `None` at the bottom, for a value outside the menu, or for an empty
menu. The retry rule: on a length stop (`EmptyCompletionError`, reasoning ate
the budget) retry once per level at `lower_effort`; stop at `None`. An unknown
endpoint raises `EndpointRegistryError`.

## Plugin opinions: seam, default, or razor verdict

Each hardcoded opinion either has a named seam and default, or a recorded
verdict (the scenario a user would need and why none does). The verdicts are
design reasoning, not measurements.

| Opinion | Seam and default, or razor verdict |
|---|---|
| JSON repair `max_edits` / `max_walks` | Seam: parameters of `repair_json_structure` (and `repair_and_evaluate_output`); defaults `MAX_REPAIR_EDITS=24`, `MAX_REPAIR_WALKS=1500`. `finalize_contract` and the CLI do not forward them: a caller that needs other bounds calls the library function. |
| `_MAX_CLOSER_RUN=8`, `_MAX_REF_DEPTH=16`, `_MISQUOTED_KEY_LOOKBACK=2` | Razor verdict: no seam. Scenarios considered: a run of more than 8 stray closers, a schema `$ref` chain deeper than 16, a misquoted key more than 2 tokens behind the failure. Each is outside the one-slip repair the module promises (a run that long is a different document, and the repair applies only when exactly one fit exists), and a deeper `$ref` chain is a schema fault the caller fixes. Raising them widens the guess space and the ambiguity risk without helping a slip. |
| Claude text-only flags (`_CLAUDE_TEXT_ONLY_ARGS`) and codex text-only config and catalog sets (`_CODEX_TEXT_ONLY_CONFIG`, `_CODEX_TEXT_ONLY_CATALOG_SET`, `_CODEX_TEXT_ONLY_CATALOG_DROP`) | Razor verdict: no seam. They ARE the guarantee the mode advertises (no filesystem write, shell or subagent); a user-editable set would let the advertisement and the argv disagree. Scenarios considered: a user wanting one tool kept (then they do not want text-only mode; use a mode that allows it), a new codex tool source (a plugin change, re-measured per "A guarantees requirement admits AND arms"). |
| Effort `_MENUS` | Razor verdict: no seam. The tables mirror what each wire style accepts (ninfer rejects `high` with a 400). A user-set menu could only disagree with the server; a server with a different menu declares a different `effort_style` in the registry, which is the existing seam. |

**Home of the codex temp files.** `codex_out_*.txt`, `codex_schema_*.json` and
`codex_catalog_*.json` (the text-only `model_catalog_json`) are created with
`tempfile.mkstemp` in the OS temp directory and deleted in the call's
`finally`. Per plugins/CLAUDE.md's definitions that is project-ephemeral
scratch in neither project nor user home: one call's private working file,
never read by another process after the call, and not derived from the
script's location (`__file__`, `BASH_SOURCE`, `$0`). It writes no durable
path.

That inequality is no longer folklore: **each adapter ADVERTISES it.** Every
backend class carries a `capabilities: ClassVar[Capabilities]`
(`completion/adapter_capabilities.py`), naming the params it honors, the ones it
drops, the constraints it emits, its structured-output mode, and how system text
reaches the model. Read it from the package API via `adapter_capabilities()`, or
from the `endpoints` CLI verb, whose payload carries a `capabilities` block
keyed by adapter family plus an `adapter` field on each endpoint.

**The one rule the advertisement obeys: a capability describes what the adapter
EMITS, never what the provider or CLI does with it.** Nothing here promises a
target HONORS a control -- no fake seam can establish that, and advertising it
would be the overclaim the advertisement exists to prevent. Two corollaries,
each got wrong once and now pinned by tests: **suppressing a flag is not a
control** (codex emits nothing for `network=False`), and **a value menu is
advertised only where the request-building code validates it**.

**A guarantees requirement admits AND arms; a guarantee needs a live check.**
`declaration.run` passes every call through `requirements.arm_call`, which
arms each request-sourced control the requirement relies on (claude-cli and
codex-cli text-only mode through the backend field `text_only`, opencode-cli
through `disallowed_tools`) and raises when one cannot be armed; `run()` then
refuses a response that does not report the armed control. A selector that
admits an entry it does not arm is a fake gate. Add a canonical subject to a
mode only after a live call shows no tool for it remains. For codex the model
CATALOG, not the config, supplies code mode, multi-agent, apply_patch and the
node REPL, so text-only mode patches the model's `codex debug models` entry and
ignores the user config (`-c mcp_servers={}` merges and removes nothing);
re-measure when codex adds a tool source. README "Text-only mode" has the argv
and the evidence.

**A protocol error is not an endpoint error.** The `complete` verb speaks a
versioned protocol both ways; `EXIT_PROTOCOL` (4) means nothing ran and retrying
the same bytes cannot help, distinct from `EXIT_FAILURE` (1), a call that ran
and may succeed on retry.

The full per-call contract -- the advertisement's shape, the truthful response
record, the per-key `extras` verdicts, the tool-denial and system-prompt-mode
asymmetries, and the request/result protocol with its exit codes --
is [references/completion-seam-contract.md](references/completion-seam-contract.md).
Read it before changing an adapter, a capability record, or the `complete` verb.

**The same call does not behave the same way.** Retry, timeout defaults, token
accounting (codex reports one undifferentiated `total_tokens` and no
input/output split at all; opencode's default output reports no usage), cost
(flat zero for the subscription CLIs, unavailable from opencode's default
output, real money for OpenRouter), and prompt delivery all differ -- the CLI
transports have one stdin prompt rather than a separate system channel.

So: uniform CALL SHAPE and uniform FAILURE VOCABULARY, not interchangeable
behaviour. Say so when documenting this layer rather than letting "four
transports behind one `complete()`" imply more than it delivers.

It does not own the concerns of a RUN OF MANY CALLS. Response caching, cost
accounting, budget guarding, batching, concurrency, and rate limiting all
belong to the caller. Those are policy, not transport: what a cache is keyed on,
what a budget is measured against, and how many calls may run at once are
questions the calling pipeline can answer and a transport cannot, so answering
them here would mean guessing once on behalf of every caller. Output validity is
split the same way. The seam enforces a contract the CALLER declares: the caller
answers what a valid output looks like by declaring an `OutputContract` (a JSON
Schema and a policy), and the transport only checks the returned object against
that declaration; it never invents a schema. What to do with a structurally
valid object -- semantic or domain validation, retry, caching -- stays with the
caller. A declared contract is refused before dispatch until an adapter
advertises a policy for it. Holding that altitude is also what keeps this layer
stdlib-only apart from a lazy `openai` import (the schema validator is a stdlib
JSON Schema subset in `completion/json_schema.py`), so a consumer
driving only `claude-cli` installs no SDK. content-pipeline-kit's
`lib/content_pipeline/llm/platform.py` is the in-fleet implementation of the
layer above.

**Two behaviours look like exceptions to that split and are not.**
`ClaudeCliBackend` CAN retry a transient 5xx envelope (`retry_max_attempts`,
`retry_cooldown_s`) and enforces a per-call timeout -- both are properties of
the subprocess it spawns, not run-level policy, and 429 / 401 never retry
because they persist. `OpenRouterBackend` makes exactly one attempt and leaves
retry to the caller, which holds the run-level context that decision needs.

## The front door is a separate surface, and it is where cross-caller concurrency lives

The run-of-many rule above governs the COMPLETION SEAM: one process, one
`complete()`, no policy. `llm_scripting_kit.frontdoor` is not that seam. It is
an OpenAI-compatible HTTP server (`llm-scripting-kit frontdoor`, launched by
`scripts/frontdoor.sh` from the plugin venv, `--print-command` supported) that
sits in FRONT of the registry's transport entries and arbitrates admission
ACROSS every caller on the LAN -- job-kit runs, content-pipeline waves, opencode
sessions, a curl. That is the one concurrency concern no caller can own, because
no caller can see the others' in-flight requests; a bounded local model server
queues or 503s whoever arrives fifth, so the arbitration has to sit where all
the requests pass through. The seam stays run-once; the front door owns the
run-of-many across processes.

What it reads: `routing:` on a transport entry in the user's model-endpoints
registry -- `group` (the model name callers send), `order` (tier; lower fills
first), `max_parallel` (cap; omitted = uncapped, only sensible on the last
tier), `effort_style` (`top-level`, `ninfer` = top-level with `high` mapped to
`xhigh`, `chat_template_kwargs`, or `unsupported`; omitted = top-level, and an
entry-level `effort_style` overrides it). Fill-first: the lowest tier fills to its
cap, the next tier takes the excess, and `--spill-after` (default 0 s) is how
long a request waits for a slot in a lower tier before spilling. A transport
entry WITHOUT `routing:` is not a deployment, which is how the front door's own
registry entry stays out of its own tiers; `--check` lists every untagged
transport entry so a forgotten tag is visible rather than silently absent.

What it owns and what it does not: in-memory in-flight counts (hence ONE uvicorn
worker, enforced in `main`), one retry onto the next deployment on a connection
error or 5xx, the `user` field stripped after logging (no backend sees it), a
JSONL access log, `/health` (liveness plus per-deployment counts, never an
upstream probe) and `/who` (in-flight requests with their `user` ids). It does
NOT own cost accounting, caching, or budget guarding -- those stay with the
caller exactly as the seam rule says. Keys for keyed deployments resolve through
`api_key.get_api_key`, so the hosting machine needs the secrets layer, not an
exported env var.

**Marker and billing are explicit metadata, never inferred.** `frontdoor: true`
(strict boolean) on a transport entry says only that its `base_url` is a front
door and its `model` names the group; it is not derived from `routing`, ids,
hosts, ports, or `/v1/models`. An invalid value is noted, treated as false, and
the entry (even the default) is kept. `billing.mode` (`unmetered` or
`provider-reported`) is separate and entry-local: `unmetered` is an explicit
USD zero, `provider-reported` declares native `usage.cost` is USD, omission
declares neither. `key_env` stays independent of both. Both keys surface in the
`endpoints` JSON (`frontdoor`, and `billing` when set) and in
`models.resolve_endpoint()` (`frontdoor`, `billing_mode`).

**Reported cost has one read point and a trust gate.** The front door strips
upstream `usage.cost` / `usage.cost_source` unless the serving deployment is
`provider-reported`, and injects `cost: 0.0` + `cost_source: registry-unmetered`
only for `unmetered`. `OpenRouterBackend` reads native `usage.cost` once and
accepts it only for a `frontdoor: true` or `provider-reported` endpoint (an
unmarked provider cannot opt in via response content). The paired
`LLMResponse.reported_cost_usd` / `reported_cost_source` are both present or
both `None`, finite and non-negative; invalid values are ignored, never coerced
to zero. Streaming is not normalized.

**Backend health is a preference with a fallback, inside one budget.**
`GET /health` is liveness only (never an upstream probe). `GET /health/backends?budget_ms=N`
(protocol 1) probes the app's OWN loaded registry entries (`account.probe_entry`,
explicit timeout, never the 2 s default and never a re-load from disk), and
`reachability.check_transport` asks it only for a `frontdoor: true` entry. The
health socket gets `HEALTH_SHARE` of the client timeout, the requested inner
budget is strictly below that socket budget, and the `/models` fallback gets only
what is left, so health plus fallback never exceed the caller's one timeout.
Anything other than a decisive `reachable`/`unreachable` group verdict falls back.
Do not make the front door's group aggregate the only signal: a group with an
uncapped paid tier reads `reachable` even when local deployments are down, so the
per-deployment statuses stay visible in the detail.

**The seam is RUN-ONCE by default: one request, at most one invocation.**
`retry_max_attempts` defaults to 1, so the claude retry is opt-in and
`LLMResponse.attempts` above 1 is evidence of a caller's own policy rather than
of hidden adapter behaviour. The budget was kept rather than deleted because the
transient-5xx case is real; what was wrong was doing it invisibly underneath a
caller that runs its own retry loop. One consequence is worth stating because it
was a latent bug: a transient envelope that survives the budget now RAISES, in
the canonical `"api_error_status":NNN` form the halt matchers read. It used to
fall through and be reported as a completed call with empty text -- the retry
loop had been hiding a failure the contract now names.

## Insights

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/llm-scripting-kit
    covers:
      - the local model-server entry points and their profiles
      - the single-call altitude and the shared halt taxonomy
      - the runner seam shared by the CLI-backed completion backends, and where
        that seam is NOT uniform across transports
      - usage pacing (`conserve_usage`), its declared pools, its fail-open rule,
        and the de-prioritize / disable split its two thresholds produce
      - the per-transport rules the Codex and OpenCode backends carry
      - "the front door: the `frontdoor` verb and launcher, the transport-only
        `routing:` keys, fill-then-spill ordering, the single-worker constraint
        and `--check`"
      - "the `swapper` CLI group: its process-safety rules, exit codes, and
        the fail-open launcher guard"
    excludes:
      - codex dispatch mechanics (orchestrate's codex-dispatch.md)
      - codex dispatch mechanics and endpoint compatibility (awesome-kit's
        orchestrate skill, references/codex-dispatch.md)
  insights:
    - id: run_cli_streaming_rename
      keywords: [run_claude_streaming, run_cli_streaming, back-compat alias, claude_runner, content-pipeline-kit, shared lib rename, transport-neutral runner]
      summary: llm-scripting-kit's claude -p subprocess runner is now the transport-neutral run_cli_streaming; run_claude_streaming remains as a back-compat alias because llm-scripting-kit's own completion.backends imports it by name and re-exports it.
      detail: |
        The runner in
        plugins/llm-scripting-kit/lib/llm_scripting_kit/completion/claude_runner.py
        was always structurally generic -- cmd in, stdin written, both pipes drained
        on daemon threads, bounded timeout, caller-supplied hard-stop markers -- but
        was claude-BRANDED in its name and its two error strings. Adding a codex
        backend made the branding misleading, so it took a `label` parameter and the
        neutral name. The alias is load-bearing, not courtesy:
        llm_scripting_kit.completion.backends imports the old name and uses it as
        ClaudeCliBackend's default `runner`, and completion/__init__ re-exports it.
        content-pipeline-kit depends on it only transitively (its adapter delegates
        with runner=None and mentions the name in a docstring), so dropping the
        alias breaks llm-scripting-kit's own import first --
        test_completion_codex_backend.py::test_run_claude_streaming_alias_is_the_renamed_runner
        is what pins it. Re-run tests/llm-scripting-kit before touching it.
        Note the runner's `(stdout, stderr, returncode)` contract does NOT carry a
        codex result -- codex returns via `-o <FILE>` -- so CodexCliBackend manages a
        temp output file around the call rather than parsing stdout.
      origin: "2026-08-10 -- rename performed while adding CodexCliBackend alongside ClaudeCliBackend."
      added: "2026-08-10"
```
