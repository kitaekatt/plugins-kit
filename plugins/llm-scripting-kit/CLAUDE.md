# CLAUDE.md -- llm-scripting-kit plugin

Guidance for an AI agent working in this plugin. llm-scripting-kit is the
fleet's LLM ACCESS layer: it answers which endpoint, which model, which key,
and which transport -- then makes one call. `README.md` is the human-facing
description of the same surface; `skills/openrouter-account/` owns key
management.

## Scope: one call, made correctly

This layer owns everything a SINGLE completion needs -- endpoint resolution,
the model registry, credential lookup with source attribution, prompt-cache
message shaping, and a shared halt taxonomy (`classify_halt_text`, `HaltError`
in `completion/halt.py`) so a persistent failure classifies identically
whichever transport produced it. Two transports sit behind one `complete()`:
`OpenRouterBackend` over HTTP, and `ClaudeCliBackend` driving the local
`claude -p` CLI.

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
