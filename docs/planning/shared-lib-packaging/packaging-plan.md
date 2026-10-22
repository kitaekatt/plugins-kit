# Shared library packaging: implementation plan

Status: plan approved with amendments by the Sol advisor (binding), with the Astra advisor concurring.
The release channel was ruled public PyPI; both distributions are released at 1.0.0 (`packages/released.json`). Owning task: `shared-lib-packaging`.
Predecessor: `recommendation.md` (timing NO-GO, superseded by the user's ruling below). Every
measurement cited is a scratch measurement, 2026-10-22 (uv 0.12.5, CPython 3.12.9, macOS arm64); no
tracked plugin, version, tag or consumer was changed to take it.

## Goal and end state

The user's rulings, verbatim:

> but i want databench in its own .venv the context is i want to make databench installable via npm and i don't want people to have to install bootstrap or plugins-kit to run databench

> there is another use case to note. Today there is an unreal project called databench that uses llm-kit to run two pipelines. However, not everyone installs llm-kit, so not everyone can use the pipeline. We will convert spiritcrossing to use the published library to remove that dependency so everyone can use these pipelines.

> i'll note my assumption has been that once we have these libraries separated we will simplify our plugins to install and work off of these libraries as well. This is not a priority but assuming the libraries are properly packaged and installable, it make sense to make this repo a consumer of them as well.

End state: `llm-kit` and a small contracts package are real, versioned, installable distributions.
External consumers (databench, spiritcrossing) install them into their own `.venv` with no bootstrap
and no plugins-kit. This repo's plugin venvs later install the same packages, which replaces the
`shared_libs` `.pth` mechanism instead of living beside it. That plugin migration is not a priority.

## Consumers

| Consumer | Install target | Reaches |
|---|---|---|
| databench | its own `.venv`, shipped by npm | five in-process `llm_kit` modules (scheduler, deadlines, failure_cache, gate, atomic_json); the CLI verbs `completion complete\|request-schema\|repair-json`, `models endpoints\|describe\|resolve`, `jobs run\|resume\|--version`; `bootstrap_lib.execution_event` |
| spiritcrossing | its project venv (host-side Python) | the `llm_kit.pipeline` library API, `llm_kit.adapters.claude_runner`, `llm_kit.cli.run.build_commands`, `llm_kit.scaffold.dispatch` |
| this repo's plugins | their plugin venvs | the same packages, after the migration step |

## Clean-machine closure

Measured with `bootstrap_lib` blocked and `HOME` pointed at a nonexistent directory (databench), and
with a wheel of llm-kit 0.4.0 in a fresh 3.12 venv under the venv interpreter in isolated mode (`-I`) (spiritcrossing surface).

| Reach | Result | Cause |
|---|---|---|
| databench in-process modules (5) | clean | stdlib only |
| `models endpoints`, `completion request-schema`, `jobs --version` | clean | none |
| `completion complete`, `models describe` | HARD FAIL | `registry/declaration_syntax.py:18-52` and `dispatch/declaration.py:80,179-201` import `bootstrap_lib.model_declaration` |
| `jobs run`, `jobs resume` | HARD FAIL | `jobs/interrupts.py:162-185` imports `bootstrap_lib.interrupt_contract`; `jobs/events.py:87-215` imports `bootstrap_lib.execution_event`; `jobs/model.py:552-580` imports `model_declaration` |
| databench's own `index_pages_runner.py:161` | HARD FAIL | imports `bootstrap_lib.execution_event` directly |
| `registry/models.py:262-304` | soft | `config_resolve` and `manifest_merge` degrade to the shipped defaults, with a stderr line |
| model registry file absent | soft | 10 shipped entries; override with `MODEL_ENDPOINTS_REGISTRY` |
| API key | clean | plain `OPENROUTER_API_KEY` env var has top precedence |
| CLI re-exec (`cli/__init__.py:146-165`, `bootstrap_guard.py:140-215`) | wrong env | no-op without the plugin venv; with it, re-execs into the plugin's floating copy |
| spiritcrossing: `route(models=[...])`, `CONTENT_PIPELINE_LLM_MODELS`, `create-run --models` | HARD FAIL | `model_declaration` (`DeclarationSupportError`) |
| spiritcrossing: `execution.events.project_run`, `store.request_interrupt` | HARD FAIL, on demand | `execution_event`, `interrupt_contract` |
| spiritcrossing: pipeline, adapters, cli.run, scaffold, backends | clean | 162 of 163 modules import; the one failure is `dispatch.review_lane`, outside its surface |

The three failing `bootstrap_lib` modules (`execution_event`, `interrupt_contract`,
`model_declaration`) are stdlib-only, and the contracts distribution below closes them. Extras: base
llm-kit has no dependencies; `sdk` (openai) is needed for the OpenRouter HTTP path; `pipeline`
(pyyaml) is needed for `scaffold.dispatch` rendering; `jobs` and `config` also bring pyyaml.

## Architecture

### Release channel (ruled: public PyPI)

Distribution names. The bare name `llm-kit` is taken on PyPI by an unrelated project (HTTP 200); the
names below returned 404 (free) on 2026-10-22.

| Distribution | Import | Executable |
|---|---|---|
| `katsoft-llm-kit` | `llm_kit` | `llm-kit` |
| `katsoft-contracts` | `katsoft_contracts` | none |

Names on a public index are permanent once published, so reserving them is the first step.

- Option P (recommended by Sol): public PyPI. The existing plain `uv sync --project` in
  `plugins/bootstrap/bootstrap_lib/venv_check.py` (around line 406) needs no change, because
  bootstrap passes no index arguments to uv. Publish from GitHub Actions with Trusted Publishing, so
  no long-lived credential exists. Needs a PyPI account.
- Option G: a GitHub Pages PEP 503 simple index. Each plugin pyproject must then declare
  `[[tool.uv.index]]`, since bootstrap cannot supply one. Works for external consumers once they add
  the index line.
- Rejected as the normal channel: GitHub Release assets with `--find-links` (a wheelhouse for
  bundled or offline use, not a durable resolver index) and git-subdirectory SHA URLs (a plugin would
  pin SHAs of its own repo; end users need git; a bare name has nowhere to resolve). SHA URLs and
  local wheels stay valid for databench development only.

Offline: an index-only first sync needs the network. Offline installs need bundled wheels or a
preseeded uv cache.

### Contracts distribution

Three stdlib-only modules move into one distribution owned by bootstrap, source under
`plugins/bootstrap/packages/`: `execution_event`, `interrupt_contract`, `model_declaration`.

- One canonical implementation under the `katsoft_contracts` namespace. The
  `bootstrap_lib.<module>` files become shims that re-export the same objects. Frozen literals,
  messages and event bytes are preserved; tests cover the old import paths and module-qualified
  serialized identities.
- llm-kit declares `katsoft-contracts` by name with a version range. The contracts distribution
  has its own version and a package-version bump gate.
- Closure obligation. A shim does not carry its dependency into a consumer venv. Every environment
  that imports a shim must also install the contracts distribution. Importers: bootstrap, llm-kit,
  awesome-kit, workflow-kit, and the frozen legacy plugins. The activating release either adds
  `katsoft-contracts` to every affected plugin pyproject, or each consumer keeps its full
  modules until it migrates.
- Rejected: a generated copy inside `llm_kit` (a dead end given the end state) and moving the
  modules into `llm_kit` (bootstrap would depend on llm-kit).

### Re-exec

`cli/__init__.py:146-165` re-execs into a plugin venv whenever one exists. The replacement requires
positive plugin-launch context: only a launch from the plugin cache or plugin data directory
re-execs. An installed console script or the module entry `-m llm_kit.cli` stays in its invoking environment
and never discovers an unrelated plugin venv. The private opt-out `_BOOTSTRAP_GUARD_SKIP_REEXEC=1`
goes away.

### Transition and `.pth` revocation

The fleet keeps the `.pth` mechanism until a plugin migrates. A plugin venv must never hold both a
`.pth` copy and an installed copy of one package: the `.pth` is inserted at `sys.path[0]` and shadows
an installed wheel, and `plugins/bootstrap/bootstrap_lib/shared_lib.py:105` plus
`engine.py:7495` show bootstrap links declarations but never removes them. Removing a
`shared_lib_imports` entry therefore leaves the old `.pth` in place.

Before any migration: add targeted revocation of bootstrap-owned `.pth` files, or build and validate
a fresh venv and switch to it. Then restart the consuming process and verify both distribution
metadata and module origin (`__file__`) for each migrated package.

### Release gating

`scripts/publish.py` (header, line 7) moves plugin releases between branches; it neither builds
packages nor creates package release identities. Package publication must finish before master
exposes a plugin version that needs those artifacts. A coupled contracts and llm-kit release must
refuse `--only` unless the full dependency closure ships.

## Increment

Order is binding. Each step has a done-check; the task item id follows in parentheses.

1. Reserve names and build the release identity and channel (`release-identity-channel`). Done when
   the two names are claimed (or the Pages index exists), the owning workflow builds wheels, and
   `publish.py` refuses a partial release that splits contracts from llm-kit (a test fails if the
   guard is removed).
2. Local wheels and closure tests (`local-wheels-closure-tests`). Build the contracts and llm-kit
   wheels, add the shims and the positive-context re-exec. Done when a fresh venv from the built
   wheels, with a scratch `HOME`, `bootstrap_lib` blocked and a fake `OPENROUTER_API_KEY`, passes
   `models describe`, `completion complete`, `jobs run|resume`, the console script and
   the module entry `-m llm_kit.cli` (against a stub endpoint or dry path); metadata tests pass; reverting the
   `model_declaration` move turns the closure test red.
3. CLI goldens and deprecation (`cli-goldens-deprecation`). Golden fixtures for the verbs databench
   uses, beside `contract/public-surface.json`; the two-minor deprecation policy
   (`docs/reference/deprecation-policy.md`) applies to external consumers. Done before the first
   external release; a verb change without a fixture update fails.
4. Publish immutable artifacts and verify (`publish-verify-artifacts`). Publish both distributions
   to the chosen channel. Done when a clean machine installs from the real channel (not a local
   path) and the step-2 closure test passes against it.
5. Activate the plugin side (`activate-plugin-deps`). Only after step 4. Ship the plugin
   pyproject dependency on `katsoft-contracts` for every shim importer, `.pth` revocation, and
   the shims. Done when each plugin venv imports the shims with contracts installed and module
   origin is the installed distribution.
6. Revise Mode 4 (`mode4-doc-revision`). Update
   `plugins/bootstrap/skills/bootstrap/references/library-consumption.md` (Mode 4, lines 274-302):
   llm-kit and contracts are package-ready via the channel; other shared libs stay unsupported
   off-fleet until packaged; the vendoring prohibition is reconciled with the end state.
7. Hand off to databench (`databench-migration-handoff`). Done when the handoff below is delivered
   and databench confirms its clean-machine run.

## Databench handoff

Released on public PyPI at 1.0.0 (`packages/released.json`): `katsoft-contracts` (import
`katsoft_contracts`) and `katsoft-llm-kit` (import `llm_kit`).

- One `.venv` containing databench, `katsoft-llm-kit[sdk,jobs,config]==1.0.0` and
  `katsoft-contracts==1.0.0` from PyPI; launch llm-kit from that venv's interpreter. Extras defined by
  the distribution: `config`, `sdk`, `local`, `frontdoor`, `jobs`, `pipeline`.
- Replace `bootstrap_lib.execution_event` with `katsoft_contracts.execution_event`; read the llm-kit
  version through `importlib.metadata`, not `~/.claude/plugins/installed_plugins.json`
  (`generate_config.py:157-165`).
- Pass the key through `OPENROUTER_API_KEY`. Package-mode llm-kit with no model registry uses the
  shipped default model list (`llm_kit/registry/models.py`, `package_mode_model_config`); setting
  `MODEL_ENDPOINTS_REGISTRY` to a registry file is optional and overrides it.
- Remove `shared_lib_imports` from `.claude/bootstrap.json`, delete the stale `.pth`, and rewrite the
  "claude plugin install" messages.
- CLI behaviors to handle (goldens under `tests/llm-kit/goldens/cli/`):
  - `llm-kit models describe` with no usable target writes its error JSON to stderr and leaves stdout
    empty (`models_describe_mismatch.json`).
  - The `completion complete` envelope key is `endpoint`, not `entry` (`complete_ok.json`).
  - `--request-file` combined with `--prompt` exits 4 (`complete_request_file_conflict.json`).
- The plugins-kit side verified the released wheels with
  `WHEEL_CLOSURE_SOURCE=pypi uv run --extra dev pytest tests/packaging` (35 passed, 2026-10-22).
  Databench's acceptance is its own clean-machine run of its verbs in a fresh `.venv` with no bootstrap
  or plugins-kit.
- The npm design must still settle Python 3.12 provisioning, platform support, upgrade and uninstall
  behavior, and whether it downloads or bundles the wheels.

## Spiritcrossing

Its stated use works on a clean machine, with one exception: declaration-based routing
(`route(models=...)`, `CONTENT_PIPELINE_LLM_MODELS`, `create-run --models`) fails until the contracts
distribution is installed. Backends `openrouter` and `claude-cli` work when instantiated directly or
through `route()` with no declaration. Codex is unused.

Needed extras: `pipeline` (pyyaml, for `scaffold.dispatch`) and `sdk` (openai, for the OpenRouter
HTTP path). The jobs commands it mounts are `llm_kit.cli.run.build_commands` (the execution-ledger
set), not `llm_kit.jobs`. `llm_kit.scaffold.dispatch` is a function in `llm_kit/scaffold/__init__.py`;
there is no module of that name.

Public-surface gaps in `contract/public-surface.json` (modules without `__all__` make
`test_every_exported_name_is_listed` blind to them): `core.validate.*` (contract, floor_guard,
riders), `artifacts.freshness.*`, `artifacts.store.*` (attributed, intermediary),
`execution.events`, `adapters.claude_runner` (`llm_kit.adapters` is not in `covers`), and
`runtime.gate` (2 of 11 names listed). These need `__all__` and listing before spiritcrossing can
rely on a stability promise.

Remaining unknowns: spiritcrossing's Python version against llm-kit's `>=3.12`, target operating
systems, and whether it routes by declaration. Its source is not on this machine; the facts above
come from the user's answers and the surface check. Prefer a host-side managed Python process unless
Unreal's embedded interpreter is proven compatible.

## Advisor record

- Sol (binding): approve with amendments. Changes to the proposed plan: public PyPI over a
  self-hosted index, unique distribution names, the shim closure obligation, `.pth` revocation before
  migration, positive-context re-exec, publication before plugin activation, `--only` refusal, a
  single databench `.venv`, and spiritcrossing discovery. Step 4 is needed before databench's
  released state, though databench development may start on local wheels or SHA URLs.
- Astra (consulted by Sol, concurred): independently returned amend on closure, stale `.pth`,
  release gating and the single databench venv. Fable was not warranted.

## Deferred

- Migrating this repo's plugins from `.pth` to installed packages, one at a time (not a priority).
- An in-process Python API for pure calls in databench, in place of subprocess calls.
- Spiritcrossing migration, after its entry points and Python host are confirmed.
- Packaging other shared libs for off-fleet use.
- The frozen legacy plugins (`content-pipeline-kit`, `llm-scripting-kit`, `job-kit`): deleting them
  remains the user's call.
