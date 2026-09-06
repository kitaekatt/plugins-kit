# llm-scripting-kit

The installed `llm-scripting-kit` command is the host-neutral interface to the
shared endpoint registry and completion backends:

```bash
llm-scripting-kit endpoints
llm-scripting-kit endpoints --verify
llm-scripting-kit probe --endpoint sol
llm-scripting-kit usage
llm-scripting-kit models --endpoint openrouter
llm-scripting-kit resolve --endpoint sol
printf 'Review this design' | llm-scripting-kit complete --endpoint sol
llm-scripting-kit complete --endpoint openrouter --model qwen \
  --system-file system.txt --prompt-file prompt.txt
```

Discovery and completion commands emit JSON by default. `complete --format
text` prints only the response text. Exit codes are `0` for success, `1` for a
runtime failure, `2` for invalid input/configuration, and `3` for a classified
persistent halt such as authentication, credit, or rate limiting. The existing
`status`, `set-key`, and `which` account commands retain their human-readable
output.

### `endpoints --verify` / `probe` -- configured vs. usable right now

`endpoints` lists what is CONFIGURED (adapter, base_url, model, name): pure
static data, always instant, never touches the network. It carries no
liveness information on its own -- two locally hosted endpoints can be listed
identically while only one actually answers.

`--verify` (opt-in, default off so plain `endpoints` never starts paying for
network calls silently) adds a `reachability` object to every endpoint in the
same JSON, checked concurrently so a full-list verify is not the sum of every
individual timeout:

```bash
llm-scripting-kit endpoints --verify --timeout 5
```

```json
{"endpoints": {"local": {"kind": "transport", "base_url": "...",
  "reachability": {"status": "reachable", "checked": "models-endpoint", "detail": "ok"}}}}
```

`probe --endpoint NAME` is a thin exit-code wrapper over the *same*
reachability check for one endpoint -- for a caller that wants a yes/no answer
before queueing work rather than a listing:

```bash
llm-scripting-kit probe --endpoint local; echo $?
```

Both share one code path (`llm_scripting_kit.reachability`) and **never issue
a completion, ever** -- verification costs zero LLM tokens:

- **transport** endpoints (the `openrouter` adapter, including a self-hosted
  OpenAI-compatible server): a `GET {base_url}/models` metadata request. This
  proves the server is up and answering HTTP -- it does **not** prove a
  completion would succeed. A model can be unloaded or a worker wedged behind
  a perfectly healthy `/models` response, which is why a passing verdict is
  `"status": "reachable"`, never `available` or `healthy` -- those would claim
  more than a metadata probe can support.
- **harness** endpoints (`claude-cli`, `codex-cli`, `opencode-cli`): the
  underlying CLI resolves on PATH and answers `--version` within the timeout.
  This establishes the harness is *invocable*, not that a completion would
  succeed -- a real completion would spawn an agent and cost real time (and,
  for a subscription CLI, real quota), so it is never attempted. `codex-cli`
  prefers `bootstrap_lib`'s cached detector when that optional shared lib is
  importable, and falls back to the identical PATH + `--version` check the
  other two harnesses use when it is not -- the absence of an optional
  dependency of *this plugin's own* is not evidence about whether codex
  itself is installed and working.

#### Three-state status, not a bool

`reachability.status` is one of three strings, never a bare `true`/`false`:

| `status` | Meaning | `endpoints --verify` | `probe` exit code |
|---|---|---|---|
| `reachable` | The check ran and the target answered. | field present, `status: "reachable"` | `0` |
| `unreachable` | The check ran and the target did **not** answer (dead host, missing CLI, nonzero exit, timeout, ...). | field present, `status: "unreachable"` | `1` |
| `unknown` | The check itself could **not** be run to a verdict (an optional dependency was unavailable, or the check machinery raised unexpectedly). | field present, `status: "unknown"` | `5` |

A bare `reachable: bool` cannot distinguish "I checked and it is down" from "I
could not check" -- collapsing the second into `false` is a false negative a
caller cannot see: gating on `reachable is False` would skip a perfectly
usable endpoint whose check merely failed to run. `unknown` exists so that
misreading is structurally impossible -- the same honesty rule that produced
the `reachable`/`available` distinction above, applied one level down.

