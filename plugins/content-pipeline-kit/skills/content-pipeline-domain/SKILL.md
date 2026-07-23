---
_schema_version: 1
name: content-pipeline-domain
author: christina
skill-type: domain-skill
description: Use when designing, building, or porting an LLM-in-the-loop batch content pipeline. Do NOT use for one-off LLM calls or non-batch chat interactions.
---

# Content Pipeline Domain

An LLM-in-the-loop batch content pipeline is a system that regenerates a
corpus of authored content through an LLM, on a schedule or on demand,
while never destroying human edits and never re-paying for work that hasn't
gone stale. This domain owns the vocabulary and abstraction map for that
architecture, and routes to the library (`content_pipeline`, this plugin's
shared lib) that implements it. Neutral illustrations only in this skill and
its references -- e.g. a product-copy generator that mutates authored rows
in place, or a support-macro translator that emits standalone artifacts.
Never specifics of any particular consuming project.

```yaml
domain_skill:
  _schema_version: "1"
  identity: Content-pipeline-domain owns the architecture vocabulary and abstraction map for LLM-in-the-loop batch content pipelines, and routes to the content_pipeline library that implements it.
  companions:
    siblings: []
    note: No sibling domains at this layer. openrouter-kit's openrouter-account skill is a companion at the plugin-dependency level (this domain's llm/ package consumes openrouter-kit for key + model + client) but is not a sibling domain -- it owns a different subject (API-key/account management).
  scope:
    covers:
      - the abstraction map (attributed store, freshness, validate, providers, llm, pipeline, deliver, vcs, roundtrip, audit, cli)
      - vocabulary for the store/freshness/validation/delivery/VCS/audit primitives
      - deciding which pipeline shape (single-pass vs. convergence-loop) and delivery mode (inplace vs. projection) fit a new pipeline
      - porting an existing monolithic LLM-batch tool onto these abstractions
      - the design-discipline philosophy behind the library's opt-in guardrails
    excludes:
      - one-off / interactive LLM chat usage (not a batch pipeline)
      - API key or model-registry management (see openrouter-kit's openrouter-account skill)
      - any project-specific pipeline instance (this domain carries zero project-specific knowledge)
  orientation:
    summary: |
      A content pipeline reads authored content, decides what has gone stale, regenerates
      the stale parts through an LLM, validates the result, and delivers it -- while
      preserving every human edit along the way and never re-doing work that is still
      fresh. The library (`content_pipeline`) is organized as one sub-package per
      concern in that lifecycle; a new pipeline composes from them, a ported pipeline
      collapses its generic modules onto them. Guardrails (floor guards, do-no-harm
      markers, validator tiers) are opt-in components, never forced -- a minimal
      pipeline is buildable with none of them registered.
    behavioral_guardrails:
      - Never invent project-specific illustrations. Use a neutral domain (product-copy generator, support-macro translator) for every example.
      - The library's guardrails are guidance, not gates -- do not present a floor guard, a do-no-harm marker, or a validator tier as mandatory. Each is a component a pipeline opts into.
      - When porting an existing tool, port freshness first (it is pure -- no LLM, no VCS, no I/O side effects to mock) and pin the existing tool's freshness tests as an equivalence baseline before collapsing the module.
      - A pipeline picks exactly one delivery mode (inplace XOR projection) and exactly one VCS backend (null / git / a p4-kit-shipped backend) -- these are not layered.
    vocabulary:
      - term: attributed store
        definition: A canonical record whose fields carry three slices -- sourced, machine, human -- resolved to an effective value by fixed human-always-wins precedence. Do-no-harm is baked into the data model, not a runtime check.
      - term: single-intermediary hash anchor
        definition: Synthesizing one per-entity slice from raw inputs and hashing only that slice, so downstream freshness depends on a narrow purpose-built hash rather than a sprawling set of raw-input hashes.
      - term: candidate cell
        definition: The many-candidates generalization of an attributed field -- active/shadow/retired candidate lists, each carrying a cached grade and deterministic fact riders. The degenerate case is one candidate per field (equivalent to a plain attributed field).
      - term: two-tier freshness
        definition: A source-tier hash plus a generation-tier hash, cross-referenced by one predicate (human > missing > stale > fresh) that every "needs regen" call site delegates to.
      - term: one-validator-many-sites
        definition: A single Validator contract shared by in-generation-loop and post-hoc call sites, so a rule can never drift between "what the agent checks while generating" and "what the audit checks afterward." Rejections tier hard/soft/advisory.
      - term: provider tiers
        definition: A name -> (callable, tier) registry for prompt context; source tier is unit-agnostic, generation tier is parameterized per-language/per-variant. A single-owner assembly module structurally prevents prompt-block drift between build sites.
      - term: convergence gate
        definition: The CONVERGED/STALLED verdict that drives a fill -> grade -> select -> apply cycle to a stopping point. Only reached by pipelines using the convergence-loop shape; a single-pass pipeline never touches it (CRP -- opt-in, not on the core import path).
      - term: delivery modes -- inplace / projection
        definition: inplace mutates authored content directly with a do-no-harm marker and first-class revert; projection emits append-only artifacts alongside the source with .bak rollback, never overwriting. A pipeline picks exactly one.
      - term: VCS seam
        definition: A VcsBackend protocol (open_for_edit, add, make_changeset, move_into, finalize_description, revert, delete_if_empty) that delivery drives. git is the implied default backend; a Perforce backend for the same seam ships in p4-kit, not here.
      - term: round-trip
        definition: The default human-in-the-loop loop -- either per-entity (machine asks a question, a human answers, the answer re-enters as context) or batch (export for review, intake corrections as human-attributed values).
      - term: floor guards
        definition: Opt-in, advisory-only diagnostics comparing a candidate against a known-good fixture within a <10% acceptance band. Never a hard block; a pipeline registers one only when it wants the signal.
  index:
    references:
      - id: building_a_pipeline
        path: references/building-a-pipeline.md
        keywords: [new pipeline, pipeline shape, single_pass, convergence_loop, work-unit strategy, FlatChunkStrategy, GraphWalkStrategy, attributed store schema, MergePolicy, two-tier freshness, classify, ensure, provider registration, run_tier, assembly, validators, Severity, floor guard, delivery mode, inplace Marker, projection, VcsBackend, git null, roundtrip, CLI scaffold budget bulk unsupported, AuditSpec Recorder, MockBackend, submit_validated, standing up a pipeline]
        summary: Eleven-step how-to for building a new pipeline on content_pipeline -- pick shape (single_pass/convergence_loop) and work-unit strategy, define the attributed store + MergePolicy, wire two-tier freshness, register providers + assembly, write Severity-tiered validators + optional floor guards, pick delivery mode (inplace Marker / projection) and a VcsBackend, add roundtrip, stand up the CLI, wire the audit spec + Recorder, and test with MockBackend. Real API snippets.
      - id: porting_a_pipeline
        path: references/porting-a-pipeline.md
        keywords: [port, migration, existing tool, monolithic facade, inventory subpackages, equivalence baseline, freshness first, pin existing tests, dependency order, collapse module, retired surfaces, delete not stub, documented deviation, shared_libs, sys.path shim, fail-soft, _shared_libs path, project-venv consumption]
        summary: Subsystem-first porting method -- inventory the monolith against the 13 subpackages, port freshness first, pin the existing test suite as an equivalence baseline before collapsing each module, work outward in dependency order (store, validate, llm, providers, deliver/vcs, cli last), re-run the tool's own suite as the final oracle, record retired surfaces honestly (delete-not-stub), and the fail-soft sys.path shim for project-venv consumption.
      - id: design_discipline
        path: references/design-discipline.md
        keywords: [design philosophy, opt-in guardrails, human-always-wins, do-no-harm markers, first-class revert, write-only-on-diff, one-predicate freshness, one-rule-set-many-sites, advisory floor guards, known-good <10% gate, deterministic seeding, exact-path never-wildcard VCS, truthful audit stamping, altitude discipline, entity-level vs item-level, minimal pipeline needs none]
        summary: The opt-in design philosophy behind the library and the component that embodies each principle -- human-always-wins attribution, do-no-harm markers + first-class revert, write-only-on-diff, one-predicate freshness, one-rule-set-many-sites validation, advisory floor guards on a known-good <10% gate, deterministic seeding, exact-path-never-wildcard VCS, truthful audit stamping, altitude discipline. Every one opt-in; a minimal pipeline needs none.
```
