# llm-scripting-kit

The installed `llm-scripting-kit` command is the host-neutral interface to the
shared endpoint registry and completion backends:

```bash
llm-scripting-kit endpoints
llm-scripting-kit endpoints --verify
llm-scripting-kit probe --endpoint sol
llm-scripting-kit usage
llm-scripting-kit models --endpoint openrouter
llm-scripting-kit describe fable sol opus --self opus
llm-scripting-kit record-halt sol
llm-scripting-kit resolve --models sol,opus
printf 'Review this design' | llm-scripting-kit complete --models sol,opus
llm-scripting-kit complete --models openrouter --model qwen \
  --system-file system.txt --prompt-file prompt.txt
```

`--models` takes a model declaration: registry ids, comma-separated or
repeated, in the order you prefer them. `resolve` and `complete` use the first
usable entry of the pace-ordered list (see `describe` below), and `--model`
overrides the model id of whichever entry is chosen. `resolve` and `complete`
take no `--endpoint` flag; name one entry as `--models NAME`.

Discovery and completion commands emit JSON by default. `complete --format
text` prints only the response text. Exit codes are `0` for success, `1` for a
runtime failure, `2` for invalid input/configuration, `3` for a classified
persistent halt such as authentication, credit, or rate limiting, `4` for a
protocol error (`complete --request-file` could not understand the request,
so nothing ran), and `5` for an indeterminate reachability check (`probe`
attempted a check that could not run to a verdict). The existing
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

### `describe` -- a model declaration on this machine

`describe` applies all three axes to a model declaration, the ordered list of
registry ids you would let do a unit of work:

```bash
llm-scripting-kit describe fable astra sol opus sonnet --self opus
```

```
Declared models (ordered by pace): choose one; default is marked.
  fable   claude/agent   under quota  38% left, 50% of window   pace 76%   [default]  shares seven_day with opus
  astra   codex          out of quota until 2026-09-19 15:34 UTC
  sol     codex          out of quota until 2026-09-19 15:34 UTC
  opus    claude/agent   under quota  38% left, 50% of window   pace 76%   [author]  shares seven_day with fable
  sonnet  claude/agent   n/a (unpaced)
Rule: any usable entry may be chosen; ...
```

**Pace** is `remaining / window_remaining`: 100% is exactly on pace, above is
ahead, below is behind. Entries that have a pace are re-sorted by it, highest
first, among their own positions; entries without one (unpaced, no reading,
out of quota, about to reset) keep their places, and ties keep your order. The
first usable entry is `[default]`. The status and `[default]` come from this
session's pinned verdict; only the pace number is read fresh.

What is shown and what is not:

- **Shown:** usable entries, out-of-quota entries with their reset time, and
  unreachable entries -- all real on this machine.
- **Hidden, silently:** an id that resolves to nothing, one this caller cannot
  route, one that fails `--requirements`, and one you `--exclude`. No notice or
  warning is printed for them.
- **The floor:** when no usable entry is left, `describe` exits `1` with a JSON
  error on stderr that lists EVERY declared id and what happened to it, in
  declared order. That is where a typo shows up.

### `record-halt` -- write an observed quota halt back

```bash
llm-scripting-kit record-halt ENTRY [--kind quota|credit] [--resets-at EPOCH] [--project-root DIR]
```

