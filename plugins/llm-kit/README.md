# llm-kit

One plugin and one shared library, `llm_kit`, for LLM access, durable agent
jobs and batch content pipelines. It consolidates `llm-scripting-kit`, `job-kit`
and `content-pipeline-kit` (migrated from those plugins; they stay installed,
published and unchanged, and llm-kit never imports them).

## Library

`lib/llm_kit/` holds the library source, plus `llm_kit/bootstrap_guard.py`; the plugin's
own venv installs it as the released `katsoft-llm-kit` distribution rather than
running this copy (see `pyproject.toml`). The
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

Every declared model entry has a cost class, and a caller that opts in with `allow=` is
limited to the classes it names. Classification fails closed: what cannot be shown to be
`free` or `usage` is `spend`.

| Class | Meaning |
|---|---|
| `free` | A transport with `billing.mode` `unmetered`, or a front door whose every tier is unmetered (a front door with no tiers is `spend`). |
| `usage` | A `claude` or `codex` harness on a subscription session pool. |
| `spend` | Everything else: a metered transport, an undeclared entry, any other harness, or a harness whose authentication is metered or unknown. |

Harness authentication is probed, never assumed. Claude is metered when a billing-diverting
variable is set and non-empty (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`, `AWS_BEARER_TOKEN_BEDROCK`, `ANTHROPIC_VERTEX_PROJECT_ID`);
otherwise `claude auth status` must report `authMethod` `claude.ai` and `apiProvider`
`firstParty`. Codex is metered when `CODEX_API_KEY` or `OPENAI_API_KEY` is set; otherwise
`codex doctor --json` (run in the call's working directory and environment) must report
provider `openai`, ChatGPT auth and no stored API key. A probe that cannot run, times out or
returns an unrecognized report is unknown, which counts as `spend`.

Consent is one file per machine, `~/.claude/plugins/data/plugins-kit/llm-kit/consent.json`.
`free` is always allowed; `usage` and `spend` are off until the user turns them on. An absent
or malformed file means free-only.

```
llm-kit consent show [--json]                       # usage, spend, source, path, note
llm-kit consent set [--usage on|off] [--spend on|off]   # only the flags given change; none given exits 2
```

Opt-in is per caller. `describe(..., allow=...)` and `run(..., allow=...)` take class names,
and a caller passes what the user consented to:

```python
from llm_kit.registry.consent import load_consent

consent = load_consent()
if consent.note:
    print(consent.note)        # the caller prints it; the library never does
result = run(names, request, allow=consent.allowed)
```

Without `allow`, nothing is classified or probed and behavior is unchanged. With it, an entry
whose class is not allowed gets the disposition `switched-off`: it is shown (a `Switched off:`
line in `render()`, a `switched_off` key in `to_json()`, and in the `NoUsableRoutingTarget`
floor), never hidden and never dispatched to or probed. `llm-kit models describe <ids> --allow
free,usage` applies the same filter from the command line.

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
- Credentials, configuration and routing use the legacy names, precedence and
  locations (for example the user-scoped `.env` under
  `plugins-kit/llm-scripting-kit/.env`). llm-kit seeds no configuration of its own.
