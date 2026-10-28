# CLAUDE.md -- llm-kit plugin

Guidance for an AI agent working in this plugin. llm-kit is one plugin with one
library, `llm_kit`, that answers three families of question:

- **LLM access** -- which endpoint, which model, which key, which transport, how
  a configured local model server starts, and how one call is made correctly
  (`llm_kit.registry`, `llm_kit.completion`, `llm_kit.adapters`,
  `llm_kit.dispatch`, `llm_kit.frontdoor`).
- **Durable agent jobs** -- a flat set of jobs run through a bounded worker pool
  against a ledger (`llm_kit.jobs`).
- **Batch content pipelines** -- the questions a RUN of many LLM calls raises:
  which units need doing, which are stale, which are done and must be left
  alone, whether a result is valid, what the run cost, where the output goes
  (`llm_kit.pipeline.*`).

`README.md` is the short orientation. The long-form user documents are
`references/llm-access.md`, `references/jobs.md` and `references/pipeline-library.md`;
the per-call completion contract is `references/completion-seam-contract.md`.
`skills/openrouter-account/` owns key management, and
`skills/content-pipeline-domain/SKILL.md` owns the pipeline vocabulary and
abstraction map (`skills/content-pipeline-domain/references/design-discipline.md`
beside it carries the philosophy behind the opt-in guardrails). The signed
design that fixes the layout below is `docs/reference/llm-kit-architecture.md`.

## Layers, imports and release

`llm_kit` has 16 import components. The root `llm_kit` and the grouping package
`llm_kit.pipeline` hold no behavior and re-export nothing: import the component
you need, and take a public symbol from the component that owns it.

```
Layer 6  cli
Layer 5  pipeline.execution
Layer 4  jobs, pipeline.runtime
Layer 3  dispatch, frontdoor
Layer 2  adapters, pipeline.analysis
Layer 1  completion, execution, pipeline.artifacts
Layer 0  schema, registry, storage, scaffold, pipeline.core
```

An import may point only to a lower layer, and only along an edge the
architecture lists; an omitted edge is forbidden. The complete edge set is the
"Exact dependency graph" in `docs/reference/llm-kit-architecture.md`, and
`tests/llm-kit/test_import_graph.py` enforces it over eager, local,
`TYPE_CHECKING` and literal dynamic imports. Three consequences to hold when you
add code:

- `jobs` and `pipeline.execution` never import each other. A flat job runner owns
  a call; a pipeline worker claims a unit under a lease and a fencing token.
  Neither receives the other's policy. What they share lives below them, in
  `execution` (bootstrap event and interrupt adapters, the schema-validator
  loader) and `storage` (SQLite posture, atomic JSON).
- `completion` holds only provider-neutral contracts and the `LLMBackend`,
  `BackendFactory` and `CapabilityProvider` protocols. Filled capability tables,
  concrete backends and factories live in `adapters` and `dispatch`. A
  re-export from a `completion` initializer that restored an `adapters` or
  `dispatch` edge would break the layering.
- `cli` is the composition root. It resolves credentials through `registry`,
  injects the key into `adapters.account`, and injects one
  `llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory` into jobs and
  the pipeline runtime. No other component builds a concrete adapter from argv.

