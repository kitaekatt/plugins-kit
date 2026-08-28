# Reusable library capabilities

Use this index before building project-local machinery. It covers the Python packages that plugins publish through a `bootstrap.json` `shared_libs` declaration. It does not catalog plugin commands, hooks, one-off scripts, or internal algorithms.

To consume one from a provisioned plugin environment, declare its package name in that plugin's `shared_lib_imports`. A project outside the plugin bootstrap environment must arrange its own import path and dependencies.

## `bootstrap_lib`

**Capability:** Harness-neutral, cross-platform machine and project state-management primitives: inspect and repair tools, paths, virtual environments, Git dependencies and configuration, JSON/INI/YAML configuration, downloads, plugin/marketplace state, and managed environment resources such as symlinks, shell entries, macOS defaults, hotkeys, and login items. It also provides bootstrap-state guards and Codex CLI discovery/argument construction.

**Use when:** A project bootstrap CLI needs safe, reusable inspect, install, update/converge, or repair operations, or a plugin needs to test whether provisioning completed. Prefer composing these primitives over creating a one-off installer or updater. Codex-facing tools must expose them through Codex-legal CLI and permission semantics; OpenCode and Claude Code integrations should consume the same core through adapters. Claude SessionStart hooks are one consumer, not the library boundary.

**Platform direction:** The primary goal is a portable state-management platform exposed through a harness-compatible CLI. Codex is the first integration; OpenCode and Claude Code follow as harness layers over the same neutral core. The `AGENTS.md` symlink is an initial proving resource, not a special-purpose architecture.

The governing architecture is **capability = harness-independent core + harness layer**: the core owns capability/state semantics and operations; each harness layer translates them into that harness's invocation, permissions, lifecycle integration, and presentation without forking or redefining the capability.

Harness layers may add optional native ergonomics, automation, richer presentation, and lifecycle integration. Essential behavior remains complete in the core; an enhancement cannot become an implicit prerequisite or create harness-dependent core semantics.

**Entry points:** `bootstrap_lib.managed_state` is the explicit public surface for project declarations, `check` / `install` / `update` planning and reporting, CLI execution, and managed resources such as `Symlink`. The `bootstrap_lib` package root does not declare a public export list for its older focused modules; imports such as `bootstrap_lib.bootstrap_guard`, `bootstrap_lib.env_features`, `bootstrap_lib.json_check`, `bootstrap_lib.ini_check`, `bootstrap_lib.downloader`, or `bootstrap_lib.codex` remain module-level contracts. Treat other modules as internal unless their module documentation or an existing consumer establishes a contract.

**More:** [`plugins/bootstrap/README.md`](../../plugins/bootstrap/README.md), [`plugins/bootstrap/bootstrap_lib/`](../../plugins/bootstrap/bootstrap_lib/), and the bootstrap skill's [`engine-internals.md`](../../plugins/bootstrap/skills/bootstrap/references/engine-internals.md).

## `llm_scripting_kit`

**Capability:** Resolve named models and endpoints, API keys, and OpenAI-compatible clients; probe endpoint/account health; invoke completion-shaped work across HTTP, Claude, Codex, and OpenCode backends; classify persistent halt conditions; and adapt configured harness endpoints.

**Use when:** A script or pipeline needs model selection and invocation without binding its own configuration, credentials, or transport layer.

**Entry points:** The supported root exports are declared in `llm_scripting_kit.__all__`, including `get_api_key`, `make_openai_client`, `resolve_endpoint`, `resolve_model`, registry resolution, harness adapters, backend types, and halt classification. `llm_scripting_kit.completion` declares the completion seam and backend factory in its own `__all__`.

**More:** [`plugins/llm-scripting-kit/README.md`](../../plugins/llm-scripting-kit/README.md), [`llm_scripting_kit/__init__.py`](../../plugins/llm-scripting-kit/lib/llm_scripting_kit/__init__.py), and [`completion/__init__.py`](../../plugins/llm-scripting-kit/lib/llm_scripting_kit/completion/__init__.py).

## `content_pipeline`

**Capability:** Project-independent building blocks for LLM-in-the-loop batch content systems: attributed stores, content-hash freshness, validation, context providers, model calls and accounting, pipeline stages, durable execution state, delivery, audit, human review round trips, CLI scaffolding, and a VCS seam.