`probe`'s exit codes, stated fully:

| Exit | Meaning |
|---|---|
| `0` | `status: "reachable"`. |
| `1` | `status: "unreachable"`. |
| `2` | The `--endpoint` NAME does not resolve to any configured endpoint at all -- a configuration error, decided *before* any check is attempted (same code the other verbs use for a bad endpoint name). |
| `5` | `status: "unknown"` -- the check was attempted against a real, configured endpoint, but could not run to completion. **Never conflate this with `1`**: a caller branching on "nonzero means down" must special-case `5` rather than treating it as a failure verdict about the endpoint. |

`--timeout` defaults to 5 seconds (`reachability.DEFAULT_VERIFY_TIMEOUT_S`):
short on purpose, because a caller reaching for either of these is asking
precisely because it does not want to block a queued unit of work on a dead
target. A live `/models` endpoint or a present CLI's `--version` answers in
well under a second; 5s is headroom for a slow hop while still failing a
genuinely dead target fast.

### `usage` -- is an opted-in model being spent too fast?

A third axis over the two above: an endpoint can be configured and reachable
and still be one you should leave alone this week. An entry opts in with
`conserve_usage`, and its quota state then has one of two effects: an endpoint
whose pool is **spent** is disabled and leaves selection, while one that is
merely **being spent faster than the clock** is de-prioritized -- still usable,
but it loses to an equally-suitable endpoint that is not behind pace.

```yaml
# ~/.claude/config/llm-scripting-kit.yaml -- the fleet layer, so this reaches
# every machine that clones your profile.
endpoints:
  fable:
    conserve_usage: {pool: model_scoped, display_name: Fable}  # its own weekly bucket
  opus:
    conserve_usage: {pool: seven_day}                          # all-model weekly
  sol:
    conserve_usage: true                                       # codex's principal window
```

Each entry names its OWN pool, because they are different quotas: a per-model
weekly bucket for one model, the all-model weekly window for another. Nothing
is opted in by default.

```bash
llm-scripting-kit usage            # this session's pinned verdicts
llm-scripting-kit usage --no-pin   # evaluate now, without reading or writing the pin
```

Four statuses: `available`, `under-quota` (de-prioritized), `out-of-quota`
(disabled), and `no-data`. **`no-data` never withholds or de-prioritizes a
model** -- a missing snapshot, an absent pool, or a window that has already
reset all leave the endpoint fully usable, the same way `probe` reports
`unknown` rather than claiming an endpoint is down.

`choose` applies that to a preference order you state:

```bash
llm-scripting-kit choose --prefer opus,sol --default openrouter
```

Both fine, you get `opus` -- your stated order. `opus` out of quota, you get
`sol`. Both out of quota, you get the default. `opus` under quota and `sol`
fine, you get `sol` -- but `opus` stays in the returned chain, because
de-prioritized is not disabled. Both under quota and your order decides again.
Add `--json` for the whole ranking, the disabled endpoints, and the reason.

The numbers come only from files the harnesses already write: claude-ui-kit's
statusline snapshot for claude, the newest `~/.codex/sessions` rollout for
codex. Nothing here reads a credential or calls a usage API, so what a CLI does
not expose shows up as `no-data` rather than being fetched with a token.

A verdict is computed once per session and reused, so a model that was
available when your session started does not become unavailable partway
through. An `under-quota` or `out-of-quota` verdict is recomputed once its
window resets, which can only give capacity back.

`seats` applies the same check: an out-of-quota seat is reported under
`out_of_quota` rather than in `seats`, so "no seat above me" stays
distinguishable from "the seat above me is spent"; an under-quota seat stays in
`seats` and sorts after an equally-suitable peer that is not behind pace.

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
  (`~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`) > the endpoint's
  configured `key_file` (a bare-value credential file whose whole, stripped
  content is the key; source `key_file`). Keys for multiple
  endpoints coexist in the same `.env`. `llm-scripting-kit which` tells you which
  source won. The marketplace-less project path
  (`<project>/.local-data/llm-scripting-kit/.env`) predates the alignment with
  the project `config.yaml` layer, which has always been namespaced
  `<marketplace>/<plugin>`. It is still read, at lower precedence (but above
  `key_file`), and a key
  resolved from it is flagged (`KeyLookupResult.legacy_location`, plus a
  one-time stderr notice) rather than silently accepted.
