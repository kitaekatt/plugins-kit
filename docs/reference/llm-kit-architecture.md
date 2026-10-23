# llm-kit Python package architecture

Status: signed off by the user 2026-10-16 (all 14 decisions).

## Decision summary

- Create one published plugin, llm-kit, with one release version and one library, llm_kit.
- Consolidate under the final namespace privately, prove differential parity, then restructure one boundary at a time before consumer migration.
- Use 16 import components with the exact acyclic edge set below; namespace initializers re-export nothing.
- Keep completion contracts and capability types below concrete adapters, capability tables, factories, and account probes.
- Route pipeline backend construction through dispatch while preserving every constructor option and injection seam.
- Keep flat jobs and leased pipeline execution separate above shared execution and storage mechanics.
- Preserve job schema 12 (as the common prefix; llm-kit's job ledger is schema 13, see "Job ledger schema 13"), pipeline schema 10, spend-ledger schema 1, DDL, pragmas, and event bytes during coexistence.
- Preserve job-kit's unsupported, undetected one-process-per-ledger stance; preserve pipeline leases and fencing; retain the verdict file's last-writer-wins semantics.
- Publish only llm_kit; keep the old plugins published, installable, and feature-frozen for at least two llm-kit minor versions.
- Migrate qualified skill, agent, workflow, launch-flag, and resumable references before retiring any registration.
- Defer a generic migration runner until differential tests demonstrate a common algorithm worth extracting.
- Require Python 3.12 and raise the four lagging consumer declarations during migration.
- User sign-off on the numbered decisions is required before implementation.

## Ruling, scope, and repository rules

The controlling ruling from 2026-10-16 is:

> how we're going to do this is we're going to create a new plugin called llm-kit which will be the merged plugin. Once this plugin is stable we will migrate customers, freeze the other plugins for a while, and eventually delete them.

The design assumes that llm-kit, llm-scripting-kit, content-pipeline-kit, and job-kit coexist. llm-kit starts from copies; source is not moved out of the old plugins. The old plugins remain published and installable for a freeze of at least two llm-kit minor versions before deletion is eligible.

The merge is a narrow, user-approved exception to plugins/CLAUDE.md, section "Published-plugin boundaries", which otherwise forbids moving content between plugins or creating a unifying plugin. It must still honor that section's rule against shipping maintainer-only material. Renamed or removed names within llm-kit follow docs/reference/deprecation-policy.md; the old plugins instead follow the freeze defined here.

Implementation would propose this amendment to plugins/CLAUDE.md; this design does not edit that file:

> **Approved consolidation exception: llm-kit.** The user-approved llm-kit merge may copy and restructure behavior from llm-scripting-kit, content-pipeline-kit, and job-kit into llm-kit. The old plugins retain their source during migration and freeze. This exception does not generalize to other plugin-boundary cohesion work. Every renamed or removed public name follows docs/reference/deprecation-policy.md.

## Terms and package principles

The plugin is the REP release unit: one manifest, version, marketplace entry, and release cadence. An import package is the component used for CCP, CRP, ADP, SDP, and SAP. The root llm_kit and grouping package llm_kit.pipeline contain no behavior and are not components.

- REP: every component ships in one llm-kit release.
- CCP: code that changes for the same actor and reason belongs together.
- CRP: consumers do not import or initialize unrelated behavior.
- ADP: every allowed import points down the graph.
- SDP: volatile applications depend on stable contracts and utilities.
- SAP: stable packages expose abstractions at actual variation points.

One release prevents undeclared internal skew, but gives up TanStack's ability to version each npm package independently. SAP does not require an interface for frozen formats; schemas, DDL, and event bytes remain concrete and are protected by compatibility fixtures.

## Proposed source tree

~~~text
packages/katsoft-llm-kit/src/
  llm_kit/
    __init__.py
    schema/
    completion/
    registry/
    scaffold/
    adapters/
    dispatch/
    storage/
    execution/
    jobs/
    pipeline/
      __init__.py
      core/
      artifacts/
      runtime/
      execution/
      analysis/
    frontdoor/
    cli/
~~~

Public symbols are exported from their owning component, never the root.

## Package boundaries

| Package | Responsibility | CCP reason to change | CRP reuse without the rest |
|---|---|---|---|
| llm_kit.schema | JSON Schema subset, validation, errors, deterministic repair. | The supported subset or repair rules change. | Bootstrap interrupt validation and both execution products need no transports. |
| llm_kit.registry | Endpoint/model declarations, effort vocabulary, conserve metadata, credential paths and precedence, config resolution. No HTTP. | Configuration, endpoint identity, credential precedence, or declaration syntax changes. | awesome-kit, git-kit, p4-kit, workflow-kit, jobs, runtime, and frontdoor resolve models without pipeline products. |
| llm_kit.storage | SQLite connection/transaction posture, WAL/network handling, atomic JSON, and schema primitives. Product DDL, migration loops, and product ownership policy stay out. | Safe local persistence and transaction mechanics change. | Jobs, pipeline execution, and spend ledger reuse mechanics without sharing schemas. |
| llm_kit.scaffold | Command dispatch, scope filtering, suggestions, YAML rendering, stable exit codes. | Reusable command-composition semantics change. | Pipeline authors use it without llm-kit commands. |
| llm_kit.completion | Request/result/usage/halt/capability/requirement/output-contract types plus LLMBackend, BackendFactory, and CapabilityProvider protocols. No filled adapter tables or factories. | The meaning of a completion call or provider-neutral contract changes. | Completion-only consumers type calls without config, transports, jobs, or pipelines. |
| llm_kit.execution | Bootstrap event/interrupt adapters, shared schema-validator loading, common request/resolution values, lapse delegation, bounded-pool control. | Bootstrap contract shape or common bounded-execution semantics change. | Jobs and pipeline execution reuse it without sharing state machines. |
| llm_kit.adapters | OpenAI-compatible, Claude, Codex, OpenCode, harness, swapper, process, usage-source and account-probe integrations; endpoint profiles and adapter capability tables. | An SDK, CLI, wire request, filesystem source, process protocol, account endpoint, or endpoint specialization changes. | Dispatch and frontdoor reuse integrations; completion-only consumers import none. |
| llm_kit.dispatch | Declaration/run policy, configured backend factory, reachability, seats, pacing cache, observer, review-lane service, and policy-facing re-exports. | Selection, construction, reachability, pacing, or invocation policy changes. | Measured direct LLM consumers need no job or pipeline product. |
| llm_kit.jobs | Flat jobs, schema-13 ledger (schema 12 plus cancellation) and migration loop, selection, runner, workers, acceptance, workspaces, events, interrupts. | Flat-job, workspace, acceptance, or job-ledger policy changes. | skills-kit's emitter needs jobs without leased pipeline execution. |
| llm_kit.pipeline.core | Stages, work units, gates, convergence, seeding, validator/rejection contracts, extension ports. | Pipeline unit, stage, validation, or port meaning changes. | Runtime, artifacts, execution, and analysis reuse it. |
| llm_kit.pipeline.artifacts | Stores, unsupported registry, freshness, delivery, round trip, provider assembly, Git/null adapters. | Artifact identity, precedence, freshness, correction, or delivery changes. | Runtime, pipeline execution, and analysis reuse it. |
| llm_kit.pipeline.runtime | Factory binding, retry/backpressure/cache/cost/budget/validate loop, spend ledger, YAML extraction. | Pipeline call economics, retry, cache, validation, or spend policy changes. | Inline callers and yaml-data-editor-kit need no leased reporting or flat jobs. |
| llm_kit.pipeline.execution | Schema-10 migration loop, unit store, claims, fencing, leases, controller, waves, workers, status, drivers. | Worker, lease, claim, wave, or durable-unit policy changes. | yaml-data-editor-kit consumes it without flat jobs. |
| llm_kit.pipeline.analysis | Audit reports, reasoning chains, provenance, call audits, snapshots, replay, impact-per-cost views. | Traceability, replay, audit, or reporting requirements change. | The reporting CLI needs no worker controller. |
| llm_kit.frontdoor | FastAPI routes, translation, group routing, server lifecycle, swapper acceptance. | HTTP compatibility or server lifecycle changes. | Other paths avoid FastAPI, Uvicorn, and httpx. |
| llm_kit.cli | argv parsing, subcommand routing, formatting, bootstrap guard, lazy composition. | User-facing syntax or application composition changes. | Executable entry points only. |

## Execution-overlap ruling

The measured execution-overlap recommendation is adopted with a split shared kernel. Flat jobs and leased pipeline execution keep different state machines, schemas, users, and acceptance rules. A job runner owns a call; a pipeline worker claims a unit under a lease and fencing token. Combining them would violate CRP and weaken the pipeline concurrency contract.

Shared bootstrap execution contracts live in execution; SQLite and ownership mechanics live in storage. Product interrupt records, transitions, schemas, and migration loops remain product-owned. A generic migration runner is not in the first contract. Differential parity tests must first show that the legacy runners share an algorithm rather than only similar syntax.

## Exact dependency graph

A -> {B} is the complete inter-component import set. Same-package imports are unrestricted; an omitted edge is forbidden.

~~~text
Layer 6  cli                -> {schema, completion, registry, adapters, dispatch, jobs,
                                pipeline.runtime, pipeline.execution, pipeline.analysis,
                                frontdoor, scaffold}
Layer 5  pipeline.execution -> {execution, storage, pipeline.core,
                                pipeline.artifacts, pipeline.runtime}
Layer 4  jobs               -> {completion, registry, dispatch, execution, storage}
         pipeline.runtime   -> {completion, registry, dispatch, storage,
                                pipeline.core, pipeline.artifacts}
Layer 3  dispatch           -> {completion, registry, adapters}
         frontdoor          -> {completion, registry, adapters}
Layer 2  adapters           -> {completion, registry}
         pipeline.analysis  -> {pipeline.core, pipeline.artifacts}
Layer 1  completion         -> {schema}
         execution          -> {schema}
         pipeline.artifacts -> {pipeline.core}
Layer 0  schema             -> {}
         registry           -> {}
         storage            -> {}
         pipeline.core      -> {}
         scaffold           -> {}
~~~

Deployment-mode support sits outside the 16 components, like `bootstrap_guard`: `llm_kit._runtime` (`mode()`, the routed-contract `load()`, `fleet()`, `require_fleet()`) may be imported by any component, imports no llm_kit module except `llm_kit._deployment`, and `llm_kit._deployment` (the marker the `katsoft-llm-kit` wheel replaces) is imported by `_runtime` alone. The guard `tests/llm-kit/test_import_graph.py` enforces all three. The package deployment, its contracts routing and the features it refuses are in `docs/planning/shared-lib-packaging/implementation-spec.md`, section C.

### Required moves and composition seams

| Legacy source | Destination or rule | Dependency result |
|---|---|---|
| usage_budget Budget, STATUS_*, ConserveSpec, POOL_*, and conserve parsing | registry.conserve | Adapter quota probes depend only on registry values; registry stays below dispatch. |
| declaration._model_declaration and declaration probe | registry.declaration_syntax | Removes lazy registry -> dispatch. |
| Static effort-order tables | registry.effort | Removes lazy registry -> adapters. |
| Raw Claude/Codex usage readers | adapters.usage_sources | Adapters do not import pacing policy. |
| Pacing, session pinning, verdict writes | dispatch.usage_budget | Policy stays above sources. |
| completion.adapter_capabilities filled records | adapters.capabilities | Capability types stay in completion; tables change with adapter emission. |
| account HTTP, decoding, status translation, endpoint probes | adapters.account | Registry keeps credential/config contracts and performs no transport I/O. |
| claude_runner.AgentTimeoutError | completion.halt | Removes completion -> adapters. |
| freshness.seed | pipeline.core.seed | Removes pipeline.core -> artifacts. |
| completion.endpoint_profile | adapters.endpoint_profile | Concrete backends use profiles without adapters -> dispatch; dispatch may re-export them. |
| job_kit and content_pipeline _schema_validator loaders | execution.schema_validator | Both products reach the shared validator through their existing execution edge. |
| content_pipeline.llm.backends concrete construction and endpoint probe | dispatch.pipeline_backends and dispatch.reachability | Runtime wrappers call dispatch, never adapters. |

pipeline.runtime.backends keeps the public pipeline wrappers and every constructor option: endpoint, project root, client, timeout, retry, diagnostics, executable, argv prefix, runner, and model endpoint. Each wrapper passes an immutable construction spec to an injected completion.BackendFactory; the default is dispatch.ConfiguredBackendFactory. ModelEndpointBackend.probe calls dispatch reachability rather than adapters.account. Dispatch maps names to concrete adapter classes. CLI is the composition root and injects the configured factory into jobs, pipeline runtime, and frontdoor. Direct library users either inject the protocol or call the public dispatch builder.

Dispatch requires a new construction builder, not a rename of llm_scripting_kit.completion.factory.create_backend. That factory accepts only endpoint, model, cheap, and project root, then constructs default harness instances. The new builder accepts the full construction spec while preserving content_pipeline's lazy delegate construction. An omitted runner remains omitted from the concrete constructor rather than being passed as None, so each adapter retains its default runner semantics.

The old llm_scripting_kit.completion facade is split, not copied. Neutral result, option, halt, requirement, output-contract, skill-context, and capability TYPE exports move to llm_kit.completion. The other exports move as follows:

- ADAPTER_CAPABILITIES, adapter_capabilities, OpenRouterBackend, ClaudeCliBackend, CodexCliBackend, OpencodeCliBackend, CodexRunError, OpencodeRunError, run_cli_streaming, run_claude_streaming, looks_like_hard_stop, compose_prompt, and their transport constants -> llm_kit.adapters.
- EndpointProfile and endpoint-profile functions -> llm_kit.adapters; llm_kit.dispatch may re-export them for policy callers.
- BackendSelection, create_backend, and create_transport_backend -> llm_kit.dispatch.
- EffortDelivery, EffortPlan, and plan_effort -> llm_kit.registry.effort.

AgentTimeoutError moves to llm_kit.completion.halt as a provider-neutral halt type; the concrete runner imports it from that lower package.

No llm_kit.completion initializer re-export may restore an adapters or dispatch edge. A selected adapter supplies its filled capability record through CapabilityProvider; consumers receive it through the injected factory. The account CLI resolves credentials through registry and injects them into adapters.account. These concrete composition paths, not protocol counts alone, are the SAP mechanism.

### Dependency principles

SDP holds because volatile applications point toward stable contracts and utilities. SAP is limited to actual variation points: backend factories, capability providers, execution ports, and pipeline extension ports. Stable concrete formats are protected by byte and schema fixtures rather than decorative interfaces. The exact edge set and import guard are the sign-off material.

## Cycles removed

The 2026-10-16 inventory found one eager subpackage cycle and five module cycles when lazy imports were included:

1. Root <-> completion: both new namespace initializers are empty.
2. effort -> harness_adapters -> model_endpoints -> effort: effort tables move to registry.
3. swapper <-> _swapper_process: the process primitive imports no service.
4. The ten-module completion/config/declaration cycle: neutral types, concrete tables/transports, registry syntax, and dispatch policy separate as above.
5. workerpack -> controller -> protocol -> workerpack: a private worker-types leaf holds shared values; composition moves to drivers.
6. job_kit.store <-> workspace: workspace calls store; store performs no workspace operations.

## Optional dependencies

The base shared-library path is standard-library only.

| Extra | Reachable package | Requirements |
|---|---|---|
| config | registry and dispatch config paths | pyyaml>=6.0 |
| sdk | OpenAI-compatible adapter | openai>=1.0.0 |
| local | local swapper/process adapter | psutil>=6.0 |
| frontdoor | frontdoor | fastapi>=0.110, uvicorn>=0.29, httpx>=0.27 |
| jobs | job YAML and CLI | pyyaml>=6.0 |
| pipeline | pipeline YAML and CLI | pyyaml>=6.0 |

The owner venv enables every extra its bundled CLI and skills reach. Importing llm_kit.completion costs only the source link and standard library. A dispatch consumer enables config; selecting the SDK adapter also enables sdk. Optional imports occur at the call site and a missing requested capability refuses. pyyaml and psutil are hard dependencies of llm-scripting-kit, so moving them to extras requires migration guards and refusal tests; it is not a free packaging change.

## Namespace and public-name migration

Option A, recommended: publish only llm_kit. Consumers change imports and shared_lib_imports once. Bootstrap recognizes both llm_kit and llm_scripting_kit during coexistence; distinct shared-library names avoid cache and .pth collisions.

Option B: llm-kit also publishes llm_scripting_kit, content_pipeline, and job_kit. This is rejected during coexistence because old plugins own those names and shared-library publication is keyed by package name.

Legacy names remain owned and implemented by their old plugins. Once llm-kit is stable, each old plugin receives only a version bump plus README and plugin-description pointers to llm-kit. The frozen release does not import or delegate to llm_kit, and old CLIs remain unchanged. Each old plugin stays published and installable for at least two llm-kit minor versions; during that window it takes no feature changes, only fixes needed to keep it working. llm-kit never publishes the old top-level packages.

*Amendment 2026-10-17 (user ruling):* stability is not a gate. The user ruled: "whether or not llm-kit is stable is absolutely irrelevant as i am the only user". The user is the only user and controls every machine, so "Once llm-kit is stable" above means "now": the pointer-only freezes and consumer migration proceed without a stability decision. Only deleting the old plugins remains the user's call.

| Old surface | New surface |
|---|---|
| Neutral llm_scripting_kit.completion exports | llm_kit.completion |
| Completion transports, runners, endpoint profiles, capability tables | llm_kit.adapters |
| Completion factory exports and policy-facing profile re-exports | llm_kit.dispatch |
| llm_scripting_kit.account network APIs | llm_kit.adapters.account |
| Model/config/credential resolution | llm_kit.registry |
| Declaration, seats, reachability, pacing | llm_kit.dispatch |
| job_kit.* | llm_kit.jobs.* facades |
| Content pipeline audit/provenance | llm_kit.pipeline.analysis.* |
| content_pipeline.cli.scaffold | llm_kit.scaffold |
| Content pipeline artifacts and VCS implementations | llm_kit.pipeline.artifacts.* |
| Content pipeline stages, validation, convergence, seed, VCS port | llm_kit.pipeline.core.* |
| Content pipeline LLM wrappers and spend policy | llm_kit.pipeline.runtime.* |
| Content pipeline backend construction | llm_kit.dispatch.pipeline_backends |
| content_pipeline.execution.* | llm_kit.pipeline.execution.* facades |

## Skills, agents, workflows, and CLIs

The final qualified names are llm-kit:openrouter-account, llm-kit:content-pipeline-domain, llm-kit:background-pipeline, llm-kit:execute-work-unit, llm-kit:workflow-pipeline, llm-kit:pipeline-worker, and llm-kit:run-ready-wave.

Legacy registrations are not converted to refusals. pipeline-worker can be selected explicitly through launch flags, and resumable workflows can persist qualified names. Before activation, implementation must migrate maintained qualified references, launch flags, and resumable records, or provide a tested old-name translator within llm-kit. Frozen registrations keep executing their own unchanged implementations; description wording is not a safety mechanism.

Activation requires all of these machine-checkable conditions:

1. Source and manifest scans find no unsupported old qualified reference.
2. Old-created resumable fixtures resume through both the old name and final llm-kit name.
3. Rollback tests disable llm-kit registration and resume through the old names.

Retirement requires a freeze spanning at least two llm-kit minor versions, zero maintained old references, and zero nonterminal persisted records that require an untranslated old identity. Rollback restores routing to the still-functional frozen registrations; it does not reverse persisted data.

The console script is llm-kit = llm_kit.cli:main, with account, completion, models, jobs, pipeline, frontdoor, and local groups. Old executable names and behavior remain unchanged in the frozen plugins. The new POSIX and .cmd launchers are venv-first and retain redirected-data-root and cached-copy refusals; launcher tests cover both platforms.

## Sequencing decision

Option A, recommended:

1. Consolidate behavior privately under the final llm_kit namespace.
2. Establish old-vs-llm-kit differential parity for API, CLI, events, schemas, state, and failure behavior.
3. Restructure one boundary at a time and enforce the final import graph.
4. Publish and stabilize llm-kit under the final public namespace.
5. Release version-bumped old plugins whose README and description point to llm-kit, with their code and CLIs otherwise unchanged.
6. Migrate consumers, skills, agents, workflows, launch flags, and resumable references once.
7. Keep each old plugin published, installable, and feature-frozen for at least two llm-kit minor versions; accept only fixes needed to keep it working.
8. Retire and delete old plugins only after the migration and persisted-record gates pass.

An llm-kit version may ship before customer migration, but it exposes no temporary public namespace and triggers no migration before the stability gate. A generic migration runner is considered only after differential parity demonstrates reusable behavior.

*Amendment 2026-10-17 (user ruling):* "stabilize" and "the stability gate" in this section are not gates. The user ruled: "whether or not llm-kit is stable is absolutely irrelevant as i am the only user". Steps 5-7 proceed without a stability decision; only step 8's deletion is the user's call.

Option B restructures the private llm_kit tree to the final package boundaries before establishing differential parity; option A establishes parity before restructuring those boundaries. Both options keep the same final public namespace, freeze, consumer migration, rollback checks, and retirement gates. Option B can reduce temporary internal adapters, but parity failures span consolidation and restructuring and are therefore harder to localize. Option B is not recommended.

## TanStack insights

| Insight | Decision and reason |
|---|---|
| Core plus adapters | Adopt: completion contracts sit below concrete adapters. |
| Durability as a layer | Adopt with two CCP packages: execution contracts and storage mechanics. |
| Uniform naming | Adopt: public components use llm_kit.*. |
| Independent versions | Reject: the plugin is one REP unit and cannot express internal install ranges. |
| Many installed packages | Reject: shared links would recreate cross-plugin skew probes. |

## Persisted-state compatibility

### Credentials, config, and verdict cache

llm-kit preserves all public environment-variable spelling and the historical configuration layers (the fleet file `~/.claude/config/llm-scripting-kit.yaml`, the user and project `config.yaml` files) until old-plugin deletion, and seeds no competing llm-kit configuration layer. CLI_COMMAND is the exception: it names the llm-kit venv and executable.

The three user-scoped files do not follow the legacy locations. They live under `$XDG_CONFIG_HOME/llm-kit/` (fallback `$HOME/.config/llm-kit/`), resolved on every call by `llm_kit.registry.user_paths`: `.env` (user keys; override `LLM_KIT_ENV_FILE`), `model-endpoints.yaml` (override `MODEL_ENDPOINTS_REGISTRY`) and `consent.json` (override `LLM_KIT_CONSENT_FILE`; see "Caller consent"), plus `<project>/.llm-kit/.env` per project. An override names one explicit file and replaces the location. The files the old plugins kept under `~/.claude` (`plugins/data/plugins-kit/llm-scripting-kit/.env`, `config/model-endpoints.yaml`, `plugins/data/plugins-kit/llm-kit/consent.json`) are read only when the neutral file does not exist and are never written; the first write on a machine that holds only a legacy file starts the neutral file from the legacy values. A read creates nothing, and the library opens no path under `~/.claude` for a session under an empty home.

LEGACY_LLM_DATA/usage-verdicts.json remains exactly the legacy single-session pacing cache. The legacy implementation reads one session's dictionary, rewrites the whole file, and uses atomic replacement without an interprocess lock. llm-kit retains those semantics during coexistence:

- Simultaneous writers are last-writer-wins, including writers from different sessions and writers updating different endpoints.
- Atomic replacement prevents torn JSON but does not prevent lost updates.
- A stale snapshot can overwrite an observed OUT_OF_QUOTA verdict with AVAILABLE. pinned_evaluate returns that pinned AVAILABLE verdict without recomputing it.
- While old and new writers coexist, that resurrection can repeatedly select an exhausted endpoint, cause additional failed calls, and defeat session pacing.
- The file is advisory pacing state, not authorization, accounting, reservation, or double-spend prevention.

Barrier-synchronized old/new process tests cover same-session and different-session writers. They assert parseable legacy-shaped output and include stale-AVAILABLE resurrection: one writer records exhaustion, a stale writer restores AVAILABLE, and a subsequent pinned read returns AVAILABLE without recomputation. The test also demonstrates that selection can retry the exhausted endpoint. Tests do not assert merged verdict retention. This cost already exists between legacy processes sharing a session key; the merge changes nothing.

### SQLite compatibility, ownership, and handover

llm-kit preserves job schema 12 as the prefix of its schema-13 job ledger, pipeline schema 10, spend-ledger schema 1, canonical JSON, interrupt documents, DDL, pragma order, and event bytes. Product migration loops stay in jobs and pipeline.execution; spend ledger keeps its one create schema, autocommit, and configurable synchronous value.

llm_kit.jobs is a copy of job-kit whose first twelve migration steps are byte-identical; step 13 is the one deliberate change ("Job ledger schema 13"). It retains job-kit's one-process-per-ledger operating rule without adding detection or enforcement: two active processes on one ledger remain unsupported and undetected, including when one is job-kit and one is llm-kit. Each write verb still uses its existing BEGIN IMMEDIATE transaction, but there is no process-lifetime OS lock, refusal signal, or new ownership protocol. Operational handover stops one process before starting the other. Legacy to llm-kit starts llm-kit against the legacy bytes, which it upgrades to schema 13. Rollback starts the unchanged frozen job-kit against a ledger llm-kit has never opened; against a ledger llm-kit has opened, frozen job-kit refuses ("Job ledger schema 13").

The pipeline execution store keeps its existing ownership model. Many short-lived worker processes use transactional unit claims with expiring leases and monotonically increasing fencing tokens. At most one dispatcher process holds the existing dispatcher lease and fence for a run; a different live holder is refused, expiry or release permits a higher fence, and stale dispatcher or unit fences are rejected. SQLite serializes the short writes. This permits separate pipeline runs and workers to proceed concurrently and adds no job-style ledger lock or new pipeline ownership policy. Old content-pipeline and llm-kit implementations must preserve these schema-10 lease, fence, and transaction semantics.

Compatibility tests preserve the existing concurrency stance and the persisted formats:

- Job-kit and llm-kit same-ledger tests document that concurrent writers are unsupported and undetected; they assert no new lock or refusal contract.
- Old and new pipeline dispatchers for one run: one live lease wins; release or expiry permits takeover with a higher fence.
- Pipeline workers: valid concurrent claims proceed, while stale unit and dispatcher fences refuse mutation.
- Distinct pipeline runs proceed concurrently under their own dispatcher leases.
- llm-kit-to-old rollback resumes new-created nonterminal pipeline records with identical events and terminal state. For job ledgers it is the pinned refusal in "Job ledger schema 13": frozen job-kit refuses a ledger llm-kit has opened.
- Fresh-create and every migration prefix compare sqlite_master, version rows, pragmas, isolation posture, events, interrupts, and canonical JSON.

No schema change is allowed during coexistence beyond the one recorded below. Any other change requires a separate dual-readable format, conversion, ownership, and rollback decision.

### Job ledger schema 13

Decision: the user ruled "(8) llm-kit (unpublished) may change its frozen formats for a per-job timeout and `jobs cancel`" (task-kit dashboard, Increment 3). The ruling is the authority for this one exception to the schema freeze; the user is the only user of every plugin.

- Owner: llm_kit.jobs (`llm_kit.jobs.store`, migration step 13). Frozen job-kit stays at schema 12 and is not changed.
- Format: the job state `cancelled` (terminal) and a fourth interrupt-resolution outcome `cancelled`. Everything else a schema-12 ledger holds is unchanged, and the execution-event schema is unchanged: a cancel writes the existing `result` (status `lost`), `job-kit:continuation-result` and `terminal` (`state: cancelled`, `reason`) events. No `interrupt` event closes a cancelled interrupt, because the event schema's interrupt phases (requested, resolved, rejected, expired) have no cancelled phase.
- Conversion: step 13 rebuilds `interrupt_resolutions` with `CHECK (outcome IN ('answered', 'rejected', 'expired', 'cancelled'))` because SQLite cannot alter a CHECK. It drops the three triggers that name the table, copies every row with its id, drops the old table, renames the new one, and recreates the three triggers byte for byte, all inside the migration's one transaction. A schema-12 ledger upgrades in place on the first verb that opens it through the store (`run`, `resume`, `resolve`, `cancel`, `events`, `gc`); `status` reads through the read-only ledger reader and never migrates.
- Deliberate old-reader refusal: the existing future-schema check (`database schema version 13 is newer than this ... supports (max 12)`) makes frozen job-kit, and any llm-kit older than the schema-13 release, refuse a schema-13 ledger loudly and before it writes anything. This is intended: an older reader cannot represent a `cancelled` job, so it must not open the ledger. A llm-kit-to-job-kit rollback of a job ledger is therefore the refusal, not a resume. The cost is that a ledger llm-kit has opened cannot be handed back to job-kit; the remedy is to keep using llm-kit.
- Ownership: unchanged. `jobs cancel` requires the run process to be stopped already; llm-kit adds no lease, lock or refusal signal to detect a live runner.
- Visible status changes: `counts` in the status payload gains a `cancelled` key, and a run with a cancelled job exits 1 from `run`/`resume` like any job that did not end accepted.
- Tests: `tests/llm-kit/test_jobs_cancelled_state.py` covers the state, the cancel from every non-terminal state, idempotence, the migration and the refusal; `test_jobs_parity.py` (`TestLedgerSchema`, `TestDeliberateDifferences`) pins the schema-12 prefix and the frozen-job-kit refusal; `test_cross_state_parity.py` (`TestJobLedgerRollbackRefusal`) pins the handover directions.

## Caller consent

User rulings (2026-10-23, verbatim):

> the library does not make any llm calls, the user of the library makes the llm calls, we don't need the user to consent to writing code that makes llm calls

> this principle should be durable

Operative rule: katsoft-* libraries enforce no consent gate on calls a caller's code makes. Cost-class classification may be reported as information. This section supersedes any statement that the library refuses a call without consent. Callers may opt into the library's optional consent layer; its design is docs/planning/shared-lib-packaging/consent-design.md.

## Python, bootstrap, marketplace, and guard changes

llm-kit requires Python 3.12. Migration raises requires-python in awesome-kit, workflow-kit, yaml-data-editor-kit, and skills-kit and updates their CI matrices.

Implementation also:

- Adds plugins/llm-kit and its generated marketplace entry.
- Ships llm_kit as the katsoft-llm-kit package, whose one source is packages/katsoft-llm-kit/src/llm_kit; the plugin holds no library source and publishes no shared lib. The plugin imports bootstrap_lib.
- Adds llm_kit to bootstrap first-party recognition while retaining llm_scripting_kit through coexistence.
- Keeps the old interrupt validator constant and adds an llm-kit-specific probe.
- Migrates consumer plugin declarations and shared imports only after parity.
- Keeps all old marketplace entries through their freeze windows.

An AST guard over packages/katsoft-llm-kit/src/llm_kit uses the exact adjacency set above. It counts eager, local, TYPE_CHECKING, and literal dynamic imports; forbids root re-exports and old-package imports; forbids jobs/execution cross-imports; keeps product DDL and migration loops product-owned; and reports both packages, import mode, and line. Implementation proves the guard by observing a forbidden import fail before removing it and observing the test pass.

## Decisions for sign-off

1. Use only llm_kit as the new top-level package. Recommendation: yes. Cost: consumers change imports once; benefit: deterministic coexistence.
2. Accept the corrected 16-package map, facade splits, factory injection, and exact edges. Recommendation: yes. Cost: code is reorganized rather than copied as three intact trees.
3. Keep jobs and leased pipeline execution separate above shared execution/storage. Recommendation: yes. Cost: two product models remain; benefit: neither receives the other's policy.
4. Choose sequencing option A (parity before restructuring) over option B (restructuring before parity), with identical freeze, migration, rollback, and retirement gates. Recommendation: A. Cost: more temporary internal adapters; benefit: parity and restructuring failures remain separable.
5. Copy job ledger behavior byte-for-byte and preserve its unsupported, undetected one-process rule; preserve pipeline dispatcher leases and unit fencing. Recommendation: yes. Cost: the existing multi-process hazard remains; benefit: the freeze adds no job-kit feature or false guarantee.
6. Retain the verdict cache's advisory last-writer-wins semantics rather than add locking during the merge. Recommendation: yes. Cost: the stale-AVAILABLE resurrection already exists between legacy processes sharing a session key; the merge does not change it.
7. Share legacy credential, config, and routing environment contracts until deletion. Recommendation: yes. Cost: llm-kit carries legacy logical names.
8. Replace migrated CLI use with one venv-first llm-kit command while frozen old commands remain unchanged. Recommendation: yes. Cost: scripts migrate.
9. Keep final skill short names under llm-kit, migrate explicit and resumable references first, and keep old registrations independently functional through the freeze. Recommendation: yes. Cost: duplicate registrations temporarily exist; benefit: either selection path succeeds.
10. Keep third-party dependencies in extras around a standard-library completion core. Recommendation: yes. Cost: consumers declare reached groups, and moving the existing pyyaml/psutil hard dependencies requires migration guards.
11. Require Python 3.12 and raise the four lagging declarations. Recommendation: yes. Cost: metadata, CI, and possible external 3.10/3.11 users.
12. Retain bootstrap's old validator identity and add llm-kit recognition. Recommendation: yes. Cost: dual-name knowledge during coexistence.
13. Release all components under one llm-kit version. Recommendation: yes. Cost: one component change releases the plugin; benefit: no internal skew.
14. Approve the narrow plugin-boundary exception and proposed plugins/CLAUDE.md amendment while retaining maintainer-content and deprecation rules. Recommendation: yes. Cost: one explicit repository-policy exception.

Sign-off accepts all recommendations or names decisions that must change. No implementation begins before that response.

## Appendix A: legacy module map

Prefixes: L is llm_scripting_kit, C is content_pipeline, and J is job_kit. An asterisk means a facade whose internals split by the rules above.

| Destination | Legacy modules |
|---|---|
| Empty namespaces | L:__init__; C:__init__ |
| schema | L:completion/json_repair, completion/json_schema |
| registry | L:api_key, constants, effort*, env_file, model_endpoints*, models*; slices of declaration*, harness_adapters*, usage_budget* |
| completion | Neutral slice of L:completion/__init__; completion/capabilities, contract, contract_types, halt*, prompt_fold, requirements, results, skill_context, skill_context_types, types; request_protocol; AgentTimeoutError slice of completion/claude_runner* |
| adapters | L:account, _swapper_process, client, completion/backends, completion/adapter_capabilities, completion/claude_runner*, completion/codex_backend*, completion/endpoint_profile, completion/opencode_backend, harness_adapters*, swapper, swapper_acceptance; raw-reader slice of usage_budget* |
| dispatch | L:completion/factory, declaration*, observer, reachability, review_lane, seats, usage_budget*; construction and probe slices of C:llm/backends* |
| frontdoor | L:frontdoor/__init__, frontdoor/__main__, frontdoor/acceptance, frontdoor/server |
| scaffold | C:cli/scaffold |
| storage | C:execution/_atomic_json; connection/transaction/lock slices of C:execution/store*, C:llm/spend_ledger*, and J:store*; no product migration loop |
| execution | Shared slices of C:execution/events*, interrupts*, model*, scheduler* and J:events*, interrupts*, model*, run*, including both interrupt _schema_validator loaders |
| jobs | J:__init__, events*, interrupts*, model*, run*, select, store*, workspace, including product DDL and migration loop |
| pipeline.core | C:freshness/seed, llm/convergence, pipeline/__init__, cell_policy, convergence_loop, gate, single_pass, stage, workunit, validate/__init__, validate/contract, validate/floor_guard, validate/riders, vcs/seam |
| pipeline.artifacts | C:cli/unsupported, deliver/*, freshness/* except seed, providers/*, roundtrip/*, store/*, vcs/__init__, vcs/git_vcs, vcs/null_vcs |
| pipeline.runtime | C:cli/budget, llm/__init__, runtime wrapper slice of llm/backends*, llm/gate, llm/platform, llm/spend_ledger*, llm/yaml_extract |
| pipeline.execution | C:execution/__init__, adapter, controller, deadlines, drivers/*, events*, failure_cache, interrupts*, model*, parallel_graph, protocol, scheduler*, status, store*, wave, workerpack, including product DDL and migration loop |
| pipeline.analysis | C:audit/__init__, audit/auditor, audit/reasoning_chain, audit/report, provenance/__init__, provenance/call_audit, provenance/record, provenance/replay, provenance/snapshot |
| cli | L:bootstrap_guard, cli, scripts/llm_scripting_kit_cli; C:cli/__init__, cli/run, scripts/check_consumer_contract; J:cli |

## Appendix B: evidence

Measured consumer separation:

- Jobs only: skills-kit emits job files and launches the job-kit CLI; no production plugin outside job-kit imports job_kit.
- Pipeline execution only: yaml-data-editor-kit imports content_pipeline execution, LLM, and work-unit modules and imports no job_kit module.
- Completion only: awesome-kit, git-kit, p4-kit, and workflow-kit import llm_scripting_kit without importing either execution product.
- No plugin consumer was found that needs both jobs and pipeline execution.

Measured coupling and change history:

- Package imports run one way: content_pipeline and job_kit depend on llm_scripting_kit; llm_scripting_kit imports neither, and jobs and pipeline have no edge between them.
- content_pipeline reaches llm_scripting_kit through nine lazy module pairs. job_kit imports it eagerly from its initializer, run, and select paths; only its interrupt probe is lazy.
- Across the commit window, pair co-change counts were llm-scripting-kit/content-pipeline-kit 26, llm-scripting-kit/job-kit 18, and content-pipeline-kit/job-kit 8.
- Execution history contained 42 job_kit commits, 53 content-pipeline execution commits, and 8 touching both; those eight were cross-cutting sweeps. Same-named event, interrupt, model, and store modules did not co-change in the window.

Scope and limits:

- The inventory analyzed 133 Python modules and the last 300 commits touching the three plugin trees: 296 matching commits from 2026-07-23 through 2026-10-16.
- Its AST inventory included eager, function-local lazy, TYPE_CHECKING, and literal dynamic imports. String probes and references were separate scans.
- The execution-overlap consumer search covered plugins/ only. It did not search external projects, so consumer separation is repository evidence, not an ecosystem census.
- The coupling and history counts above are measurements. The exact dependency graph remains an implementation constraint enforced by the import guard.