**Use when:** A project processes many authored units through generation, validation, review, and delivery and needs reusable seams rather than a monolithic project-specific pipeline.

**Entry points:** The package root intentionally re-exports nothing. Import from the relevant subpackage, such as `content_pipeline.store`, `freshness`, `validate`, `providers`, `llm`, `pipeline`, `execution`, `deliver`, `audit`, `roundtrip`, `cli`, or `vcs`. Each subpackage `__init__.py` identifies its advertised surface and dependency boundary; use direct module imports where that file specifies them.

**More:** [`content_pipeline/__init__.py`](../../plugins/content-pipeline-kit/lib/content_pipeline/__init__.py) and the subpackage documentation under [`content_pipeline/`](../../plugins/content-pipeline-kit/lib/content_pipeline/).

## `p4kit_vcs`

**Capability:** A Perforce implementation of the `content_pipeline.vcs` backend seam, kept structurally compatible without importing `content_pipeline`.

**Use when:** A content-pipeline consumer needs Perforce changeset operations in place of the built-in Git or null backend, or another Python consumer needs the same narrow Perforce abstraction.

**Entry points:** `p4kit_vcs.__all__` declares `P4Vcs`, `P4Changeset`, `P4VcsError`, and `P4Runner`.

**More:** [`p4kit_vcs/__init__.py`](../../plugins/p4-kit/lib/p4kit_vcs/__init__.py) and [`p4_vcs.py`](../../plugins/p4-kit/lib/p4kit_vcs/p4_vcs.py).

## `skills_kit_lib`

**Capability:** Schema-driven validation and auditing for skill and instruction documents: fenced-YAML unit extraction, schema registration and validation, reusable rule fragments, Markdown shape heuristics, corpus discovery, bounded directory walking, and corpus-level checks.

**Use when:** Tooling needs to inspect, classify, validate, or audit `SKILL.md` and related typed Markdown without duplicating skills-kit's schemas and parsing rules.

**Entry points:** The package root performs schema registration but declares no public export list. Import from focused modules such as `skills_kit_lib.schema_engine`, `document_walker`, `schema_registry`, `markdown_heuristics`, `corpus`, `dirwalk`, or `checks`. The absence of `__all__` means public stability is not formally marked; prefer names already used by repository consumers and tests.

**More:** [`skills_kit_lib/__init__.py`](../../plugins/skills-kit/skills_kit_lib/__init__.py), [`plugins/skills-kit/README.md`](../../plugins/skills-kit/README.md), and [`skills_kit_lib/`](../../plugins/skills-kit/skills_kit_lib/).

## `yaml_data_editor_kit`

**Capability:** A project-independent YAML data dialect with schema/profile loading and corpus validation, plus anchored comments that can be resolved, checked for staleness, and re-anchored. The package also contains dispatch and editor layers, but those layers do not currently advertise a Python export surface.

**Use when:** A project needs typed YAML records and views, corpus diagnostics, or comments attached to stable semantic locations rather than raw line numbers.

**Entry points:** `yaml_data_editor_kit.schema.__all__` declares the profile, corpus, validation, merge, and diagnostic API. `yaml_data_editor_kit.comments.__all__` declares addressing, comment storage, staleness, and re-anchoring APIs. Treat `dispatch` and `editor` as non-public until they declare exports or consumer documentation.

**More:** [`schema/__init__.py`](../../plugins/yaml-data-editor-kit/lib/yaml_data_editor_kit/schema/__init__.py), [`comments/__init__.py`](../../plugins/yaml-data-editor-kit/lib/yaml_data_editor_kit/comments/__init__.py), and [`yaml_data_editor_kit/`](../../plugins/yaml-data-editor-kit/lib/yaml_data_editor_kit/).

## Boundary notes

- Inclusion here means the package is published as a bootstrap shared library, not that every module inside it is a stable public API.
- An explicit `__all__` or consumer-facing package documentation is the strongest public-surface signal in the current repository. Packages without either need a deliberate API decision before compatibility can be assumed.
- Development-only importable packages that are not declared in `shared_libs` are outside this index because bootstrap does not currently publish them for cross-plugin consumption.
