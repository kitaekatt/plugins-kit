# CLAUDE.md -- llm-scripting-kit plugin

Guidance for an AI agent working in this plugin. llm-scripting-kit is the
fleet's LLM ACCESS layer: it answers which endpoint, which model, which key,
which transport, and how a configured local model server starts. `README.md` is
the human-facing
description of the same surface; `skills/openrouter-account/` owns key
management.

## Local model server entry points

`scripts/model-server.sh` is the canonical Claude-first launcher. Invoke it as
`${CLAUDE_PLUGIN_ROOT}/scripts/model-server.sh qwen36|qwen38|qwen38l`; this
works from the plugin root Claude actually loaded and does not assume a plugin
`bin/` directory is on PATH. `bin/qwen36-server`, `bin/qwen38-server`, and
`bin/qwen38l-server` are thin shell adapters for interactive environments that
deliberately put them on PATH.

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
and opencode but not OpenRouter; `cwd`
reaches the three CLI backends and not OpenRouter, which is why `cwd` is not a
core param of this seam. `temperature` and `max_tokens` are accepted and then
dropped by all CLI backends -- dropped and REPORTED, not ignored.

That inequality is no longer folklore: **each adapter ADVERTISES it.** Every
backend class carries a `capabilities: ClassVar[Capabilities]`
(`completion/adapter_capabilities.py`), naming the params it honors, the ones it
drops, the constraints it emits, its structured-output mode, and how system text
reaches the model. Read it from the package API via `adapter_capabilities()`, or
from the `endpoints` CLI verb, whose payload carries a `capabilities` block
keyed by adapter family plus an `adapter` field on each endpoint.

**The one rule the advertisement obeys: a capability describes what the adapter
EMITS, never what the provider or CLI does with it.** `ExecutionControl.emits`
names the exact argv element, environment key path, or request field produced,
and a seam test in `tests/llm-scripting-kit/test_completion_capabilities.py`
asserts every one of them. No fake seam can establish that a target HONORS a
control, so nothing here claims it. Two corollaries that were each got wrong
once and are now pinned by tests:

- **Suppressing a flag is not a control.** codex emits nothing at all for
  `network=False`; the absence of a flag is not a deny, and no such control is
  advertised.
- **A value menu is advertised only where the request-building code validates
  it.** `CodexCliBackend` calls the shared argv builder directly and bypasses
  `CodexAdapter`'s effort validation, so codex advertises no effort `values`.

When an adapter changes what it emits, its record changes in the same commit.
The capability facts live in adapter code and are hand-authored per adapter --
codex's argv is built in `bootstrap_lib.codex`, a different plugin, so deriving
the map mechanically would mean reaching across that boundary. The seam tests
are what keep the hand-authored record honest; a param applied but unadvertised
is what they exist to catch. Never keep a second copy of these facts in YAML or
a docs table: that copy is free to disagree with the adapter, which is the drift
the advertisement replaces.

**What the advertisement is BEFORE the call, the response is AFTER it.**
`LLMResponse` carries what actually happened on one call: `dropped_params` (the
params this caller requested that the adapter does not read), `forwarded_params`
(params sent downstream without the adapter validating them),
`execution_controls_applied` (the ids of advertised controls the request
actually emitted), `structured` (schema-backed output, parsed), `started_at` /
`ended_at`, and `status` / `error`. The two halves are one system, not two
lists: `completion/results.py` DERIVES the per-call report from the adapter's
own record -- `derive_dropped_params` intersects the advertised dropped set with
what the caller actually set, and `check_applied_controls` refuses an id the
advertisement does not carry. A second hand-maintained list is exactly the drift
the SSOT rule forbids, and here it would be a drift between two claims about the
same call.

**`extras` is reported per KEY, and dropped is not the only verdict.** The
`extras` map is where the seam's inequality is sharpest: codex reads a named
set, advertised as `extras.<key>` params in `completion/adapter_capabilities.py`
and pinned against the argv builder's own list by
`test_codex_extra_keys_match_the_advertisement`; openrouter copies EVERY key
into the request as a top-level parameter without validating it; and claude-cli
and opencode-cli read none. So the report answers per key,
via `derive_extras_report`, and the bare field name `extras` never survives into
a response -- on codex it would have no single answer, and on the others it
would not say WHICH key went nowhere.

The third verdict is the one that is easy to get wrong. An unadvertised
openrouter extra is FORWARDED, not dropped: reporting it as dropped would be a
fresh overclaim in place of the old silence, since the key really is sent and
the adapter's only claim is that it did not check it. `dropped_params` and
`forwarded_params` therefore stay separate fields, because they carry opposite
advice -- remove this param, versus this param reached the provider and may be
doing its job. The advertisement is what distinguishes them: a generic `extras`
param with `handling: passthrough` means forwarded, a per-key
`extras.<name>` entry means honored, and neither means dropped.

**Tool denial exists on exactly one adapter, and the record says so.**
claude-cli's `--disallowedTools` is the only real DENY channel across the four:
`allowed_tools` is an ALLOW-list and cannot express denying an arbitrary tool
without a complete tool universe, which no adapter has. codex constrains
RESOURCES (sandbox, network) rather than tools, opencode injects fixed scalar
permission denials of its own, and openrouter has no tool surface at all. So
the honest answer differs per adapter, which is why there is no flat
`tool_deny` enum -- one word would be false for at least two of them.

The deny control is the adapter's one CONDITIONAL control, and the asymmetry
with `allowed-tools` beside it is deliberate. `--allowedTools` is emitted on
every call, passing `""` when the caller named no tools, because an empty
ALLOW-list is a real restriction (allow nothing) and is therefore reported.
An empty DENY-list restricts nothing, so an unset `disallowed_tools` emits no
flag and reports no control -- suppressing a flag is not a control.