- **Shared model registry.** A layered `config.yaml` maps aliases (plus
  `default` / `defaultCheap` selectors) to concrete slugs, per endpoint. One
  project override changes the model for every consumer at once.
- **Local server launch profiles.** The canonical `model-server.sh` script owns
  the measured NInfer argument sets for Qwen3.6 and Qwen3.8, plus a `qwen38l`
  llama.cpp profile for the same model as a comparable second backend. Claude
  calls it through `${CLAUDE_PLUGIN_ROOT}`; `qwen36-server`, `qwen38-server`,
  and `qwen38l-server` are thin PATH adapters for interactive shells.

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

`llm_scripting_kit.completion` puts four transports behind one `complete()` so
a pipeline can switch between a paid HTTP endpoint and local CLIs --
`claude -p`, `codex exec`, or `opencode run` -- purely by configuration:

```python
from llm_scripting_kit.completion import (
    OpenRouterBackend, ClaudeCliBackend, CodexCliBackend, OpencodeCliBackend,
    BackendOptions,
)

backend = ClaudeCliBackend()                       # or OpenRouterBackend(endpoint="local")
resp = backend.complete("system prompt", "user prompt", model="claude-opus-4-8")
print(resp.text, resp.input_tokens, resp.output_tokens)
```

`BackendOptions` carries per-call knobs (`max_tokens`, `temperature`,
`timeout_s`, `effort`, `allowed_tools`, `user_cache_prefix`, ...); transports
ignore the ones they do not understand. An unset `temperature` is omitted from
OpenAI-compatible requests so the server/model can choose its mode-aware
default; an explicit value is sent. The CLI backends use a shared,
battle-tested runner (UTF-8 pipes, daemon stdout/stderr drains, and a bounded
per-call timeout raising `AgentTimeoutError`). `OpencodeCliBackend` returns
default-format stdout, reports zero usage because that format supplies no
usage envelope, and injects a workspace-confining policy around required `--auto`
permissions because `--dir` alone does not confine writes. Persistent failures classify
into one halt taxonomy (`classify_halt_text`, `HaltError`, `HALT_*`) so an
orchestrator can halt-and-resume identically regardless of provider. The seam
types and the runner are stdlib-only; only `OpenRouterBackend` reaches for the
`openai` SDK, and only lazily.

The `claude-cli` backend needs the `claude` executable on PATH, and the
`opencode-cli` backend needs `opencode` on PATH. The former is already
provisioned via the `bootstrap` dependency (which declares `claude` as a tool);
OpenCode is a caller-provided CLI.

### Capability requirements

`llm_scripting_kit.completion.match_capabilities(capabilities, requirements)`
answers whether one adapter's advertised `Capabilities` (or its serialized
`to_json()` mapping) satisfies a requirement. It is the matching language for
the advertisement above, so a caller selecting among endpoints does not
maintain its own capability vocabulary.

`requirements` is `None` or `{}` to match anything, a list as shorthand for
`{"params": [...]}`, or a mapping using named convenience keys: `params`
(aliases `required_params`, `honors` -- a list of required param names, or a
mapping for nested per-param requirements, where `False` means "must be
absent"), `execution_controls` (alias `controls` -- required control ids),
`dropped_params` (required dropped-param names), `structured_output` (alias
`structured` -- a mode string, a result string, or a mapping), and
`system_prompt` (alias `system_prompt_mode` -- a mode string or a mapping).
Any other key is read as a dotted path over `Capabilities.to_json()` (e.g.
`"adapter"`, `"structured_output.mode"`), so the function carries no
capability table of its own -- it only knows how to walk the advertisement's
JSON shape.

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
