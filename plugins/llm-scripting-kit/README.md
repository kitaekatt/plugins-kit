# llm-scripting-kit

LLM access for scripts and pipelines: key resolution, a shared model registry,
and named OpenAI-compatible endpoints. **OpenRouter is the default endpoint**,
so existing setups keep working unchanged.

Plugins in this marketplace that make LLM calls (workflow-kit,
content-pipeline-kit, ...) all read the same credentials and the same model
registry from this plugin, so keys are set up once and consumed everywhere.

> One name throughout: the plugin, the importable Python package
> (`llm_scripting_kit`), the CLI command (`llm-scripting-kit`), and the
> data/config namespace are all `llm-scripting-kit`. The name *OpenRouter*
> survives only where it names the service itself (`OPENROUTER_API_KEY`, the
> `openrouter` endpoint, openrouter.ai).

## What it does

- **Named OpenAI-compatible endpoints.** `config.yaml` has an `endpoints:` map;
  each endpoint carries its own `base_url`, `key_env`, model registry, and an
  `account_check` mode. `default_endpoint` (default: `openrouter`) is used when
  a caller names none. Point a script at OpenRouter today, a local vLLM or any
  OpenAI-compatible server tomorrow, without touching the code.
- **Key setup that validates before writing.** `llm-scripting-kit set-key`
  checks the key before anything lands on disk, so a typo is rejected instead
  of stored. For an OpenRouter endpoint it uses `GET /auth/key` and
  distinguishes HTTP 401 (key revoked/rotated -- get a new one) from HTTP 402
  (key is fine, account out of credit -- do not rotate); other endpoints use a
  generic `GET /models` probe or skip validation (`account_check: none`).
- **Resolution with source attribution.** A key resolves in order:
  `<endpoint key_env>` env var > project `.env`
  (`<project>/.local-data/plugins-kit/llm-scripting-kit/.env`) > user `.env`
  (`~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`). Keys for multiple
  endpoints coexist in the same `.env`. `llm-scripting-kit which` tells you which
  source won. The marketplace-less project path
  (`<project>/.local-data/llm-scripting-kit/.env`) predates the alignment with
  the project `config.yaml` layer, which has always been namespaced
  `<marketplace>/<plugin>`. It is still read, at lower precedence, and a key
  resolved from it is flagged (`KeyLookupResult.legacy_location`, plus a
  one-time stderr notice) rather than silently accepted.
- **Shared model registry.** A layered `config.yaml` maps aliases (plus
  `default` / `defaultCheap` selectors) to concrete slugs, per endpoint. One
  project override changes the model for every consumer at once.

## API

All calls take an optional `endpoint=` -- `None` means the default endpoint, so
every existing endpoint-less call behaves exactly as before:

```python
from llm_scripting_kit import get_api_key, make_openai_client, resolve_model

resolve_model("qwen")                         # default endpoint (openrouter)
resolve_model(cheap=True, endpoint="local")   # a named endpoint's defaultCheap
client = make_openai_client(endpoint="local") # OpenAI client for that endpoint
get_api_key(endpoint="local")                 # resolve that endpoint's key_env
```

Adding an endpoint (user or project `config.yaml` override):

```yaml
endpoints:
  local:
    base_url: http://localhost:8000/v1
    key_env: MY_VLLM_KEY
    account_check: none          # or models-probe to GET /models
    default: llama
    models:
      llama: {slug: meta-llama/Llama-3.1-8B-Instruct}
```

## Completion seam

`llm_scripting_kit.completion` puts three transports behind one `complete()` so
a pipeline can switch between a paid HTTP endpoint and either of two
subscription-billed local CLIs -- `claude -p` and `codex exec`, billed on
separate accounts -- purely by configuration:

```python
from llm_scripting_kit.completion import (
    OpenRouterBackend, ClaudeCliBackend, CodexCliBackend, BackendOptions,
)

backend = ClaudeCliBackend()                       # or OpenRouterBackend(endpoint="local")
resp = backend.complete("system prompt", "user prompt", model="claude-opus-4-8")
print(resp.text, resp.input_tokens, resp.output_tokens)
```

`BackendOptions` carries per-call knobs (`max_tokens`, `temperature`,
`timeout_s`, `effort`, `allowed_tools`, `user_cache_prefix`, ...); transports
ignore the ones they do not understand. `ClaudeCliBackend` spawns via a shared,
battle-tested runner (UTF-8 pipes, daemon stdout/stderr drains, a bounded
per-call timeout raising `AgentTimeoutError`, and a live hard-stop kill on
rate-limit / auth markers). Persistent failures on either transport classify
into one halt taxonomy (`classify_halt_text`, `HaltError`, `HALT_*`) so an
orchestrator can halt-and-resume identically regardless of provider. The seam
types and the runner are stdlib-only; only `OpenRouterBackend` reaches for the
`openai` SDK, and only lazily.

The `claude-cli` backend needs the `claude` executable on PATH; it is already
provisioned via the `bootstrap` dependency (which declares `claude` as a tool),
so no extra install step is required.

## Key handling

Interactive `set-key` uses a hidden prompt (`getpass`), the `.env` file is
created with mode 0600 on Unix (at creation time, not post-hoc chmod), and
writes are atomic (temp file + rename).

## Install and first move

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install llm-scripting-kit
```

Then run `llm-scripting-kit status` (the plugin's `bin/` shim is on PATH). If no
key is set, run `llm-scripting-kit set-key` yourself -- the hidden prompt is
interactive, so an agent cannot drive it. The `openrouter-account` skill covers
verify / rotate / diagnose flows.

## When not to use

If you just export `OPENROUTER_API_KEY` yourself and have a single consumer,
you do not need this plugin. It earns its keep when several plugins or scripts
share credentials and you want validation, source attribution, a common model
registry, and pluggable endpoints.