**`system_prompt_mode` is a real mode choice, not two spellings.**
`--system-prompt` makes the caller's text the WHOLE system prompt;
`--append-system-prompt` adds it to the CLI's own default prompt. The model
ends up with different text, so the advertisement carries both under
`system_prompt.modes` with a per-mode `emits_by_mode`, and `replace` remains
the default so an existing caller's request is unchanged. The param advertises
a `values` menu because the adapter REJECTS an unknown mode before dispatch --
forwarding one would silently hand the caller `replace` while they believed
they had asked for an append, and would make the advertised menu a claim no
code backs. `_CLAUDE_SYSTEM_PROMPT_FLAGS` is the single map behind both the
validation and the advertised menu, so a mode cannot reach the argv without
appearing in the record.

As everywhere here, both are claims about the argv this adapter BUILDS. Nothing
establishes that the CLI honors a deny or that the appended text survives --
no seam test can, and claiming it would be the overclaim the advertisement
exists to prevent.

Reporting the applied controls is per-adapter work, and deliberately so: only
the code that builds the request knows what it emitted. `source` does not settle
it -- codex's `sandbox-mode` is `source=REQUEST` yet emitted on every call
because it has a default -- so `CodexCliBackend` reads its controls off the
BUILT ARGV (the argv is the emission, and codex's argv is built in another
plugin), `OpencodeCliBackend` reads its advertised `FIXED` set because all of
its controls are unconditional, and `ClaudeCliBackend` names its three
unconditional ones. `--allowedTools` goes out even when the caller named no
tools: an empty allow-list is an emitted allow-list, not a suppression, so it is
reported.

**Structured output is parsed only under a caller schema.** codex advertises
`result: parsed` and the adapter parses its `-o` file into `structured` when --
and only when -- `extras.output_schema` was sent. Valid JSON a model produced
unbidden is not schema-backed output, and presenting it as such would be the
overclaim the advertisement exists to prevent; an unparseable result under a
schema leaves `structured` None rather than failing the call, because `text`
still carries the answer verbatim.

**Error-as-data lives at the CLI surface only.** The package API keeps RAISING
typed exceptions -- every existing consumer branches on them, and returning a
failure there would make it read as a SUCCESS at call sites that never asked for
this contract. The `complete` verb emits a failure in the same envelope shape as
a success (`status` of `completed` / `timeout` / `error`, plus an `error` object
carrying the halt classification as its `code`), on stdout, with exit codes
unchanged as the shell-level signal.

**The `complete` verb speaks a versioned protocol in BOTH directions.** The
result envelope carries `protocol` (`request_protocol.PROTOCOL_VERSION`)
alongside the unchanged `endpoint` / `kind` / `backend` / `response` keys, and
`--request-file <path|->` accepts a versioned JSON request instead of flags.
`request-schema` prints the accepted shape, derived from `BackendOptions` rather
than hand-written.

The request half exists because flags ran out, not for symmetry: `extras` is an
open JSON map whose per-key handling is observable in the response, and no flag
spelling expresses it. `allowed_tools`, `disallowed_tools` and
`system_prompt_mode` had no CLI path at all. A request and the call-describing
flags are REFUSED together rather than merged -- a precedence rule is invisible
at the call site, so a flag silently overriding a request field would be the
kind of lie this contract removes.

**`request_protocol.py` is where declared types become enforced ones.**
`capabilities.py` says `ParamCapability.type` is a declared expectation and that
"validation belongs at the CLI request boundary"; this is that boundary. Note
the one place it deliberately does NOT follow the seam's drop-and-report rule:
an unknown `options` key is a protocol ERROR, not a dropped param. The seam
drops what an ADAPTER does not read -- a fact about the adapter, reported
truthfully -- whereas a key that is no `BackendOptions` field at all is a fact
about the caller, which nothing advertises and no adapter could read, so there
is no honest per-call report to make.

**A protocol error is not an endpoint error, and the exit codes say so.**
`EXIT_PROTOCOL` (4) means the request could not be understood and no call was
attempted; it writes its envelope to STDERR, leaving stdout as the result
channel a consumer parses for one shape only. That is distinct from
`EXIT_USAGE` (2), which is argparse's territory -- a person typing a command
wrong -- and from `EXIT_FAILURE` (1), which means a call ran and failed and may
succeed on retry. Retrying a malformed request cannot help; the codes exist so a
consumer can tell those apart without parsing prose.

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
accounting, budget guarding, batching, concurrency, rate limiting, and
structured-output enforcement all belong to the caller. Those are policy, not
transport: what a cache is keyed on, what a budget is measured against, how many
calls may run at once, and what a valid output looks like are questions the
calling pipeline can answer and a transport cannot, so answering them here would
mean guessing once on behalf of every caller. Holding that altitude is also what
keeps this layer stdlib-only apart from a lazy `openai` import, so a consumer
driving only `claude-cli` installs no SDK. content-pipeline-kit's
`lib/content_pipeline/llm/platform.py` is the in-fleet implementation of the
layer above.

**Two behaviours look like exceptions to that split and are not.**
`ClaudeCliBackend` CAN retry a transient 5xx envelope (`retry_max_attempts`,
`retry_cooldown_s`) and enforces a per-call timeout -- both are properties of
the subprocess it spawns, not run-level policy, and 429 / 401 never retry
because they persist. `OpenRouterBackend` makes exactly one attempt and leaves
retry to the caller, which holds the run-level context that decision needs.

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
      - the per-transport rules the Codex and OpenCode backends carry
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