A session caller drives the harness itself, so the completion seam never sees
its quota or credit halt, and the pinned AVAILABLE verdict would keep the spent
entry `[default]` for the rest of the session. `record-halt` records the halt:
for an entry that declares `conserve_usage`, it pins the entry OUT-OF-QUOTA
under the current session key, and every later `describe` in the session shows
it out of quota until its reset. The halt spends the whole quota pool: every
other entry that declares `conserve_usage` on the same harness account and
pool (the entries `describe` labels "shares <pool> with") is pinned
OUT-OF-QUOTA with it, so a sibling is not dispatched into the same spent pool. The reset is `--resets-at` when given, else
the reset the pool reading reports (codex's "try again at" clause), else a
five-hour latch. The session Re-select rule tells the agent to run it before
re-selecting.

It prints one JSON object: `entry`, `kind`, `recorded`, and either `budget`
(the written verdict) or `reason`. Exit `0` also covers the two cases that
record nothing: an entry without `conserve_usage` (no verdict to move) and no
session key (nothing is pinned). An unknown entry id exits `2`.

`--caller session` (the default) is for an agent that drives the harness
itself: a Claude id runs on the Agent tool, codex and opencode ids run through
their CLI, and a transport entry (no agent loop) is not routable -- unless the
caller passes `--dispatchable transport`, which says it runs transports through
a runner of its own (the code-review lane runner does), so they stay in the
menu. `--caller process` is for a program that dispatches through the completion
seam, where every resolvable entry routes. The printed rule text differs to
match: the session rule is choose-and-announce plus "re-select on any
unexplained dispatch failure"; the process rule is "take the default; move on
only on a classified halt". `--self` marks the author's entry `[author]` and
adds the independence preference. `--json` emits the rendered entries, the
default, and the rule -- never a hidden id.

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
  `account_check` mode. `default_endpoint` (default: `openrouter`) is the
  default model declaration, used when a caller names no model; it may be a
  list of entry ids, and a caller that takes one endpoint reads the first. Point a script at OpenRouter today, a local vLLM or any
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
- **Shared model registry.** A model declaration names registry ENTRIES. The
  OpenRouter models ship as transport entries `or-qwen`, `or-gpt-mini` and
  `or-gemini-lite`. Under an endpoint, a `models:` alias map (plus `default` /
  `defaultCheap` selectors) is a per-entry override: it picks the model inside
  that entry, and is not itself an id a declaration names. The top-level
  aliases (`qwen`, `gpt-mini`, `gemini-lite`) are deprecated in favour of the
  `or-` entries and keep resolving until they are removed. One project
  override changes the model for every consumer at once.
- **Local server launch profiles.** The canonical `model-server.sh` script owns
  the measured NInfer argument sets for Qwen3.6 and Qwen3.8, plus a `qwen38l`
  llama.cpp profile for the same model as a comparable second backend. Claude
  calls it through `${CLAUDE_PLUGIN_ROOT}`; `qwen36-server`, `qwen38-server`,
  and `qwen38l-server` are thin PATH adapters for interactive shells. Use
  `qwen-switch start qwen36|qwen38|qwen38l` to replace the resident server and
  wait for its matching model id, or `qwen-switch status` to inspect it.

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

### Model declarations: `describe` and `run`

A consumer that names "which model(s) may do this" passes a declaration (a
list of registry ids; a bare string reads as one id) to one of two calls.
Both need bootstrap >= 0.129.0, whose `bootstrap_lib.model_declaration`
validates the list's shape (non-empty, no duplicates):

```python
from llm_scripting_kit import NoUsableRoutingTarget, RunRequest, describe, run

ranking = describe(["fable", "sol", "opus"], caller="session", self_ref="opus")
print(ranking.render())          # the menu, with Ranking.rule at the end
ranking.default.id               # first usable entry of the pace-ordered list

result = run(["sol", "opus"], RunRequest(system="...", prompt="..."), max_attempts=2)
result.status                    # "completed" | "failed" | "attempt-limit"
```

`describe(names, *, project_root=None, caller, self_ref=None,
requirements=None, capabilities=None, backend_factory=None, exclude=(),
reachability_cache=None, entries=None, dispatchable=())` returns a `Ranking`:
`rendered_entries` (`EntryState` records in pace order), `dispositions`
(every declared id, in memory only), `rule` (the choice and re-selection
text), `default`, `render()`, and `to_json()` (the last two name rendered entries only). `requirements` is matched
against `capabilities` (default: the shipped advertisement) by the resolved
backend's name; `backend_factory` resolves an id (default: the merged
registry, then `create_backend`); `reachability_cache` is read first and
receives every probe, so a caller-scoped dict probes each entry once. When
nothing usable remains it raises `NoUsableRoutingTarget`, whose
`dispositions` itemise every declared id; skipping is otherwise silent.
`dispatchable=("transport",)` keeps transport entries routable for a session
caller that runs them itself. A caller that dispatches transport entries only
passes `backend_factory=create_transport_backend`
(`llm_scripting_kit.completion`), which makes a harness entry unroutable
there. `default_declaration()` returns the default declaration, and
`is_model_alias(name)` tells a deprecated alias or raw slug apart from an entry id.

`run(names, request, *, project_root=None, requirements=None, exclude=(),
max_attempts=1, on_attempt=None, ...)` is for callers with no loop of their
own. It dispatches the default entry. A quota or credit halt records that
entry out of quota for the session (when it declares `conserve_usage`), and
any classified halt or launch failure excludes it before `describe` runs
again. A task error stays a failed attempt. `max_attempts` counts executions:
reaching it returns `"attempt-limit"` and never raises the floor. When
`request.workspace` names a git work tree, it is reset to its launch state
before another entry runs. A workspace that cannot be reset ends the run
instead of stacking a second model's work on the first one's partial edits.
`on_attempt` receives each `Attempt` with its pace reading.

`order_by_pace(items)` is the ordering rule on its own. `check_registry_entry(id,
merged)` reports a core id (`fable`, `opus`, `sonnet`, `haiku`) whose merged
entry is not a Claude harness.

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

### Front door

The optional front door exposes configured OpenAI-compatible transport entries
as one local `/v1/chat/completions` endpoint. Add a transport-only `routing:`
mapping with a `group`, optional `order`, `max_parallel`, and `effort_style`;
callers send the group as `model`. Lower orders fill first, then requests spill
to the next tier. Run it with `llm-scripting-kit frontdoor ...` or
`scripts/frontdoor.sh`; the launcher selects the plugin venv Python and supports
`--print-command`. Use one uvicorn worker because concurrency counts are held in
one process's memory. `--check` prints tiers and lists transport entries that
are not tagged.

## When not to use

If you just export `OPENROUTER_API_KEY` yourself and have a single consumer,
you do not need this plugin. It earns its keep when several plugins or scripts
share credentials and you want validation, source attribution, a common model
registry, and pluggable endpoints.