The library is one release unit: every library component ships at one version.
The plugin has its own independent version and consumes the released package.
The library is stdlib-only on its base
path; third-party packages arrive as extras (`config` and `jobs` and `pipeline`
need PyYAML, `sdk` the OpenAI SDK, `local` psutil, `frontdoor` FastAPI, Uvicorn
and httpx) and are imported at the call site, so a missing requested capability
refuses at the call rather than at import. The library source and source tests
belong to the [katsoft-libs source tree](https://github.com/kitaekatt/katsoft-libs/tree/dev/packages/katsoft-llm-kit/src/llm_kit).
Change library behavior there. This plugin holds no library source and publishes no shared lib. A consumer
venv installs the package with the extras it reaches (`katsoft-llm-kit[<extras>]>=A.B,<N` in
its `pyproject.toml`). The plugin's own venv does the same: its `pyproject.toml` is a
non-package project that requires the released `katsoft-llm-kit[<every extra>]>=1.7,<2` and
`katsoft-contracts>=1.0,<2`, so the extras flow from that distribution and `bootstrap.json`
names none. The floor is 1.7 because the per-model output-cap surface first ships in 1.7.0; the job supervisor first shipped in
1.6.0. Codex in package mode, the project model file, `models catalog`, protocol 2 of
`completion complete`, local selection overrides and the neutral user paths shipped in 1.3.0;
`custom_bootstrap.py` reads `llm-kit account status --json`, which first shipped in 1.2.0;
bootstrap holds the venv at the highest imported released target the requirement
admits. Targets come from the marketplace's `data/package-targets.json`, imported
from a verified katsoft-libs ledger commit. A library change reaches this venv
after its PyPI release and the target artifact's marketplace publication.

`bootstrap_lib` is a shared library with its own owner. The library reaches it
lazily (skill material, claudx propagation, the code-review lane). Codex command
construction is not one of those edges: `llm_kit.adapters.codex_command` owns it and
imports nothing outside the standard library. A consumer venv that installs `llm_kit` but
not `bootstrap_lib` imports `llm_kit.completion.skill_context` cleanly and fails on
the first skill-context call, because the package does not carry the shared-lib edge to a consumer:
each consumer declares what it needs (`plugins/CLAUDE.md`, "Why shared libs rather than
published packages"). `shared_lib_imports` in `bootstrap.json` declares the edge for
llm-kit's own venv.

## Runtime mode: source tree or installed package

The same `llm_kit` source runs in two deployments, told apart by
`llm_kit/_deployment.py`: the source tree in katsoft-libs (`MODE = "source"`,
tested there), and the `katsoft-llm-kit` wheel (`MODE = "package"`, written at
build time, depending on `katsoft-contracts`). This repository's maintainer
environment, the plugin's venv and consumer venvs install the released wheel.
`bootstrap_lib` reaches
the plugin's venv through `shared_lib_imports`, which is what `fleet()` tests.
`llm_kit._runtime.mode()` decides once per process and raises
`RuntimeIdentityError` or `UnsupportedLayout` for an environment it cannot
identify (both copies present, a duplicate or editable install, a zip or frozen
layout). Never import `bootstrap_lib` directly for a new feature without
choosing a side:

- The three shared contracts (`execution_event`, `interrupt_contract`,
  `model_declaration`) are routed: in package mode `_runtime.load(name)` imports
  them from `katsoft_contracts` and the legacy probes are skipped; in legacy
  mode the existing probes and their diagnostics run unchanged. Add no fourth
  routed name without extending `ROUTED_CONTRACTS` and its test.
- Any other `bootstrap_lib` use (claudx, skill material, the code-review
  lane) calls `_runtime.require_fleet("<feature>")` before the import. It
  raises `UnsupportedInPackageMode` in package mode only when `_runtime.fleet()`
  is False, that is, when `bootstrap_lib` does not import (cached per process).
  `fleet()` tests availability only; whether the linked `bootstrap_lib` is new
  enough stays in each feature's own probe (skill context), never in a global
  gate. Codex needs no `bootstrap_lib` and has no gate: the backend, the harness
  adapter and reachability import `llm_kit.adapters.codex_command` directly.
- `cli.main()` re-execs into the plugin venv only in legacy mode.
- Package-mode model config without the fleet is the one policy point
  `registry.models.package_mode_model_config`; with the fleet, package mode
  uses the layered registry like legacy mode. Codex reachability uses
  `llm_kit.adapters.codex_command.detect_codex` in both.
- `_runtime` is a support module outside the 16 components: any component may
  import it, it imports only `_deployment`, and only `_runtime` imports
  `_deployment`. `tests/llm-kit/test_import_graph.py` enforces this. Import it
  inside a function where a module is compared with a legacy twin, so the
  module surface stays unchanged.

## Persisted state keeps its historical names

The credential, configuration and routing contracts, and the persisted formats,
keep the spellings they had before the plugin was consolidated. A name that looks
like a leftover is a deliberate compatibility choice
(`docs/reference/llm-kit-architecture.md`, "Persisted-state compatibility"):

- **Credentials** resolve in this order: the endpoint's `key_env` variable, the project
  `.env` at `<project>/.llm-kit/.env`, the namespaced project `.env` at
  `.local-data/plugins-kit/llm-scripting-kit/.env` (and the superseded
  `.local-data/llm-scripting-kit/.env`), the user key file, then the endpoint's
  `key_file`. The user key file is `$LLM_KIT_ENV_FILE` when set (that file alone),
  else `$XDG_CONFIG_HOME/llm-kit/.env` (`$HOME/.config/llm-kit/.env` when the
  variable is unset or not absolute), else, only when that neutral file does not
  exist, the legacy `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env`,
  which is read and never written. `account set-key` writes the override or the
  neutral file and starts a first neutral file from the legacy file's keys, so the
  neutral file that then shadows the legacy one drops none.
- **The model-endpoints registry and consent** follow the same shape. The registry is
  `$MODEL_ENDPOINTS_REGISTRY` when set (an explicit path; a missing file is an error),
  else `$XDG_CONFIG_HOME/llm-kit/model-endpoints.yaml`, else, only when the neutral
  file does not exist, the read-only legacy `~/.claude/config/model-endpoints.yaml`.
  The optional consent layer's file is `$LLM_KIT_CONSENT_FILE` when set, else
  `$XDG_CONFIG_HOME/llm-kit/consent.json`, else the read-only legacy
  `~/.claude/plugins/data/plugins-kit/llm-kit/consent.json`; `consent set` writes the
  override or the neutral file, carrying the legacy values across on the first write.
  `llm_kit.registry.user_paths` is the one place the neutral directory is spelled, and
  it names no harness. The library creates nothing on a read.
- **Configuration layers** keep the historical files: the fleet file
  `~/.claude/config/llm-scripting-kit.yaml`, the user
  `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/config.yaml`, and the
  project `config.yaml`. llm-kit seeds no competing layer of its own, and its
  `custom_bootstrap.py` migrates no legacy file. The one exception is
  `llm_kit.registry.constants.CLI_COMMAND`, which names the llm-kit venv and executable.
- **Environment variables** keep their spelling (`LLM_SCRIPTING_KIT_USAGE_SESSION`, `CONTENT_PIPELINE_LLM_MODELS`,
  `CONTENT_PIPELINE_SPEND_LEDGER`, `JOB_KIT_BACKPRESSURE_CAP_S`, the `JOB_KIT_*`
  contract variables).
- **The usage-verdict cache** is `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/usage-verdicts.json`,
  an advisory single-session pacing file written whole with an atomic replace and
  no interprocess lock. Simultaneous writers are last-writer-wins. A stale writer
  can restore an `available` verdict over an observed `out-of-quota` one, and a
  pinned read returns it without recomputing. The file is pacing state, not
  authorization, accounting or reservation; do not add locking or merge logic
  without a separate decision.
- **Schemas and event bytes** are frozen: job ledger schema 13 (schema 12 plus
  the `cancelled` state, the one recorded exception: user ruling 2026-10-19, "(8)
  llm-kit (unpublished) may change its frozen formats for a per-job timeout and
  `jobs cancel`"; `docs/reference/llm-kit-architecture.md`, "Job ledger schema
  13"), pipeline execution schema 10, spend-ledger schema 1, canonical JSON, interrupt
  documents, DDL, pragma order, and event bytes. Event owners and envelope
  literals keep their historical identity (`job-kit:` event types such as
  `job-kit:run-created`, `job-kit.interrupt-request/v1`, the `content-pipeline-kit`
  event-extension owner, the `llm-scripting-kit.json-schema-subset/v1` validator
  subset marker). Changing any of them needs a separate dual-readable format,
  conversion, ownership and rollback decision. Schema 13 is deliberately NOT
  readable by a ledger reader that predates it (the retired job runner, or an
  llm-kit older than `cancel`): the future-schema check refuses it, so a job
  ledger llm-kit has opened cannot be handed back.
- **Ledger ownership**: the job ledger keeps its unsupported, undetected
  one-process-per-ledger rule, with no process-lifetime lock and no refusal
  signal; `jobs cancel` requires the run process to be stopped already and adds
  no lease to check it. The pipeline execution store keeps transactional unit claims with
  expiring leases and monotonically increasing fencing tokens, and at most one
  dispatcher lease holder per run. Neither stance changes without a design
  decision.
- A removed or renamed public `llm_kit` name follows `docs/reference/deprecation-policy.md`.

## `llm_kit.schema`: JSON subset and model-free repair

`llm_kit.schema.json_schema` is a stdlib JSON Schema subset, not a dependency.
`SUBSET_V1` is a frozen revision (its keyword sets, its local-`$ref` rule, its
cycle refusal and the shape and order of its error tuples never change);
`SUPPORTED_SUBSETS` is the marker a consumer probes for and only grows. The full
keyword list and the refusal rule are in `references/completion-seam-contract.md`.

`llm-kit completion repair-json` is the model-free CLI over
`llm_kit.schema.json_repair.repair_json_structure`. It reads one absolute-path
schema file, checks it with `check_schema`, reads UTF-8 stdin to EOF and prints
one protocol-1 result object. It never resolves an endpoint, reads a registry or
calls a network. The CLI owns only argument, stream, exit-code and serialization
handling; repair decisions and schema support stay in `llm_kit.schema.json_repair` and
`llm_kit.schema.json_schema`. Never loosen the one-fit rule: a repaired document is applied
only when exactly one fits the schema.

## `llm_kit.registry`: declarations, credentials, configuration

Registry code makes no HTTP call. It owns the endpoint and model declarations,
the effort vocabulary, conserve metadata, credential paths and precedence, and
configuration resolution.

**Effort on a transport entry is conditional, and one module owns its
vocabulary.** `llm_kit.registry.effort` defines the styles (`top-level`,
`ninfer`, `chat_template_kwargs`, `unsupported`), the ninfer `high` -> `xhigh`
remap, and `plan_effort`, the precedence a direct call follows: an effort the
caller put in `extras` wins verbatim (top-level over nested when both are
present; an explicit null means "send none"), then `BackendOptions.effort` is
placed in the endpoint's style, else it is dropped. The front door's
`_normalize_body` uses the same `extract_effort` / `place_effort`, so the two
paths cannot disagree about a style. Style resolution for a DIRECT call is
`llm_kit.registry.model_endpoints.resolve_effort_style`: entry `effort_style` >
`frontdoor: true` (top-level) > a DECLARED `routing.effort_style` > none; a
declared-invalid style resolves to none rather than falling through. The front
door's own choice is `deployment_effort_style`, where an omitted routing style
still means top-level. Registry loading rejects a transport entry that declares
`reasoning_effort` without a deliverable `effort_style`; invalid styles and
conflicting declared styles remain registry notes.

**`effort_menu` and `lower_effort` are how a caller retries a length stop.**
`llm_kit.registry.effort.effort_menu(endpoint)` returns the efforts a registry
endpoint accepts, low to high, from its resolved style (`style_effort_menu` is
the same lookup by style): `ninfer` gives `("none", "low", "medium", "xhigh")`
and rejects `high`, `top-level` and `chat_template_kwargs` give
`("low", "medium", "high")`, and an endpoint that delivers no effort gives `()`.
`lower_effort(endpoint, effort)` returns the next value below `effort` (remapping
`high` to `xhigh` on ninfer first) and `None` at the bottom, for a value outside
the menu, or for an empty menu. Harness entries resolve through
`harness_effort_order(harness)`: codex and claude give the harness-wide menu
`("low", "medium", "high", "xhigh", "max")`, and `opencode-cli` gives `()` because
its effort values are provider-defined. That harness-wide menu is a fallback, not the
menu of a model: it serves `lower_effort` and validates a model the library has not
measured. The retry rule: on a length stop
(`EmptyCompletionError`, reasoning ate the budget) retry once per level at
`lower_effort`; stop at `None`. An unknown endpoint raises `EndpointRegistryError`.

**An effort menu belongs to one exact model over one route.**
`llm_kit.registry.model_profiles` is the library's table of measured menus, keyed on
(route, exact model id) for the routes `codex`, `claude` and `openrouter`. The data is
measured at build time and shipped; each profile records its source (the Codex CLI's
model catalogue, the Claude Code model table, the OpenRouter `/models` listing) and the
day it was taken, and nothing at runtime reads `~/.codex`, `~/.claude` or the network, so
an install with no AI harness sees the same menus. `gpt-6-luna` stops at `max`,
`gpt-6.1-sol` also takes `ultra`, no Codex or Claude CLI model takes `none`, and an
OpenRouter model takes `none` only where its vendor lists it. `check_effort` is the one
validator: `CodexCliBackend`, `CodexAdapter` and `ClaudeCliBackend` call it, so no
backend path bypasses a menu; the project model file shares the menu table
(`exact_effort_menu`, `library_effort_menu_with_source`) but does not call the validator. A model with no profile has no
exact menu and the library invents none: it falls back to the harness-wide order, and a
project row for it declares no `efforts` and selects a null effort. A new model enters by
adding a measured profile (with its source and date), never by widening a harness table.
A profile does not make an endpoint deliver an effort: the endpoint still needs a
resolved `effort_style`.

**The project model file is a tracked catalogue plus named selections.**
`llm_kit.registry.project_models` owns `.llm-kit/models.yaml` (`version: 1`, an ordered
`catalog` of rows `{id, label, endpoint, model, efforts, request_defaults}`, and
`selections` mapping consumer-chosen keys to `{model, effort}`). It holds no key, key file,
base URL, consent or vendor capability claim: endpoints, credentials and billing resolve
through the endpoint registry, and a row may narrow the library menu and never widen it.
The path is confined to the project root (a symlink or an escaping path is refused).
`llm-kit models project show|validate|write` read, check and atomically store the file in
canonical form (`show` prints the sha256 that `--if-match` compares); `validate` and
`write` take the JSON document on stdin. A second, personal layer `<file-stem>.local.yaml`
(default `.llm-kit/models.local.yaml`, `{version: 1, selections: {...}}`) overrides the
tracked selections per key: `models project write --layer local` replaces it, or with
`--key K --model M [--effort E]` sets one override, and `models project clear --key K`
removes one (the file is deleted when none remains). An override equal to the tracked
default is dropped, and the library never edits `.gitignore`. `models catalog` prints the
ordered catalogue as JSON without dispatching: each row's exact-model menu and the source
of that menu (`effort_source`), whether the endpoint can deliver an effort, its `modes`
(the adapter's call modes), its cost and the `consent` state, and `resolved_selections`, which reports per key
`{effective, default, source}` (`source` is `default` or `override`); a caller reads
`resolved_selections`, not `selections`, for what a call uses. `catalog --probe-auth` asks
the `claude` and `codex` CLIs how they are authenticated so a harness row reports a cost
class; without it that class is null.

**Marker and billing are explicit metadata, never inferred.** `frontdoor: true`
(strict boolean) on a transport entry says only that its `base_url` is a front
door and its `model` names the group; it is not derived from `routing`, ids,
hosts, ports or `/v1/models`. An invalid value is noted, treated as false, and
the entry (even the default) is kept. `billing.mode` (`unmetered` or
`provider-reported`) is separate and entry-local: `unmetered` is an explicit USD
zero, `provider-reported` declares native `usage.cost` is USD, omission declares
neither; `billing.owner: self` (optional) adds the ownership claim that `local`
needs. `key_env` stays independent of all of them. The keys surface in the `models endpoints` JSON (`frontdoor`,
and `billing` with `owner` when set; a harness entry's `endpoint`) and in
`llm_kit.registry.models.resolve_endpoint()` (`frontdoor`, `billing_mode`).

**A usage pool is DECLARED or defaulted, never derived from the model.** An entry
opts in with `conserve_usage`; `llm_kit.registry.conserve` parses it and carries
the budget values (`Budget`, `ConserveSpec`, the `POOL_*` and `STATUS_*`
constants). fable draws on a per-model weekly bucket (`model_scoped`, selected by
`display_name`); opus draws on the all-model weekly window (`seven_day`). A bare
`conserve_usage: true` takes `seven_day`, which the codex reader resolves to
codex's own `primary` -- a harness remap, not a per-model one. The derivation
that is refused is "use the model's own bucket when one exists": it would hand
opus `seven_day_opus`, the opposite of the all-model pacing an opus entry means.

## `llm_kit.storage`: SQLite and atomic-JSON mechanics

`llm_kit.storage` holds the connection and transaction posture, WAL and network
handling, atomic JSON, and schema-version primitives. Product DDL, migration loops
and ownership policy stay in `jobs`, `pipeline.execution` and the spend ledger.
`llm_kit.storage.network.looks_like_network_path` is the one implementation of the
network-path heuristic; `llm_kit.pipeline.execution.store` and
`llm_kit.pipeline.runtime.spend_ledger` keep a module-level name that delegates to
it, because callers and tests replace the name there to force or suppress the
warning. A change to the heuristic is one edit in `storage.network`. The stdlib-only
atomic-JSON writer creates its temporary file in the target's directory.

## `llm_kit.scaffold` and the pipeline command surface

`llm_kit.scaffold.dispatch` is a dispatch helper a consumer wires its own commands
onto (subcommand registry, scope filtering, did-you-mean, YAML output, stable exit
codes). `llm_kit.cli.run.build_commands` returns the execution-ledger command
adapters for a consumer's entry point; `llm-kit pipeline` mounts only the
store-only subset (`create-run`, `register-units`, `claim`, `renew`, `accept`,
`fail`, `halt`, `clear-halt`, `status`) on a ledger named by `--store`. The
`protocol` command needs a consumer's run adapter and handlers, so a consumer mount
calls `build_commands(store, adapter=...)` itself. The orchestration loop, config
loading, prompt content, field names, the pricing table and persistence belong to
the consumer. Say that before describing any pipeline subpackage: a consumer that
expects a runnable pipeline tool has already misunderstood the layer.

## `llm_kit.completion`: one call, made correctly

This layer owns everything a SINGLE completion needs -- the request, result,
usage, halt, capability, requirement and output-contract types, the prompt-cache
message shaping, and a shared halt taxonomy (`classify_halt_text`, `HaltError` in
`llm_kit.completion.halt`) so a persistent failure classifies identically
whichever transport produced it. Four transports implement the one `complete()`
(in `llm_kit.adapters`): `OpenRouterBackend` over HTTP, `ClaudeCliBackend` driving
the local `claude -p` CLI, `CodexCliBackend` driving `codex exec`, and
`OpencodeCliBackend` driving `opencode run`.

**The layer classifies; it does not halt.** Every backend exposes
`classify_halt(exc)` and the transports raise ordinary errors carrying classifiable
text -- nothing here raises `HaltError` itself. Converting a classification into a
stop is the caller's, because only the caller knows whether it is mid-sweep with
hundreds of items left or running a one-shot script that should simply die.
Exporting the type instead of raising it is what lets a new consumer inherit the
taxonomy rather than invent one.

**Backpressure is a halt that clears itself, and the rule for what counts is
narrow.** `HALT_BACKPRESSURE` (`"backpressure"`) marks transient endpoint overload:
retry after a wait, do not stop the run. `classify_backpressure(exc)` returns
`Backpressure(status, retry_after_s)` or `None`, and `classify_openai_exception`
checks it FIRST, so `OpenRouterBackend.classify_halt` reports it. A 503 counts only
with queue/overload/busy/timeout wording or a server code such as
`request_queue_timeout`; a bare 503 is an outage, not a halt. A 429 counts only
with a positive signal (a `Retry-After` header, a "try again in N s" hint,
queue/overload wording, or a code such as `rate_limit_exceeded`); a bare 429 keeps
`rate_limit`. Quota or credit wording (`insufficient_quota`, billing, credit,
usage limit) is NEVER backpressure and keeps `rate_limit` or `insufficient_credit`.
`retry_after_s` comes from a numeric `Retry-After` header, else a body hint
(seconds or ms); an HTTP-date header yields `None`. `halt_payload(kind, exc)`
builds the envelope object (`{"kind": "backpressure", "retry_after_s": 3.0}`, other
kinds `{"kind": kind}`), and `HaltError` carries `retry_after_s`. A single-call
`complete` halt adds the same `halt` object with `error.code` and exit `3`
unchanged; there is no separate backpressure exit.

**The seam is uniform; the transports are not.** You write the prompt pair once and
choose the executor separately, but the separation is incomplete, and a caller who
believes it is clean will get burned:

- **Codex contributes a harness, not transport, and a harness is warranted only
  when what the unit needs is not knowable when the prompt is written.** A fully
  supplied transformation is a completions call and stays one. The overhead figure,
  the full rule, endpoint compatibility (wire, tool schema, keyless auth) and the
  dispatch traps are owned by the orchestrate skill's `references/codex-dispatch.md`
  in awesome-kit; read it there rather than relying on a figure restated here.
- **The `model` id is not portable.** An OpenRouter slug means nothing to
  `claude -p`, and codex requires fully-qualified ids. Nothing here translates them,
  so choosing a backend is choosing a backend AND a model id. The pipeline runtime's
  `llm_kit.pipeline.runtime.backends.routed_model` is the in-library compensation,
  and its existence is the evidence: a genuinely uniform seam would not need it.
- **`BackendOptions` is a union, not a neutral description of the work.**
  `user_cache_prefix` is OpenRouter-only; `allowed_tools`, `disallowed_tools` and
  `system_prompt_mode` are claude-cli-only; `effort` reaches claude-cli, codex and
  opencode, and reaches OpenRouter only on a transport entry that resolves an
  `effort_style`; `cwd` reaches the three CLI backends and not OpenRouter, which is
  why `cwd` is not a core param of this seam. `temperature` and `max_tokens` (default unset;
  cap rule: `references/completion-seam-contract.md`, "Output cap") are
  accepted and then dropped by all CLI backends -- dropped and REPORTED, not
  ignored. `skill_context` is read by all four and delivered by OpenRouter alone,
  as the leading block of its system message; the three CLI backends refuse it
  before dispatch, because a harness loads skills itself and the seam cannot see
  what it loaded.
- **The same call does not behave the same way.** Retry, timeout defaults, token
  accounting (codex reports one undifferentiated `total_tokens` and no input/output
  split at all; opencode's default output reports no usage), cost (flat zero for the
  subscription CLIs, unavailable from opencode's default output, real money for
  OpenRouter) and prompt delivery all differ -- the CLI transports have one stdin
  prompt rather than a separate system channel.

So: uniform CALL SHAPE and uniform FAILURE VOCABULARY, not interchangeable
behaviour. Say so when documenting this layer rather than letting "four transports
behind one `complete()`" imply more than it delivers.

**The advertisement is what the adapter EMITS.** Every backend class carries a
`capabilities: ClassVar[Capabilities]` (the filled tables are
`llm_kit.adapters.capabilities`, the types are `llm_kit.completion.capabilities`),
naming the params it honors, the ones it drops, the constraints it emits, its
structured-output mode and how system text reaches the model. Read it through
`llm_kit.adapters.capabilities.adapter_capabilities()` or from `llm-kit models
endpoints`, whose payload carries a `capabilities` block keyed by adapter family
plus an `adapter` field on each endpoint. A capability describes what the adapter
EMITS, never what the provider or CLI does with it: nothing here promises a target
HONORS a control. Two corollaries, each pinned by tests: **suppressing a flag is
not a control** (codex emits `network_access=false` only under `-s workspace-write`, the one mode
that reads the key, as the `network-disable` control; other sandbox modes emit nothing for
`network=False`), and **a value menu is
advertised only where the request-building code validates it**.

**A guarantees requirement admits AND arms; a guarantee needs a live check.**
`llm_kit.dispatch.declaration.run` passes every call through
`llm_kit.completion.requirements.arm_call`, which arms each request-sourced control
the requirement relies on (claude-cli and codex-cli text-only mode through the
backend field `text_only`, opencode-cli through `disallowed_tools`) and raises when
one cannot be armed; `run()` then refuses a response that does not report the armed
control. A selector that admits an entry it does not arm is a fake gate. Add a
canonical subject to a mode only after a live call shows no tool for it remains.
For codex the model CATALOG, not the config, supplies code mode, multi-agent,
apply_patch and the node REPL, so text-only mode patches the model's `codex debug
models` entry and ignores the user config (`-c mcp_servers={}` merges and removes
nothing); re-measure when codex adds a tool source. `references/llm-access.md`,
"Text-only mode", has the argv and the evidence.

**A protocol error is not an endpoint error.** `llm-kit completion complete`
speaks a versioned protocol both ways; `EXIT_PROTOCOL` (4) means nothing ran and
retrying the same bytes cannot help, distinct from `EXIT_FAILURE` (1), a call that
ran and may succeed on retry. The full per-call contract -- the advertisement's
shape, the truthful response record, the per-key `extras` verdicts, the
tool-denial and system-prompt-mode asymmetries, and the request/result protocol
with its exit codes -- is `references/completion-seam-contract.md`. Read it before
changing an adapter, a capability record or the `complete` verb.

**Skill context is a REQUIRED edge into `bootstrap_lib.skill_material`, with a
floor and a refusal at the call.** `llm_kit.completion.skill_context` imports the
module lazily, so importing the completion package never needs it (the records live
in the stdlib-only leaf `llm_kit.completion.skill_context_types`).
`requires_bootstrap` in `bootstrap.json` is at least
`llm_kit.completion.skill_context.SKILL_MATERIAL_BOOTSTRAP`, the version that
ships the calls the seam makes (`tests/llm-kit/test_manifest.py` pins the
relation). A floor gates provisioning, not the code that is already active, so
the module still probes every call it makes and `materialize_skill_context` raises
`SkillContextSupportError` in three states, each with its own message: `absent`
(`bootstrap_lib` not importable; install bootstrap), `too-old` (the linked copy
lacks a call, or a report shape is unknown; update bootstrap, a copy left by an
uninstall reads the same) and `no-pyyaml` (the interpreter has no PyYAML). No state
falls back to another reader, because a request that names skills and is sent
without them would be read as answered with them.

**The seam is RUN-ONCE by default: one request, at most one invocation.**
`retry_max_attempts` defaults to 1, so the claude retry is opt-in and
`LLMResponse.attempts` above 1 is evidence of a caller's own policy rather than of
hidden adapter behaviour. A transient envelope that survives the budget RAISES, in
the canonical `"api_error_status":NNN` form the halt matchers read; it is never
reported as a completed call with empty text.

**The seam does not own the concerns of a RUN OF MANY CALLS.** Response caching,
cost accounting, budget guarding, batching, concurrency and rate limiting belong to
the caller (the pipeline runtime implements them for pipelines). Those are policy,
not transport: what a cache is keyed on, what a budget is measured against and how
many calls may run at once are questions the calling pipeline can answer and a
transport cannot. Output validity is split the same way. The seam enforces a
contract the CALLER declares: the caller answers what a valid output looks like by
declaring an `OutputContract` (a JSON Schema and a policy), and the transport only
checks the returned object against that declaration; it never invents a schema.
What to do with a structurally valid object -- semantic or domain validation, retry,
caching -- stays with the caller. A declared contract is refused before dispatch
until an adapter advertises a policy for it. Holding that altitude is also what
keeps the completion path stdlib-only apart from a lazy `openai` import, so a
consumer driving only `claude-cli` installs no SDK.

**Two behaviours look like exceptions to that split and are not.**
`ClaudeCliBackend` CAN retry a transient 5xx envelope (`retry_max_attempts`,
`retry_cooldown_s`) and enforces a per-call timeout -- both are properties of the
subprocess it spawns, not run-level policy, and 429 / 401 never retry because they
persist. `OpenRouterBackend` makes exactly one attempt and leaves retry to the
caller, which holds the run-level context that decision needs.

## `llm_kit.execution`: shared bootstrap contracts

`llm_kit.execution` holds the bootstrap event and interrupt adapters, the shared
schema-validator loader (`llm_kit.execution.schema_validator`), the probe helpers,
the common request and resolution values, and lapse delegation. Product interrupt
records, transitions, schemas and migration loops stay in `jobs` and
`pipeline.execution`. A generic migration runner is deliberately not part of this
layer: extract one only after the two product runners are shown to share an
algorithm rather than only similar syntax.

## `llm_kit.adapters`: transports and harness integrations

**Codex and OpenCode carry rules their siblings do not need.**

- **Model-authored text must stay out of exception messages.** Codex writes its
  transcript to BOTH stdout and stderr, and `halt` classifies by substring-matching
  an exception's message -- so `CodexRunError` keeps the transcript on attributes
  and out of the message. Inlining a channel into that message makes a healthy run
  that merely discusses a rate limit classify as a persistent halt and abort the
  caller's whole run.
- **OpenCode is workspace-guarded by policy.** Its required `--auto` flag approves
  permission prompts, so `OpencodeCliBackend` injects a highest-precedence
  `OPENCODE_CONFIG_CONTENT` policy that denies `external_directory` and `task`
  globally and on the explicitly selected `build` agent for every unattended run.
  The adapter also passes `--pure` to disable external plugins. `--dir` selects the
  intended workspace. Shell work remains available, so this is a practical
  OpenCode guardrail rather than an OS sandbox guarantee.
- **Home of the codex temp files.** `codex_out_*.txt`, `codex_schema_*.json` and
  `codex_catalog_*.json` (the text-only `model_catalog_json`) are created with
  `tempfile.mkstemp` in the OS temp directory and deleted in the call's `finally`.
  Per `plugins/CLAUDE.md` ("Where a plugin writes its own data") that is
  project-ephemeral scratch in neither project nor user home: one call's private
  working file, never read by another process after the call, and not derived from
  the script's location. It writes no durable path.
- `run_cli_streaming` is the transport-neutral subprocess runner (command in, stdin
  written, both pipes drained on daemon threads, bounded timeout, caller-supplied
  hard-stop markers). `run_claude_streaming` remains an alias because
  `llm_kit.adapters.backends` imports it by name as `ClaudeCliBackend`'s default
  runner. The runner's `(stdout, stderr, returncode)` contract does not carry a
  codex result -- codex returns via `-o <FILE>` -- so `CodexCliBackend` manages a
  temp output file around the call rather than parsing stdout.
- `CodexCliBackend` builds argv exclusively through
  `llm_kit.adapters.codex_command.build_codex_exec_argv`, the one owner of Codex
  detection and command construction. `bootstrap_lib.codex` is an exact alias of that
  module for older callers and never a second implementation.

**Effort delivery is specialized per endpoint and reported per call.** The openrouter
family record keeps `effort` in `dropped_params` (the truth for an unknown endpoint) and
lists it in `conditional_params`; `llm_kit.adapters.endpoint_profile.endpoint_capabilities`
specializes the record per endpoint, and `OpenRouterBackend.params_report` derives the
per-call report from the effort plan -- the CLI failure envelope calls it too, so a failed
call never reports an overridden or suppressed effort as delivered. The factory
(`BackendSelection.effort`), the `complete` verb and `declaration.run` fill the registry
`reasoning_effort` default into `opts.effort`. `llm-kit models resolve` reports `effort` (the
delivered value, null when undeliverable), `declared_effort` and `effort_delivery`;
`llm-kit models endpoints` reports `reasoning_effort` and `effort_delivery` per transport
entry.

**Usage readers read files harnesses already wrote, and read no credential.**
`llm_kit.adapters.usage_sources` holds the raw readers; pacing policy stays in
`dispatch`. claude's numbers come from claude-ui-kit's statusline snapshot at
`~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json` -- a FILE
CONTRACT, optional on both sides, neither plugin depending on the other -- because
the statusline hook payload is the only surface on which Claude Code emits
`rate_limits` at all. codex's come from the newest session rollout under
`~/.codex/sessions/`. Anthropic's `/api/oauth/usage` would answer more completely,
including the per-model bucket, and is deliberately NOT called: it needs the
user's OAuth access token, and a pacing check is not a good reason to put a
subscription credential into this code path. Where a CLI does not expose usage, the
honest outcome is `status: "no-data"`, not a token read.

**An exhausted codex account reports no window at all**, so the window rule alone
would read it as `no-data` and keep the seat. `read_codex_pool` treats null
`primary` AND `secondary` plus `credits.has_credits: false` (not unlimited) as OUT
OF QUOTA. `has_credits: false` alone is not the signal: a healthy plan account
reports it on every reading, because it describes purchased extra credits. The
verdict ends at the `usage_limit_exceeded` error's "try again at" time, or 5 hours
after the reading when that text is absent. Without the bound, a dropped seat
launches no new codex session, so a stale reading would never be replaced.

### `llm-kit local swapper`: operator lifecycle over a llama-swap swapper

`llm_kit.adapters.swapper` (process mechanics in `llm_kit.adapters._swapper_process`)
is a **CLI operator tool, not a client API.** A pipeline or script that wants a
model already resolves it through the registry and the completion seam; this group
is for the person (or the launcher script) managing the swapper process itself.
`--help` on any subcommand is the detailed reference; this section states only what
a reader would otherwise have to reverse-engineer from the source.

`running`, `terminate` and `unload` take exactly one of `--endpoint NAME` (a
configured model-endpoints registry entry) or `--url URL`; `strays` scans the local
process table and takes no target. Every verb except `guard-launch` takes `--format
json|text` (default `json`); `guard-launch`'s only argument is `--caller-pid`. Exit
codes are shared across the group: `0` success (or, for `strays`, none found); `1`
an operation failure, a safety refusal, a missing resident model, or (for `strays`)
strays found; `2` a usage/configuration error, including aiming `terminate` or
`unload` at a non-loopback target; `3` reserved for `guard-launch`'s explicit
`LaunchRefused` only -- never reused by another verb, because the launcher that
calls it must tell an unambiguous refusal apart from an uncaught exception, which
exits `1` like any other; `5` the inspection itself could not run to a verdict
(access denied, or `psutil` unavailable), never conflated with "not found" or
"unreachable".

`terminate` and `unload` verify the target through the local process table before
signalling anything (loopback-only, exact same-user `llama-swap` listener, direct
child, port match, `create_time` re-checked immediately before the signal) -- see
the six numbered safety rules in the `llm_kit.adapters.swapper` module docstring.
Signals never target a PID by name or command-line pattern.

`unload` is **operator-only and never a client action**: it stops every resident
model with no drain of in-flight requests, which is why the CLI requires both
`--all` and `--accept-no-drain` even for one call -- neither flag has a default
that lets the operation proceed silently.

`strays` reports recognized model-server processes (`ninfer-serve`, `llama-server`;
`mlx_lm.server` by argv only, since its process name is Python) with no swapper
ancestor -- residency that bypassed the launcher guard below, or survived a swapper
that exited. It detects and refuses; it does not kill anything.

`terminate-listener --port PORT --accept-replace` is the one explicit replacement
verb (`llm_kit.adapters.swapper.terminate_listener`), used by host launcher scripts kept
outside the plugin.
It is not for a llama-swap child and not automatic reaping: private service
wrappers own pre-start reaping, `strays` stays detect-only, and clients and probes
never kill. Rules a reader would otherwise reverse-engineer: records whose KNOWN
owner differs from the caller are dropped before any listener inspection (same
shape as `find_strays`), so root or another user cannot block it through unreadable
sockets, while an unknown-owner or unreadable same-owner candidate refuses as
indeterminate (exit `5`; on macOS, owner-`None` AccessDenied records can therefore
make it refuse, and a zombie process reads the same way). "Free" needs BOTH the
bind check and a scan of same-user listener sockets that finds none on any address
or family, because a bind check alone can pass beside a listener bound to one
specific address or only to IPv6 (macOS, Windows); an unreadable scan is never
free. A free port is a no-op; an occupied port with no recognized same-owner
listener, an unrecognized or second listener, or a llama-swap descendant refuses
before any signal. Recognition is `classify_server`, the `strays` set. It snapshots
PID and create time, rechecks, sends SIGTERM (grace default 20 s, longer than
`terminate` because model servers unload weights), and sends SIGKILL only when the
same PID and create time still exist. Success requires the same two-part free test
(`llm_kit.adapters._swapper_process.port_is_free`: loopback connect plus IPv4 and
IPv6 binds, and an empty listener scan); otherwise it refuses without signalling
again. Another user's listener is invisible to the scan, so only the bind check
covers it. Tests never signal a real process: the library tests use a fake
inspector and an injected `port_free`.

`guard-launch` is the facade a host launcher script calls immediately before `exec`, so a
manual launch cannot bypass an active same-user swapper; the plugin ships no such launcher,
and the external launchers depend on this exit-code contract. It is **fail-open by
contract**: only `LaunchRefused` (exit `3`) blocks the launch. Every other outcome -- no
Python interpreter found, the guard script missing, `psutil` unavailable
(`InspectionIndeterminate`, exit `5`), any other nonzero exit, or an uncaught exception (exit
`1`, indistinguishable on purpose from a refusal the guard never issued) -- makes the
launcher warn on stderr and proceed with the launch. A broken guard must never stop
llama-swap's own children from starting; the asymmetry is why `LaunchRefused` alone carries
exit `3` while every sibling `SwapperError` maps to `1` or `2`. Opt-outs for a deliberate
manual launch beside an active swapper belong to the launcher, not to this verb.

## `llm_kit.dispatch`: selection, reachability and pacing

### Reachability is not configuration, and it is never a completion

`llm-kit models endpoints` lists what is CONFIGURED and is pure static data --
always instant, never touches the network. `llm-kit models endpoints --verify` and
`llm-kit models probe` answer a different question, whether a configured endpoint
is ACTUALLY USABLE right now, through one shared code path
(`llm_kit.dispatch.reachability`) that a consumer can also call directly. Both
surfaces are opt-in / explicit-target only: a bare `endpoints` call must never
start paying for a network or subprocess call silently.

Verification costs zero LLM calls, on either endpoint kind. A **transport** entry
(the `openrouter` adapter, including a self-hosted OpenAI-compatible server) gets a
`GET {base_url}/models` metadata probe -- proof the server answers HTTP, not that a
completion would succeed, which is why a passing verdict is `status: "reachable"`
rather than `available` or `healthy`. A **harness** entry (claude-cli, codex-cli,
opencode-cli) gets a CLI-resolution-plus-`--version` check -- proof the CLI is
invocable, weaker still, since a real completion would spawn an agent and cost real
time/quota and is therefore never attempted. Do not add a probe path that runs
`backend.complete(...)`, even with `max_tokens=1`: a caller reaching for a liveness
check explicitly does not want to spend a token finding out.

**Status is a three-way string, `reachable` / `unreachable` / `unknown`, never a
bool.** `unknown` means the check itself could not be run to a verdict (an optional
dependency was unavailable, or the check machinery raised unexpectedly) -- and it
is NEVER reported as `unreachable`. "I could not check" and "I checked and it is
down" are different facts, and a bool collapses them: a consumer gating on
`reachable is False` would silently skip a perfectly usable endpoint whose check
never ran. `llm_kit.dispatch.reachability.check_entry` wraps every dispatch in a
catch-all that maps an unanticipated exception to `unknown` rather than letting it
escape or misreporting it, so this invariant holds even for a failure mode nobody
has hit yet. `probe` propagates the same three-way split to its exit codes: `0`
reachable, `1` unreachable, `5` unknown -- `EXIT_INDETERMINATE`
(`llm_kit.cli._common`), deliberately a different code AND a different axis from
`EXIT_USAGE` (2), which means the endpoint *name* never resolved to configuration at
all, decided before any check is attempted.

**`codex-cli` reachability is `llm_kit.adapters.codex_command.detect_codex`, the same
detector the backend and the harness adapter use.** It is cached for the process, reads a
structured version and fails closed: a CLI that is not on PATH, does not run, times out or
exits nonzero is `unreachable` with the specific reason, so a verdict here and a dispatch
agree on whether Codex is installed. The module is standard-library only and imports
nothing from a plugin or a harness, so there is no optional import whose failure could be
mistaken for a verdict about the target. Any per-harness check that adds a "preferred path,
PATH-based fallback" shape keeps the rule: an optional dependency failing to import is
`unknown`-shaped information about the CHECK, not `unreachable`-shaped information about
the TARGET, unless a real fallback check exists to produce an actual verdict.

### Usage pacing is a third axis, and it fails open

`conserve_usage` (evaluated in `llm_kit.dispatch.usage_budget`) answers a question
neither `endpoints` nor reachability can: an endpoint that is configured AND
answering may still be one whose subscription quota is being burned faster than the
clock.

**Two thresholds, two different consequences, and collapsing them is the mistake to
avoid.** With `r` the fraction of quota remaining and `t` the fraction of the window
remaining: `r <= 0` is OUT OF QUOTA and DISABLES the model -- the pool is spent, a
call would fail, so it leaves selection entirely. `r < t` is UNDER QUOTA and only
DE-PRIORITIZES it -- it is being spent quickly but still has capability, so it stays
usable and merely loses to an equally-suitable peer that is not behind pace.
Withholding a model for the second reason costs the caller something it still had.

**No data never withholds a model.** A missing snapshot, an absent pool, or a window
that has already reset yields `no-data` and the endpoint stays usable -- the same
rule reachability applies one axis over ("I could not check" is never "it is down"),
with a second concrete reason here: the per-model bucket is emitted by the server for
some accounts only, so failing closed would make fable permanently unreachable on
every machine whose payload omits it.

**A verdict is pinned for the session** (`CLAUDE_CODE_SESSION_ID`); the stance and its
rationale are the register entry in `plugins/CLAUDE.md`.

`llm_kit.dispatch.seats.discover_seats` applies both consequences: an out-of-quota
seat moves to `SeatsResult.out_of_quota` (reported, not dropped, so "no seat above
me" stays distinguishable from "the seat above me is spent"), while an under-quota
seat stays in `seats` and sorts after any peer of the same relation that is not
behind pace. Quota rank sits BELOW relation and ABOVE tier -- a seat's relation says
whether it can do the job at all, which outranks how much budget is left.

### Declarations: `describe` and `run`

`llm_kit.dispatch.declaration` is the consumer-facing half and the one API over a
model declaration. `describe` classifies every declared id, keeps usable,
out-of-quota and unreachable entries in the render, hides the rest SILENTLY, orders
the render by pace (`order_by_pace`), marks the default, and emits the choice and
re-selection rule text (`Ranking.rule`). It raises `NoUsableRoutingTarget`, which
itemises every declared id, when nothing usable remains. It RANKS and does not
dispatch, which is what lets `llm_kit.jobs` pass its own requirements, capabilities,
factory, exclusions and run-scoped reachability cache into one selection it still
owns. `run` is the dispatcher for callers with no loop of their own. Easy to break:

- **RENDER, SKIP and FLOOR are separate surfaces.** Nothing may name a hidden id
  outside the floor, including the render header. A notice, warning or log line for
  a skipped id reverses the silent-skip ruling for declarations.
- **Status is pinned, pace is fresh.** `usable` and `[default]` come from
  `pinned_evaluate`. Only the pace number comes from an unpinned read, so a pinned
  AVAILABLE verdict still never flips on a re-read.
- **`max_attempts` is not the floor.** A halt that uses up the last attempt returns
  `attempt-limit`. It never raises `NoUsableRoutingTarget`, because the pool was not
  empty.
- **`run(..., observer=)` reports execution events and refuses without their
  module.** The keyword-only `observer` (`ExecutionObserver`, an `emit` method
  keyword-compatible with `bootstrap_lib.execution_event.Emitter.emit`) gets
  `dispatch-selected`, `call-started`, `usage` and `result` per attempt (`attempt_id`
  is the attempt number as a string) and one `terminal` per call (`unroutable` before
  the floor is raised). Usage goes only through
  `bootstrap_lib.execution_event.usage_payload`, so null means unknown and is never
  written as 0. An observer exception propagates. Bootstrap >= 0.135.0 is required,
  and its absence raises `DeclarationSupportError` before any dispatch. Detail: the
  `llm_kit.dispatch.declaration.run` docstring.
- **A session caller hides transports unless it says it can run them.** A transport
  entry has no agent loop, so `caller="session"` classifies it unroutable by default.
  A session caller that reaches transports through a runner of its own passes
  `dispatchable=("transport",)` (CLI `--dispatchable transport`); the code-review
  skills do, for every reviewer lane the lane runner binds to a transport.
- **A session caller records its own quota halt.** `run()` and the jobs runner call
  `record_observed_halt` for halts they observe. An agent driving the harness itself
  observes the halt instead, so `RULE_TRIGGER_SESSION` tells it to run `llm-kit
  models record-halt` before re-selecting; without that, the pinned AVAILABLE
  verdict keeps the spent entry `[default]` for the session.
- **A halt spends the whole pool.** Every caller passes its registry as
  `record_observed_halt(..., entries=...)`, which writes OUT-OF-QUOTA for every entry
  sharing the halted entry's `quota_pool_key` (same harness account, same pool) --
  the same set `describe` labels "shares <pool> with". Without it, each sibling costs
  one failed dispatch. A new caller that omits `entries` reintroduces that gap.

`llm-kit models usage` and `llm-kit models describe` are the inspection surfaces for
a check that is otherwise invisible.

### Backend construction

`llm_kit.dispatch.factory.create_transport_backend` and `create_backend` select a
backend for an endpoint; `llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory`
is the default `BackendFactory` the CLI injects. It builds from an immutable
construction spec that preserves every constructor option (endpoint, project root,
client, timeout, retry, diagnostics, executable, argv prefix, runner, model
endpoint). An omitted runner stays omitted from the concrete constructor rather than
being passed as `None`, so each adapter keeps its default runner semantics. The
pipeline runtime's backend wrappers (`llm_kit.pipeline.runtime.backends`) pass a
spec to an injected factory and never import `adapters`.

## `llm_kit.frontdoor`: cross-caller concurrency

The run-of-many rule above governs the COMPLETION SEAM: one process, one
`complete()`, no policy. `llm_kit.frontdoor` is not that seam. It is an
OpenAI-compatible HTTP server (`llm-kit frontdoor`, launched by
`scripts/frontdoor.sh` from the plugin venv, `--print-command` supported) that sits
in FRONT of the registry's transport entries and arbitrates admission ACROSS every
caller on the LAN -- job runs, pipeline waves, opencode sessions, a curl. That is the
one concurrency concern no caller can own, because no caller can see the others'
in-flight requests; a bounded local model server queues or 503s whoever arrives
fifth, so the arbitration has to sit where all the requests pass through. The seam
stays run-once; the front door owns the run-of-many across processes.

What it reads: `routing:` on a transport entry in the user's model-endpoints registry
-- `group` (the model name callers send), `order` (tier; lower fills first),
`max_parallel` (cap; omitted = uncapped, only sensible on the last tier),
`effort_style` (`top-level`, `ninfer` = top-level with `high` mapped to `xhigh`,
`chat_template_kwargs`, or `unsupported`; omitted = top-level, and an entry-level
`effort_style` overrides it). Fill-first: the lowest tier fills to its cap, the next
tier takes the excess, and `--spill-after` (default 0 s) is how long a request waits
for a slot in a lower tier before spilling. A transport entry WITHOUT `routing:` is
not a deployment, which is how the front door's own registry entry stays out of its
own tiers; `--check` lists every untagged transport entry so a forgotten tag is
visible rather than silently absent.

What it owns and what it does not: in-memory in-flight counts (hence ONE uvicorn
worker, enforced in `main`), one retry onto the next deployment on a connection
error or 5xx, the `user` field stripped after logging (no backend sees it), a JSONL
access log, `/health` (liveness plus per-deployment counts, never an upstream probe)
and `/who` (in-flight requests with their `user` ids). It does NOT own cost
accounting, caching or budget guarding -- those stay with the caller exactly as the
seam rule says. Keys for keyed deployments resolve through
`llm_kit.registry.api_key.get_api_key`, so the hosting machine needs the secrets
layer, not an exported env var.

**Reported cost has one read point and a trust gate.** The front door strips upstream
`usage.cost` / `usage.cost_source` unless the serving deployment is
`provider-reported`, and injects `cost: 0.0` + `cost_source: registry-unmetered` only
for `unmetered`. `OpenRouterBackend` reads native `usage.cost` once and accepts it
only for a `frontdoor: true` or `provider-reported` endpoint (an unmarked provider
cannot opt in via response content). The paired `LLMResponse.reported_cost_usd` /
`reported_cost_source` are both present or both `None`, finite and non-negative;
invalid values are ignored, never coerced to zero. Streaming is not normalized.

**Backend health is a preference with a fallback, inside one budget.** `GET /health`
is liveness only (never an upstream probe). `GET /health/backends?budget_ms=N`
(protocol 1) probes the app's OWN loaded registry entries
(`llm_kit.adapters.account.probe_entry`, explicit timeout, never the 2 s default and
never a re-load from disk), and `llm_kit.dispatch.reachability.check_transport` asks
it only for a `frontdoor: true` entry. The health socket gets `HEALTH_SHARE` of the
client timeout, the requested inner budget is strictly below that socket budget, and
the `/models` fallback gets only what is left, so health plus fallback never exceed
the caller's one timeout. Anything other than a decisive `reachable`/`unreachable`
group verdict falls back. Do not make the front door's group aggregate the only
signal: a group with an uncapped paid tier reads `reachable` even when local
deployments are down, so the per-deployment statuses stay visible in the detail.

### `llm-kit local acceptance`: proofs that must be able to go red

`llm-kit local acceptance swapper|frontdoor` (`llm_kit.adapters.swapper_acceptance`,
`llm_kit.frontdoor.acceptance`) asserts a deployment through its HTTP surface only
(`/v1/models`, `/running`, `/health/backends`, `/v1/chat/completions`, the
`x-frontdoor-deployment` header), never through host process access. Exit `0`
passed, `1` an assertion failed (an unreachable target included), `2` usage or
configuration.

- **Never-evict needs a constructed overlap.** Alternating rounds cannot test
  greediness: each waits for the previous one. The overlap check runs a long request,
  demands the other model mid-flight, and fails both when the second model becomes
  resident early and when the overlap did not occur (the busy model was never
  observed resident, finished before the demand, or was never sampled while both
  were pending). A pass without the second condition would be vacuous.
- **Paid rule.** A tier is paid unless the registry declares `billing.mode:
  unmetered`; an undeclared tier is paid (fail closed), because `key_env` and
  hostnames do not distinguish owner-funded from metered. Without `--paid`, the
  spill-group fill burst is sized to the capped capacity of unpaid tiers preceding
  the first paid tier, and the paid spill leg is reported as skipped; under `--quick`
  it and the queue-overfull leg are not run at all. The run plans no request onto a
  paid tier and fails if one answered, but concurrent front-door traffic from other
  callers can still spill a burst request onto an uncapped paid tier; run it when
  the front door is otherwise idle. The queue-overfull leg sends one request past the
  queue group's total cap; that request queues on the group's capped tiers and does
  not spill. Every leg is refused, sending nothing, when a tier it needs is not
  `reachable` in `/health/backends`, and both queue legs are refused without `--paid`
  when any queue-group tier is paid (an uncapped queue tier is a configuration error,
  exit 2).
- **Tier shape comes from data.** Order, cap and billing come from the supplied
  registry through `llm_kit.registry.model_endpoints.load_endpoint_registry`;
  deployment ids must equal `/health/backends` for the group. Nothing fleet-specific
  is a literal in this library. `--expect-tier1-cap` swaps only the first spill-group
  level's expected count, so a wrong value must go red.
- Tests use stdlib fake servers. When you edit an acceptance test, show it to fail
  with its assertion removed before trusting it.

## `llm_kit.jobs`: flat durable jobs

User-facing behavior is `references/jobs.md`. Run policy over
`llm_kit.dispatch.declaration.describe(caller="process")`: halt narrowing, attempts
and the ledger's pace-reading record. The runner's writes go only to its existing
ledger (the SQLite store); it adds no second home for state.

**Backpressure: wait, then retry as a new attempt.**

- A seam call that ends in a backpressure or rate_limit halt is recorded as its own
  attempt row with that halt; never loop on `backend.complete` under one attempt
  number (the ledger's central claim; see `plugins/CLAUDE.md`).
- A `job-kit:backpressure-wait` event follows the attempt and marks it waited out. A
  waited-out attempt does not count toward `max_attempts`
  (`JobStore._reservation_budget_count`) and does not narrow dispatch (it adds
  nothing to the job's halted endpoints). The next call is a new reservation on the
  same model.
- When the next wait would pass the cap, that attempt is a plain `rate_limit` halt:
  it spends the budget and excludes the endpoint. Quota, credit and auth halts are
  never waited out.
- Code: `llm_kit.jobs.run.run_job` loops over `_run_job_attempt` with a `_WaitBudget`.

| Opinion | Default | Setting |
|---|---|---|
| Total wait per call | 600 s | env `JOB_KIT_BACKPRESSURE_CAP_S` (`0` disables), `BackpressurePolicy.cap_s` |
| First backoff without `retry_after_s` | 2 s, doubling | `BackpressurePolicy.base_s` (code only) |
| Backoff ceiling | 60 s | `BackpressurePolicy.max_s` (code only) |
| Jitter | +/-25% | `BackpressurePolicy.jitter` (code only) |

Razor verdict for base, ceiling and jitter: a deliberate code-level default; a caller
wanting another curve passes its own `BackpressurePolicy`.

**Cancel is a ledger edit, not a signal.** `JobStore.cancel_jobs` (and `mark_cancelled`
for one job) moves pending, running and waiting jobs to the terminal `cancelled` state in one
transaction: it resolves the open interrupt (the schema-13 `cancelled` resolution outcome), the
live continuation and the live reservation, and records `result` (`lost`) and `terminal`
events. A terminal job is untouched, so it is idempotent. The CLI is `llm-kit jobs cancel`
(exit 0, or 3 for an unknown run or job or a missing store). It cannot stop a live runner;
the precondition that the run process is already stopped is the caller's. Do not add an
`interrupt` event phase for it: the phases belong to `bootstrap_lib.execution_event`.

The job ledger keeps its one-process-per-ledger rule (see "Persisted state keeps its
historical names"); per-attempt worktrees are opt-in (`workspace.isolate: true`), and the
rationale for the job-selection, workspace and interrupt stances is the register in
`plugins/CLAUDE.md`. The rules the job runner shares with the pipeline for durable
interrupts execute in `bootstrap_lib.interrupt_contract`; each product keeps its own
store, continuation and event phases.

## `llm_kit.pipeline`: the run of many calls

`llm_kit.pipeline` answers the questions a RUN of many LLM calls raises. Those
mechanics are the same whatever the content is; the domain judgement stays in the
consuming project. Its components: `core` (stages, work units, gates, convergence,
seeding, validator and rejection contracts, extension ports), `artifacts` (stores,
freshness, delivery, round trip, providers, VCS adapters), `runtime` (the call
economics: backend binding, retry, backpressure, cache, cost, budget, validate loop,
spend ledger), `execution` (leased durable units, claims, fencing, waves, workers,
drivers, status) and `analysis` (audit, provenance, replay).

**The boundary with the completion seam.** `llm_kit.completion` owns making ONE call
correctly -- endpoint, model, key, transport, halt taxonomy. `pipeline.runtime` owns
what only exists across a RUN of calls: the content-addressed response cache,
run-level retry, cost accounting, the token/cost budget guard and the
validate-until-valid submission loop. Each of those is a policy a pipeline can answer
and a transport cannot. They live in `llm_kit.pipeline.runtime.platform`;
`llm_kit.pipeline.runtime.backends` holds the thin wrappers over the dispatch factory,
so `MockBackend` needs no transport and no SDK. The lower layer not implementing these
is the boundary working, not a gap a consumer is left to fill. When adding a concern,
keep it in `pipeline.runtime` unless it is a fact about making a call rather than about
running many.

**What consumers adopt, and what they rebuild.** Two independent production consumers,
building unrelated content on this library without coordinating, adopted the HORIZONTAL
concerns (freshness hashing and classification, attributed and intermediary stores, the
validation contract, the runtime platform and backends) and wrote their own VERTICAL
ones (the orchestration loop, the candidate store, the human round trip, the delivery
choreography for their VCS). When two unconnected consumers stop at the same line, treat
that line as a real seam. Two consequences for how to answer a consumer: a consumer
starting clean should try `pipeline.*` before writing its own, and a consumer porting a
working loop should expect to keep it; when a consumer keeps its own loop, say the cost
out loud, because a generic implementation left unused is still a maintenance burden.
The seam is not a reason to stop asking whether a vertical module has earned reuse.

### Cost accounting prefers reported cost

`response_cost` (`llm_kit.pipeline.runtime.platform`) returns zero for a cache hit,
then the authoritative `reported_cost_usd` of a live response, then the pricing-table
estimate, and `None` (unknown, never zero) when there is neither and `pricing=None` was
passed. `call_llm` therefore charges a reported cost with no pricing table; the budget
cap is reachable that way. The runtime backend adapter reads the pair with `getattr`
and drops an invalid or one-sided pair. `ResponseCache` stores both fields as
provenance; entries written without them still load. Exception charges stay
estimator-based. User docs: `references/pipeline-library.md`.

### Backend selection

Routing is per pipeline when the consumer passes the declaration explicitly:
`route(models=[...])`, `routed_model(requested, models=[...])` and
`declared_model_names(models)` take an ordered list of registry ids. The list wins over
the environment, so two pipelines in one process can route differently; the declaration
memo is keyed on the names. An empty explicit list raises `ConfigurationError`.
`CONTENT_PIPELINE_LLM_MODELS` (comma list) is the process-wide default only when `models`
is `None`; a supplied `mock` still wins unconditionally.

With the environment default, selection is process-global: one
`CONTENT_PIPELINE_LLM_MODELS` declaration picks the entry, and its model, for the whole
process. One exception: when the declaration resolves to the `openrouter` entry,
`route()` returns a caller-supplied `openrouter=` instance instead of building a fresh
one, and `routed_model()` returns a non-empty caller-requested model instead of the
entry's model. An empty request, or an entry naming another transport, still runs the
declaration's entry and model. The consequence to state to a consumer: two pipelines that
need different backends cannot share a process unless each passes `models=`, and with the
environment default nothing at a call site signals that one of them got the other's
backend, so a changed environment variable can move output quality with no local signal.

Only `CONTENT_PIPELINE_LLM_MODELS` routes. `CONTENT_PIPELINE_LLM_BACKEND`,
`CONTENT_PIPELINE_LLM_MODEL` and `CONTENT_PIPELINE_LLM_ENDPOINT` raise
`ConfigurationError` when set without it
(`llm_kit.pipeline.runtime.backends._refuse_removed_routing_env`; details in
`skills/content-pipeline-domain/references/building-a-pipeline.md`).

A supplied `mock` wins unconditionally in `route()`, checked before the declaration is
even read: `route(mock=FakeBackend())` always returns the supplied instance, regardless of
`CONTENT_PIPELINE_LLM_MODELS`. A test needs no environment setup to keep a routed call off
a live transport.

### The adaptive gate

An overloaded endpoint is a halt of kind `HALT_BACKPRESSURE` (`"backpressure"`) carrying
`retry_after_s` (`None` without a hint), distinct from quota, credit and auth halts.
`call_llm` does not retry it; wrap calls in `llm_kit.pipeline.runtime.gate.AdaptiveGate`
(or `call_llm_gated`), one gate per run. On the halt the gate releases the slot, halves
the concurrency limit (floor 1, once per wave), waits `retry_after_s` (else exponential
backoff with equal jitter), and retries. Every `recover_after` consecutive admitted calls
raise the limit by one, back toward `jobs`. A call still refused after `give_up_s` raises
`EndpointBusyError`, and so does every later call at once. Quota, credit, auth,
unreachable and all other errors pass through. `clock`, `sleep` and `jitter` are
injectable. The retry-after hint falls back through
`llm_kit.completion.halt.classify_backpressure`: the edge is DEGRADE
(`llm_kit.pipeline.runtime.platform._backpressure_classifier`), since without it the halt is
still true and only the wait is less informed; an absent or stale classifier raises a
distinct `RuntimeWarning` naming the cause and the remedy, once per state.

A caller outside `call_llm` supplies its own error shapes through three constructor seams.
`is_backpressure(exc)` decides what is waited out (default `gate.is_transient_halt`).
`retry_after(exc)` reads the wait hint (default `gate.halt_retry_after`, which raises
`TypeError` for an error that is not a `PipelineHaltError`); `retry_after=None` always backs
off. `not_admitted(exc)` marks an error raised before dispatch: its slot is released
without counting toward recovery (default: nothing is). For a gated `call_within_budget`,
pass `deadlines.refused_before_dispatch`: it accepts `CircuitOpen` and `BudgetExhausted`
(both `deadlines.NotDispatched`; `BudgetExhausted` is also a `DeadlineExpired`) and rejects
a `DeadlineExpired` raised after dispatch, which counts as admitted. `run(fn, label=)` names
the call in the notice line.

| Opinion | Default | Seam | Verdict |
| --- | --- | --- | --- |
| `BASE_SECONDS` | 30 | `AdaptiveGate(base_s=)` | constructor parameter with documented default; the seam |
| `CAP_SECONDS` | 600 | `AdaptiveGate(cap_s=)` | same |
| `GIVE_UP_SECONDS` | 3600 | `AdaptiveGate(give_up_s=)` | same |
| `RECOVER_AFTER` | 5 | `AdaptiveGate(recover_after=)` | same |
| `DEFAULT_CIRCUIT_LIMIT` | 2 | `DeadlineCircuit(limit=)` | same |

A constructor parameter with a documented default is the library's seam; no config-file key
is added, because a consumer calls these constructors itself.

### State files: which home

`FailureCache(path)` and `DeadlineNotes(path)` take the path from the consumer; nothing
derives from the module's location, and the atomic-JSON writer creates its temporary file in
the target's directory. `FailureCache` is project-durable (`.plugin-data`): a cross-run record
about the consuming project whose loss costs a repeated model call. `DeadlineNotes` is
project-ephemeral (`.local-data`): it only orders work. Neither is user-scoped. The job store
defaults to the project-ephemeral `.local-data/plugins-kit/job-kit/runs.sqlite3` under the
working directory, and the spend ledger is a file the consumer names.

### Parallel graph, deadlines and the failure cache

`scheduler`, `deadlines` and `failure_cache` (in `llm_kit.pipeline.execution`) import only
the standard library.

**Parallel dependency graph.** `ParallelGraphStrategy(dependencies, capacity=1,
payload_of=None, deferred=frozenset())` names each unit's direct dependencies.
`wave.ready_wave` releases every `pending` unit whose dependencies are settled (`skipped`,
or `accepted` and applied), up to `capacity` in flight, `deferred` units last. A unit that is
`failed`, `operator_rejected`, `interrupt_expired`, or accepted with its apply refused gates
its transitive dependents only; `wave.gated_units` lists them. The graph is validated at build
(unknown, duplicate and self dependencies, cycles), and registered unit ids must equal its node
ids (`GraphRegistrationMismatchError`); register them in `strategy.node_ids` order.
`max_wave_size` and `reclaim_at` apply as for the other shapes; `prepare_run` and
`workerpack.build_wave_args` narrow to the same wave. A drain loop calls `finalize_run`
between waves and is done when every unit `unfinished_units` returns is in `gated_units`.
Without a durable store, `scheduler.Scheduler(nodes, capacity=)` holds the same rule in memory
(`admit_ready`, `complete`, `fail`, `requeue` with a quiet period) and
`scheduler.run_graph(scheduler, execute)` runs it on a thread pool. An executor raising
`NodeFailed` fails its node and gates its dependents; any other exception stops admission,
waits for running nodes, and is re-raised.

**The parallel graph is a third strategy, not a mode of the graph walk.**
`ParallelGraphStrategy` is deliberately NOT a `GraphWalkStrategy`, so
`wave.is_graph_strategy` stays false for it and every sequential-graph guard
(`UnsafeGraphParallelismError`, `prepare_run`'s order and unapplied-predecessor refusals,
`graph_block_reason`) keeps its meaning. A code path that selects units outside `ready_wave`
must test `wave.is_dependency_ordered`, not `is_graph_strategy`, or it bypasses the parallel
graph's dependency and capacity rule; `controller._wave_with_reclaims` and
`workerpack.build_wave_args` do. The in-memory scheduler and the durable wave share one rule
(`validate_graph`, `dependents_closure`); change them together. `scheduler`, `deadlines`,
`failure_cache` and the atomic-JSON writer are stdlib-only by test, because a consumer's
portable run code imports them without the rest of the package. Field names are generalized
(`stage`, `unit_id`, `subject`). User docs: `references/pipeline-library.md`.

**Per-unit deadline and run-wide circuit.** `deadlines.UnitBudget(seconds)` is one unit's
wall-time budget, shared by its first call and repairs. `call_within_budget(budget, timeout_s,
call, is_timeout=..., circuit=..., uncharged=...)` runs one admitted call with `min(timeout_s,
remaining)`, charges its wall time, turns a budget-imposed timeout into `DeadlineExpired`, and
refuses before dispatch when the budget is spent (`BudgetExhausted`) or the circuit is open
(`CircuitOpen`); both are `NotDispatched`. Call it inside the gate,
`gate.run(lambda: call_within_budget(...))`, with `uncharged` accepting the backpressure error
so queueing and backoff are not charged, and the gate built with
`not_admitted=deadlines.refused_before_dispatch` so a refusal does not count toward recovery.
`DeadlineCircuit(limit=2)` opens after `limit` consecutive units END on a deadline
(`record_outcome`, once per unit) and stays open. `DeadlineNotes(path)` counts expiries per
attempt key; a note orders work (mark the unit `deferred`) and never suppresses it.

**Negative failure cache.** `FailureCache(path)` records a deterministic failure as
`FailureRecord(stage, unit_id, attempt_key, error_fingerprint, fingerprint_version, code,
summary)`. `lookup(stage, unit_id, attempt_key)` hits only for the same attempt key;
`clear(stage, unit_id)` on success. `error_fingerprint(code, details)` digests a normalized
error. Never record a deadline, open circuit, transport error or backpressure. A corrupt cache
or notes file reads as empty and `report` says why.

### The durable wait is opt-in and lives in the execution store

A durable wait (`store.request_interrupt`, `InterruptRequested` in the inline lane) is a
waiting state of a unit in the execution store, requested per unit and per call. It adds no
flag, config key or manifest entry, and a consumer that never asks has no waiting units. It
leaves `llm_kit.pipeline.artifacts.roundtrip` and a consumer's own round trips alone: they are
a different shape (a question about an entity that re-enters as context between runs), and the
wait does not replace or wrap them. Keep it that way when editing either. The wait reaches
`bootstrap_lib` and the schema validator as libraries, inside its verbs, never at import. Say
the lane scope when describing it: the inline lane and a consumer's own loop can ask, the
background lane refuses under an open dispatch, and the workflow lane has no request surface
(do not describe it as refusing). User docs: `references/pipeline-library.md` and step 9 of
`skills/content-pipeline-domain/references/building-a-pipeline.md`.

## Cost classes and consent

Principle (user ruling 2026-10-23; `docs/reference/llm-kit-architecture.md`, "Caller consent"):
consent governs caller-selected execution policy. The library makes no LLM calls of its own, the
user of the library makes them, and writing code that invokes models needs no consent. Design record:
`docs/planning/shared-lib-packaging/consent-design.md`.

**Classification is information, reported with its reason.** Every declared entry has a cost class
(`llm_kit.registry.cost.classify`): `local`, `subscription` or `metered`, with no aliases. A
catalog row carries `cost` as `{class, reason}`, and a protocol 2 response carries `cost.reason`.
A `claude` or `codex` class depends on harness authentication, which costs a CLI probe, so a
protocol 2 request that did not opt in to consent reports that class as `null`
(see below).
Classification fails closed, so an entry that cannot be shown to be `local` or `subscription` is
`metered`; the name says billing is paid or unverified, never that a charge occurred.

- **`local`**: a transport whose registry entry declares `billing: {mode: unmetered, owner: self}`,
  and a front-door entry whose every tier (the transports with the same `routing.group`) does.
  `billing.mode: unmetered` states marginal cost, not ownership (a provider free tier is unmetered
  too), so an unmetered entry without `owner: self` is `metered` (billing unverified). The front
  door's own `billing` does not count, and a front door with no tiers is `metered`. A harness
  entry other than claude and codex that declares `endpoint: <transport entry id>` takes that
  entry's class (a front door counts every tier); an `endpoint` that is missing or not a transport
  is `metered`, and its reason names it. Without an `endpoint` an opencode entry is `metered`.
- **`subscription`**: a `claude` or `codex` harness running on a subscription session pool. Its
  authentication is probed, never assumed (`llm_kit.adapters.harness_auth`):
  - `claude`: any billing-diverting variable set and non-empty (`ANTHROPIC_API_KEY`,
    `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`,
    `CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `AWS_BEARER_TOKEN_BEDROCK`,
    `ANTHROPIC_VERTEX_PROJECT_ID`) is metered. Otherwise `claude auth status` must report
    `authMethod` `claude.ai` and `apiProvider` `firstParty`; another login is metered.
  - `codex`: `CODEX_API_KEY` or `OPENAI_API_KEY` set is metered, and `CODEX_ACCESS_TOKEN`
    alone is unknown. Otherwise `codex doctor --json` runs in the call's working directory
    and environment, and only provider `openai`, ChatGPT auth and no stored API key is a
    subscription. Another provider (from any configuration layer, including managed ones) or
    another auth mode is metered. No file-reading fallback exists.
  - A probe that cannot run, times out, exits non-zero or returns an unrecognized report is
    unknown, and unknown is `metered`. A harness other than `claude` and `codex` is always
    `metered`. A probe reason names fields and variables, never a secret value.
- **`metered`**: everything else, including a metered transport, an entry with no registry
  entry, and an unknown kind.

**The consent layer is optional and off by default.** `llm_kit.registry.consent` is a policy a
caller may apply; nothing consults it unless the caller opts in, and a caller may build its own
layer instead. The file is `$LLM_KIT_CONSENT_FILE` when set, else
`$XDG_CONFIG_HOME/llm-kit/consent.json`, else the legacy
`~/.claude/plugins/data/plugins-kit/llm-kit/consent.json` when the neutral file does not
exist (resolved on each call; "Persisted state keeps its historical names"), with
`{"schema": 2, "local": bool, "subscription": bool, "metered": bool, "scopes": {name: {class: bool}},
"updated_at": ...}`. The global classes are a ceiling and a scope may only restrict:
`effective = global AND scope.get(class, true)`, so an absent scope key inherits, and a scope `on`
that the global policy blocks is reported as blocked with the global command that lifts it. A
global change preserves every scope entry. With no file the policy is `local` on, `subscription`
on, `metered` off, with provenance `default`, and nothing is created; a file that is unreadable or
malformed raises `ConsentConfigError` when a caller opts in, and is never read otherwise.

- API: `llm_kit.registry.consent.policy(scope=None)` returns the effective `Policy` (`allowed`,
  `allows(cls)`, `refusal(cls)`, `remedy(cls)`, `to_json()`); `load_config`, `save_config` and
  `update_config` read and write the file atomically and propagate errors.
- CLI: `llm-kit consent show [--scope NAME] [--json]` prints the effective policy and the source
  of each value, and `llm-kit consent set [--scope NAME] --local|--subscription|--metered on|off`
  changes the one class given; `set` states that operations that opt in send their inputs
  unredacted to the providers of those models.
- Opt-in: `llm-kit models describe <ids> --consent [--consent-scope NAME]`, the protocol 2 request
  field `"consent"` (`false` or omitted: not opted in; `true`: global policy; `{"scope": NAME}`:
  scoped policy; a malformed object is refused), or the API call above. A misconfigured file is a
  `configuration` refusal only when the request opted in.
- Caller-owned filter: `describe(..., allow=...)`, `run(..., allow=...)` and
  `models describe --allow <classes>` take class names (an unknown name raises `ValueError`) and
  replace the layer. `--allow` and `--consent` are alternatives. Protocol 2 has no allow field, so
  an `allow` key in a protocol 2 request is refused as an unknown key.
- Discovery: `models catalog` and `models describe` carry a top-level structured `consent` note
  (the layer is optional, how to opt in, the commands), and `consent show` prints the policy.

With `allow` given, or the layer applied, an entry whose class is not allowed gets the disposition
`switched-off`, with its `cost_class` and the detail `<class> not allowed: <reason>`. Switched-off
entries are SHOWN, not hidden: `Ranking.switched_off` lists them, `render()` adds a
`Switched off: <id> (<class>)` line and `to_json()` a `switched_off` key (both only when there are
any), and the floor (`NoUsableRoutingTarget.switched_off_ids`) itemises them. They are never
dispatched to, and the check precedes the requirements match and every probe, so a switched-off
entry is never probed for reachability. Harness authentication is probed lazily, once per harness,
only for `claude` and `codex` entries, unless `describe` is given `harness_auth`. Without `allow`
and without the layer, nothing is filtered. A caller that opts in prints the policy's note itself;
the library never does.

Protocol 2 of `completion complete` (`references/completion-seam-contract.md`) classifies the one
entry a project selection names (for a `claude` or `codex` entry of an opted-in request, probing
harness authentication in the request workspace; without the opt-in the class is `null` with the
reason "<harness> cost class depends on its authentication, which was not probed"), reports
`cost` with its `reason`, and reports `consent` as `{opted_in, applied, scope}`. When the request
opted in and the effective policy does not allow the class, it refuses with `error.kind` `consent`
before any workspace scan, argv or runner call, so a refused call spawns nothing; the message names
the scope and the `consent set` command that would allow the class.

A protocol 2 request that sends no `consent` field does not opt in: it is never refused for its cost
class and never reads the consent file. Only an opted-in request is checked.

## `llm_kit.cli`, launchers and bootstrap

The console script is `llm-kit = llm_kit.cli:main`, with the groups `account`, `completion`,
`models`, `jobs`, `pipeline`, `frontdoor`, `local` and `consent`. `llm_kit.cli.LEGACY_VERBS` maps each
verb the consolidated commands carried to its group; a bare old verb prints the migration
hint. Each carried verb keeps its arguments, output and exit codes; the exit-code tables
differ by group because the commands differed (the scripting groups use 2 usage, 3 halt, 4
protocol, 5 indeterminate; `jobs` uses 3 for a runner failure and 4 for a waiting job), and
each group's module documents its own.

- **Launchers.** `bin/llm-kit` and `bin/llm-kit.cmd` are venv-first and delegate to
  `bin/llm_kit_entrypoint.py`, which puts nothing on `sys.path`: the library that runs is
  the installed package in the chosen interpreter (an interpreter without the package fails
  at the import). Under a redirected `CLAUDE_BOOTSTRAP_DATA_ROOT` the redirected
  venv is the only candidate (a missing one exits 2, never the production venv), and under
  `CLAUDE_PLUGIN_TEST` a shim running from the plugin cache refuses.
  `tests/llm-kit/test_launchers.py` covers both platforms.
- **`custom_bootstrap.py`** checks the OpenRouter credential at session start and nothing else.
  It runs inside the engine process and imports no `llm_kit` module (an AST test pins this):
  it launches `llm-kit account status --json --endpoint openrouter` under the plugin venv's
  interpreter and reads the document's `state` field (`missing_key`, `key_present`,
  `not_validated`, `ok`, `rejected_key`, `no_credit`, `check_failed`; schema
  `llm-kit.account-status/v1`), never prose or the exit code. A first call with
  `--no-validate` finds the key's hash and source; a second validates it against
  `GET /auth/key` only when the hash-and-age cache (`last_validated.sha256` in the plugin's own
  data directory) is stale. A missing, rejected (401) or out-of-credit (402) key becomes a
  deferred requirement, never a fix-all entry. A subprocess that cannot run, exits nonzero,
  prints no document or names an unknown state raises, and the engine reports the script as
  FAILED. It writes no credential or config file and migrates nothing.
  `tests/llm-kit/test_custom_bootstrap.py` covers the script and
  [the library account-status test](https://github.com/kitaekatt/katsoft-libs/blob/dev/tests/llm-kit/test_account_status_json.py)
  covers the document.
- **Consumer contract.** `contract/public-surface.json` lists the public names of the pipeline
  products and `scripts/check_consumer_contract.py` reports deprecated and removed names in a
  consumer's source (invocation: `README.md`).

## Plugin opinions: seam, default, or razor verdict

Each hardcoded opinion either has a named seam and default, or a recorded verdict (the scenario a
user would need and why none does). The verdicts are design reasoning, not measurements. The
adaptive-gate and job-backpressure tables above are part of this register.

| Opinion | Seam and default, or razor verdict |
|---|---|
| JSON repair `max_edits` / `max_walks` | Seam: parameters of `repair_json_structure` (and `repair_and_evaluate_output`); defaults `MAX_REPAIR_EDITS=24`, `MAX_REPAIR_WALKS=1500`. `finalize_contract` and the CLI do not forward them: a caller that needs other bounds calls the library function. |
| `_MAX_CLOSER_RUN=8`, `_MAX_REF_DEPTH=16`, `_MISQUOTED_KEY_LOOKBACK=2` | Razor verdict: no seam. Scenarios considered: a run of more than 8 stray closers, a schema `$ref` chain deeper than 16, a misquoted key more than 2 tokens behind the failure. Each is outside the one-slip repair the module promises (a run that long is a different document, and the repair applies only when exactly one fit exists), and a deeper `$ref` chain is a schema fault the caller fixes. Raising them widens the guess space and the ambiguity risk without helping a slip. |
| Claude text-only flags (`_CLAUDE_TEXT_ONLY_ARGS`) and codex text-only config and catalog sets (`_CODEX_TEXT_ONLY_CONFIG`, `_CODEX_TEXT_ONLY_CATALOG_SET`, `_CODEX_TEXT_ONLY_CATALOG_DROP`) | Razor verdict: no seam. They ARE the guarantee the mode advertises (no filesystem write, shell or subagent); a user-editable set would let the advertisement and the argv disagree. Scenarios considered: a user wanting one tool kept (then they do not want text-only mode; use a mode that allows it), a new codex tool source (a plugin change, re-measured per "A guarantees requirement admits AND arms"). |
| Effort `_MENUS`, `CODEX_EFFORT_ORDER`, `CLAUDE_EFFORT_ORDER`, and the measured `model_profiles.PROFILES` | Razor verdict: no seam. The tables mirror what each wire style accepts (ninfer rejects `high` with a 400). A user-set menu could only disagree with the server; a server with a different menu declares a different `effort_style` in the registry, which is the existing seam. The harness orders mirror the values each CLI accepts (`codex exec -c model_reasoning_effort`, `claude --effort`) and are the fallback for a model without a profile. The exact-model profiles are measurements with a recorded source and date; a project may narrow a menu in its model file (`efforts`) and never widen it, and a CLI or vendor that changes a menu is a library change (a new measured profile). |
