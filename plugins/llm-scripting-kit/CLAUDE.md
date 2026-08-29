# CLAUDE.md -- llm-scripting-kit plugin

Guidance for an AI agent working in this plugin. llm-scripting-kit is the
fleet's LLM ACCESS layer: it answers which endpoint, which model, which key,
which transport, and how a configured local model server starts. `README.md` is
the human-facing
description of the same surface; `skills/openrouter-account/` owns key
management.

## Local model server entry points

`scripts/model-server.sh` is the canonical Claude-first launcher. Invoke it as
`${CLAUDE_PLUGIN_ROOT}/scripts/model-server.sh qwen36|qwen38`; this works from
the plugin root Claude actually loaded and does not assume a plugin `bin/`
directory is on PATH. `bin/qwen36-server` and `bin/qwen38-server` are thin shell
adapters for interactive environments that deliberately put them on PATH.

The two profiles preserve their measured GPU settings while keeping paths and
ports overridable through environment variables. Qwen3.6 resolves NInfer from
`NINFER_ROOT` or conventional dev roots and uses INT8 KV plus MTP3. Qwen3.8
resolves llama.cpp and its GGUF from their existing environment overrides and
uses full GPU offload plus Q8 KV. Both bind localhost by default; broader
network exposure is an explicit host override.

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

**The `model` id is not portable.** An OpenRouter slug means nothing to
`claude -p`, and codex requires fully-qualified ids. Nothing here translates
them, so choosing a backend is really choosing a backend AND a model id.
content-pipeline-kit's `routed_model()` is the in-fleet compensation for this,
and its existence is the evidence: a genuinely uniform seam would not need it.

**`BackendOptions` is a union, not a neutral description of the work.**
`user_cache_prefix` is OpenRouter-only; `allowed_tools` is claude-cli-only;
`effort` reaches claude-cli, codex, and opencode but not OpenRouter.
`temperature` and `max_tokens` are accepted and then SILENTLY IGNORED by all
CLI backends.

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
`ClaudeCliBackend` retries a transient 5xx envelope (`retry_max_attempts`,
`retry_cooldown_s`; 3 attempts over 60s) and enforces a per-call timeout -- both
are properties of the subprocess it spawns, not run-level policy, and 429 / 401
never retry because they persist. `OpenRouterBackend` makes exactly one attempt
and leaves retry to the caller, which holds the run-level context that decision
needs.
