# llm-kit

One plugin and one library, `llm_kit` (the `katsoft-llm-kit` package), for LLM access, durable agent
jobs and batch content pipelines. It consolidates `llm-scripting-kit`, `job-kit`
and `content-pipeline-kit` (migrated from those plugins; they have been
removed and llm-kit replaces them).

## Library

The library source and source tests live in
[katsoft-libs](https://github.com/kitaekatt/katsoft-libs), under
`packages/katsoft-llm-kit/src/llm_kit/`. This plugin's own venv installs the
released `katsoft-llm-kit` distribution (see `pyproject.toml`). The
sub-packages are `completion`, `adapters`, `dispatch`, `registry`, `schema`,
`storage`, `execution`, `jobs`, `pipeline` (`core`, `artifacts`, `runtime`,
`execution`, `analysis`), `frontdoor` and `scaffold`. The root initializers are
empty: import the sub-package you need.

## CLI

The console script is `llm-kit = llm_kit.cli:main`, with the groups `account`,
`completion`, `models`, `jobs`, `pipeline`, `frontdoor`, `local` and `consent`. Run it by
its version-free venv path, which works from any shell and directory:

```
"${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"/plugins-kit/llm-kit/.venv/bin/llm-kit
(Windows: "${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"/plugins-kit/llm-kit/.venv/Scripts/llm-kit.exe)
```

Exit codes are `0` for success, `1` for a runtime failure, `2` for invalid input
or configuration, `3` for a classified persistent halt, `4` for a request the
completion protocol refused and `5` when `models probe` could not determine
reachability. The `jobs` group has its own table (`llm-kit jobs --help`): `0`
every job accepted, `1` a job not accepted, `2` usage, `3` the runner itself
failed, `4` a job waiting on an interrupt.

## Consumer contract

`contract/public-surface.json` lists the public names of the pipeline products
(`llm_kit.pipeline.*`, `llm_kit.scaffold`, `llm_kit.cli.run`) and whether each is
public, deprecated or removed. A stdlib-only scanner reads it over a consumer's
source and reports the deprecated and removed names in use:

```
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${LLM_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/check_consumer_contract.py" <consumer source path>
```

It exits 1 when a removed name is used (a deprecated name too under `--strict`)
and 2 when the contract cannot be read.

## Cost classes and consent

Every declared model entry has a cost class, reported with its reason whether or not any
consent layer is in use. A protocol 2 completion request that does not opt in to consent reports
a `claude` or `codex` entry's class as `null` (its authentication was not probed). Classification fails closed: what cannot be shown to be `local` or
`subscription` is `metered`, which states that billing is paid or could not be verified, not
that a charge occurred.

| Class | Meaning |
|---|---|
| `local` | A transport declared `billing: {mode: unmetered, owner: self}`, a front door whose every tier is, or a harness entry other than claude and codex whose `endpoint` names one (a front door with no tiers is `metered`; `unmetered` without `owner: self` is `metered`, because the billing mode states cost, not who owns the hardware). |
| `subscription` | A `claude` or `codex` harness on a subscription session pool. |
| `metered` | Everything else: a metered transport, an undeclared entry, any other harness, or a harness whose authentication is metered or unknown. |

Harness authentication is probed, never assumed. Claude is metered when a billing-diverting
variable is set and non-empty (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_VERTEX_PROJECT_ID`);
otherwise `claude auth status` must report `authMethod` `claude.ai` and `apiProvider`
`firstParty`. Codex is metered when `CODEX_API_KEY` or `OPENAI_API_KEY` is set; otherwise
`codex doctor --json` (run in the call's working directory and environment) must report
provider `openai`, ChatGPT auth and no stored API key. A probe that cannot run, times out or
returns an unrecognized report is unknown, which counts as `metered`.

The library makes no model calls of its own; the caller's code makes them. Classification and
the consent layer are designed as follows. The consent layer is optional and applies only when a
caller opts in; a protocol 2 request without a `consent` field is never refused for its cost
class, and a caller may build its own policy instead. Opt in
with `--consent [--consent-scope NAME]` on `models describe`, with the protocol 2 request
field `"consent": true` (global policy) or `"consent": {"scope": "NAME"}`, or with
`llm_kit.registry.consent.policy(scope=None)`. An explicit caller-owned `--allow <classes>`
filter replaces the layer; `--allow` and `--consent` are not combinable.

When the layer is applied and there is no consent file, the policy is `local` on,
`subscription` on, `metered` off, with provenance `default`; reading creates nothing. The
file is `$XDG_CONFIG_HOME/llm-kit/consent.json` (`$HOME/.config/llm-kit/consent.json` when
`XDG_CONFIG_HOME` is unset or not absolute), or the file `LLM_KIT_CONSENT_FILE` names, which
replaces the location for reads and writes. When the neutral file does not exist, the legacy
file `~/.claude/plugins/data/plugins-kit/llm-kit/consent.json` is read; a
write always targets the neutral file (or the override) and carries the legacy values across,
after which the neutral file wins. The file is `{schema: 2, local, subscription, metered,
scopes, updated_at}`. The global classes are defaults; a scope overrides them in either
direction for the classes it states, and an absent scope key inherits.

```
llm-kit consent show [--scope NAME] [--json]                      # effective policy and where each value comes from
llm-kit consent set [--scope NAME] --local|--subscription|--metered on|off   # only the class given changes
```

With the layer applied, an entry whose class is not allowed gets the disposition
`switched-off`: it is shown (a `Switched off:` line in `render()`, a `switched_off` key in
`to_json()`, and in the `NoUsableRoutingTarget` floor), never hidden and never dispatched to
or probed. `llm-kit models catalog` and `models describe` print a structured `consent` note
that the layer is optional and how to opt in; each catalog row carries `cost` as
`{class, reason}`.

## Project model file

A project that picks models for several call sites keeps one tracked file, `.llm-kit/models.yaml`:
an ordered catalogue of rows (id, label, endpoint, exact model, optional narrowed effort menu) and named
selections of `{model, effort}`. A personal, untracked layer `.llm-kit/models.local.yaml` overrides
selections per key.

```
llm-kit models project show|validate|write|clear     # read, check, store (atomic, canonical) or clear an override
llm-kit models catalog [--probe-auth]                # the ordered catalogue as JSON: menus, modes, cost
llm-kit completion complete --request-file -         # {"protocol": 2, "selection": "<key>", "prompt": "..."}
```

Each effort menu is the measured menu of the exact model (`gpt-6-luna` stops at `max`, `gpt-6.1-sol` also
takes `ultra`). A protocol 2 call refuses before spawning anything (exit 4, `error.kind` on stderr) when the
project file, selection, effort, mode, path, contract or consent is wrong. Codex runs in package mode with no
`bootstrap_lib`. Details: `references/llm-access.md` and `references/completion-seam-contract.md`.

## Local model servers and the front door

`scripts/frontdoor.sh` runs `llm-kit frontdoor`; it calls the sibling `bin/llm-kit`
by path. A command anchors on the plugin directory:

```
"${LLM_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/frontdoor.sh" --print-command --check
```

The plugin ships no model-server launcher. A host script that starts a local
model server can ask `llm-kit local swapper guard-launch --caller-pid PID` before
it execs, and only that verb's exit `3` (an explicit refusal) should block the
launch; every other outcome is the caller's to treat as fail-open. A host script
that replaces a resident server can free its port with
`llm-kit local swapper terminate-listener --port PORT --accept-replace`.
`tests/llm-kit/test_local_launchers.py` runs the front-door script against a stub CLI.

## Documentation

`CLAUDE.md` is the agent guidance, organized by library package: the layer rules,
the stances and gotchas of each component, and the register of hardcoded
opinions. The long-form documents live in `references/`:

| Document | Covers |
|---|---|
| `references/llm-access.md` | The registry, credentials, the completion seam, model declarations, the `account`, `completion`, `models`, `frontdoor` and `local` groups, the front door and the swapper |
| `references/completion-seam-contract.md` | What one completion call guarantees: the capability advertisement, the response report, output contracts, skill context and the request protocol |
| `references/jobs.md` | The `jobs` group: the job file, selection, acceptance contracts, parallelism, backpressure, events and durable interrupts |
| `references/pipeline-library.md` | The `llm_kit.pipeline` packages: structured output, cost and budget, routing, the adaptive gate, deadlines, durable waits and provenance |

## Registrations

| Kind | Qualified name | Source |
|---|---|---|
| skill | `llm-kit:openrouter-account` | `skills/openrouter-account/` |
| skill | `llm-kit:content-pipeline-domain` | `skills/content-pipeline-domain/` |
| skill | `llm-kit:background-pipeline` | `skills/background-pipeline/` |
| skill | `llm-kit:execute-work-unit` | `skills/execute-work-unit/` |
| skill | `llm-kit:workflow-pipeline` | `skills/workflow-pipeline/` |
| agent | `llm-kit:pipeline-worker` | `agents/pipeline-worker.md` |
| workflow | `llm-kit:run-ready-wave` | `workflows/run-ready-wave.js` |

A command in a document under `references/` or this README that needs the
plugin directory anchors on `"${LLM_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}"`.
A skill body uses the harness variables for the plugin and skill directories.
A Python script runs under `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"`.

`tests/llm-kit/test_registrations.py` checks the names, the absence of legacy
package, CLI and plugin-root references, and the interpreter rule.

## Environment

- Python 3.12 or newer.
- The base library path is standard-library only. Extras: `config` (pyyaml),
  `sdk` (openai), `local` (psutil), `frontdoor` (fastapi, uvicorn, httpx),
  `jobs` and `pipeline` (pyyaml).
- The user-scoped files live under `$XDG_CONFIG_HOME/llm-kit/` (`$HOME/.config/llm-kit/` as the fallback):
  `consent.json`, `model-endpoints.yaml` and the key file `.env`, with the overrides `LLM_KIT_CONSENT_FILE`,
  `MODEL_ENDPOINTS_REGISTRY` and `LLM_KIT_ENV_FILE`. The files an earlier release kept under `~/.claude` are read
  when the neutral file is absent and are never written. The layered `config.yaml` files and the usage-verdict
  cache keep their historical locations (`CLAUDE.md`, "Persisted state keeps its historical names").
  llm-kit seeds no configuration of its own.
