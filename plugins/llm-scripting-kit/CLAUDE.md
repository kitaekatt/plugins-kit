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
EMITS, never what the provider or CLI does with it.** Nothing here promises a
target HONORS a control -- no fake seam can establish that, and advertising it
would be the overclaim the advertisement exists to prevent. Two corollaries,
each got wrong once and now pinned by tests: **suppressing a flag is not a
control** (codex emits nothing for `network=False`), and **a value menu is
advertised only where the request-building code validates it**.

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
