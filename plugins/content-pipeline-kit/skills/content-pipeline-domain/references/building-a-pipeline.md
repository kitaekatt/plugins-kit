# Building a Pipeline

A stepped procedure for building a NEW pipeline on `content_pipeline`. Each
step names the sub-package it composes from, states the decision it forces,
and shows the real API. Steps 1-6 build a minimal working pipeline; steps
7-11 add the opt-in guardrails (each is a component you register only when
you want its signal -- a minimal pipeline needs none of them).

Illustrative domains used throughout, both neutral: a **product-copy
generator** that regenerates catalog blurbs by mutating authored rows in
place, and a **support-macro standardizer** that emits normalized standalone
artifacts alongside authored macros without overwriting them.

Every import below is `from content_pipeline.<subpackage> import ...`. The
package re-exports nothing eagerly -- import the submodule you need.

## 1. Pick the pipeline shape

Two shapes ship, in `pipeline`:

- **`single_pass.run_single_pass`** -- the regenerate-on-stale, two-phase
  generate/apply shape. Each unit is classified for freshness, generated once
  if stale, then applied. This is where most pipelines start.
- **`convergence_loop.run`** -- the `grade -> select -> apply -> fill` cycle,
  driven to a `CONVERGED` / `STALLED` verdict. Use it only when a unit needs
  multiple candidate values graded against a signal before a winner is picked.

Pick single-pass unless you genuinely iterate candidates against a grader.
Convergence-loop is heavier (a candidate store, a grading stage, a progress
measure) and pays off only when "generate once and apply" cannot express the
work. The product-copy generator that writes one blurb per product is
single-pass; a variant that generates three blurbs per product and grades
them for tone before selecting is convergence-loop.

```python
from content_pipeline.pipeline import single_pass

outcomes = single_pass.run_single_pass(
    units,
    freshness_of=classify_unit,     # WorkUnit -> FreshnessState  (step 4)
    generate=generate_unit,         # WorkUnit -> candidate       (step 5/6)
    apply=apply_unit,               # (WorkUnit, candidate) -> None (step 7)
)
```

`run_single_pass` catches `generate` / `apply` exceptions per unit and
surfaces them as an `ERROR` `UnitOutcome`, so one bad unit never aborts the
sweep -- the bulk driver (step 10) decides whether an error class should halt.

For convergence-loop, `run(store, grade=, select=, apply=, fill=, measure=,
max_cycles=)` drives the four stages in the fixed order grade -> select ->
apply -> fill (grade precedes fill so a cold-start store's empty seed is
baked gradeable before fill runs), reads `measure(store) -> (produced,
outstanding)` after each cycle, and stops the instant the gate (step 6's
`ProgressEvaluator` by default) returns `CONVERGED` or `STALLED`.

## 2. Choose a work-unit strategy

`pipeline.workunit` defines `WorkUnit(id, payload, context)` and two
strategies implementing the `WorkUnitStrategy` protocol (`.units(store) ->
list[WorkUnit]`):

- **`FlatChunkStrategy(select, chunk_size=0)`** -- when units are independent.
  `select` maps the store to `(id, payload)` pairs; `chunk_size` drives
  `.chunks()` for the bulk worker. Nothing reorders, because a flat strategy
  asserts independence. The product-copy generator (each product's blurb
  stands alone) uses this.
- **`GraphWalkStrategy(order, payload_of=, context_of=, predecessors_of=)`**
  -- when structural adjacency matters. `order` yields node ids; `context_of`
  receives the units already walked, so a node's context can depend on its
  predecessors. Use it when a unit's generation reads its neighbors (a
  dependency order, a cadence between adjacent units).

```python
from content_pipeline.pipeline.workunit import FlatChunkStrategy

strategy = FlatChunkStrategy(
    select=lambda store: [(row["sku"], row) for row in store.rows],
    chunk_size=50,
)
units = strategy.units(store)
```

Both strategies expose the same `.units()` interface, so this choice does not
ripple into the rest of the pipeline.

## 3. Define the attributed store schema + MergePolicy

The store (`store` sub-package) is the canonical record. Its core is
**attribution**: every field carries up to three slices -- `sourced`
(authored/original), `machine` (last generated), `human` (a correction) --
resolved by fixed `human > machine > sourced` precedence.
`store.attributed.effective_value(sourced, machine, human)` (or
`AttributedField(...).resolve()`) does the resolution; the default presence
test is truthiness, and a block-precedence field passes a `present` predicate.

Do-no-harm is a property of this data model, not a runtime check: a machine
regeneration writes only the `machine` slice, so a populated `human` slice
always wins. You do not "remember to preserve human edits" -- the schema
makes losing them impossible.

Declare a **`MergePolicy`** to say which fields survive a regeneration:

```python
from content_pipeline.store.attributed import MergePolicy, merge_preserved_fields

policy = MergePolicy(
    human_fields=["blurb_human"],       # human overrides, never clobbered
    carry_fields=["source_hash", "generation_hash"],  # always reused (hashes)
    conditional_fields=["blurb_machine"],   # reused only when inputs unchanged
    unchanged=lambda old, new: old.get("source_text") == new.get("source_text"),
)
merged = merge_preserved_fields(existing_record, fresh_record, policy=policy)
```

`human_fields` and `carry_fields` both carry-when-present (kept distinct only
to document intent); only `conditional_fields` gates on `unchanged`. Keyed
sub-collections (a list of per-line items) merge under a `CollectionMerge`
with its own `id_key` and a `keep_orphans_when` rule that retains a dropped
item still carrying authored work. Every rule is a field-name list, so the
module never learns a domain field name.

For the many-candidates case (step 1's convergence-loop), use
`store.candidate`: a `CandidateCell` holds active/shadow/retired `Candidate`
entries, and `promote_candidate(cell, id, retire_previous=False)` makes one
active (default keeps the prior as a still-selectable shadow). The degenerate
one-candidate-per-field case is equivalent to a plain attributed field.

If raw inputs are sprawling, anchor freshness on a synthesized per-entity
slice with `store.intermediary.ensure_intermediary(IntermediarySpec(...))`:
you hash only that narrow slice, so a change to an unrelated entity's sources
produces zero downstream drift. The full path always writes (re-stamping the
current hash) so the cheap path reclaims the entity next run.

## 4. Wire freshness (two-tier hashes)

`freshness` decides what has gone stale. It is pure -- no LLM, no VCS, no I/O
side effects -- and it is the subsystem to get right first.

Two hash tiers (`freshness.tier`), cross-referenced by one predicate:

- **Source tier** (`SourceTier`) -- a hash of a unit's raw source content.
  Drift here invalidates the cheap derived artifact.
- **Generation tier** (`GenerationTier`) -- a per-item hash of the exact
  inputs a generation call consumed. Drift here invalidates the expensive
  machine output.

`is_cross_ref_stale(recorded_source_hash, current_source_hash)` is the single
staleness predicate; an empty recorded hash reads as stale, forcing one
rebuild. Build hashes with `freshness.hashing`: `content_hash(*values)` for
the general case, `shared_snapshot(*values)` + `combined_hash(item, shared)`
to canonicalize unit-level inputs once and reuse them per item, and
`corpus_hash(pairs)` for the cross-reference digest a derived artifact
records as "the source state I was built from."

Every "needs regen" call site delegates to the single predicate in
`freshness.classify`:

```python
from content_pipeline.freshness import classify

def classify_unit(unit):
    return classify.classify(
        unit.payload, expected_hash=current_generation_hash(unit),
        human_field="blurb_human", machine_field="blurb_machine",
        hash_field="generation_hash",
    )
# needs_generation(state) is True for MISSING (always) and STALE (default sweep)
```

Priority is `HUMAN > EXCLUDED > MISSING > STALE > FRESH`. `bucket_counts`
tallies states for a coverage view; because the coverage buckets and the
regen set both derive from this one `classify`, they cannot disagree.

For the write itself, `freshness.ensure.ensure(ArtifactSpec(...))` regenerates
in memory, compares content hashes, and writes only on a real change --
carrying an optional `pre_write` hook (the VCS open-for-edit seam) and a
`prerequisites` cascade so upstream drift is materialized first.

## 5. Register providers + assembly

`providers` is the tiered context registry a prompt assembles from.
`registry.register(name, fn, tier=...)` (or the `@provider(name, tier=...)`
decorator) records a `name -> (callable, tier)` pair. Two tiers:

- **`SOURCE_TIER`** (`"source"`) -- unit-agnostic context (the same value
  regardless of which unit runs).
- **`GENERATION_TIER`** (`"generation"`) -- parameterized per variant, with
  the variant forwarded as extra args to `run_tier`.

```python
from content_pipeline.providers import registry
from content_pipeline.providers.registry import SOURCE_TIER

registry.register("style_guide", lambda src, item: {"text": src.style}, tier=SOURCE_TIER)
brief = registry.run_tier(SOURCE_TIER, source, item)   # {name: output}, sorted order
```

Assemble the prompt through `providers.assembly`, the single owner of block
composition, so two build sites cannot drift on how a block is composed.
`assemble_blocks([Block(name, body, include=...)])` joins ordered, optionally
conditional blocks; `SlotSyntax().render_map(template, values)` fills
`${name}` slots.

Use **label indirection** (`assign_labels(keys) -> {key: label}` and
`relabel(response_by_label, label_by_key)`) only when you batch many items
into one LLM request: opaque `item_1` / `item_2` labels stop the model
collapsing sibling items that share a visible key pattern, and `relabel`
round-trips the response back to real keys. A single-item request needs none
of this.

## 6. Pick an LLM backend and the mock seam

`llm.backends` ships five live transports, a `MockBackend`, and a process-level
`route`:

- **`OpenRouterBackend`** -- the real completion transport (consumes
  llm-scripting-kit for key + model + client).
- **`ClaudeCliBackend`** -- an agent-loop transport.
- **`CodexCliBackend`** -- an agent-loop transport over `codex exec`.
- **`OpencodeCliBackend`** -- an agent-loop transport over `opencode run`; its
  model id is the user's `provider/model` string and its answer is on stdout.
- **`ModelEndpointBackend`** -- a completion against an endpoint declared in
  the model-endpoints registry.
- **`MockBackend`** -- deterministic and scriptable, for every test.

`route(openrouter=, claude_cli=, codex_cli=, opencode_cli=, model_endpoint=, mock=)` reads the
`CONTENT_PIPELINE_LLM_BACKEND` env var and returns the active instance; a
supplied `mock` always wins so tests never reach a live transport.

### The model-endpoint backend

`CONTENT_PIPELINE_LLM_BACKEND=model-endpoint` talks to an OpenAI-compatible
endpoint declared in the registry at `~/.claude/config/model-endpoints.yaml`
(or whichever file `MODEL_ENDPOINTS_REGISTRY` names) -- typically a locally
hosted keyless server, though keyed entries are supported too. Pick a specific
entry with `CONTENT_PIPELINE_LLM_ENDPOINT=<entry id>`; omit it for the
registry's own `default`.

Two things differ from the other transports:

- **Availability is checked, not assumed.** `route()` pings the selected entry
  and raises `LLMUnavailableError` if it is down, so a bulk run fails at
  selection instead of rediscovering the same dead host once per unit. Only the
  selected entry is pinged, and only there. A server that dies mid-run surfaces
  as a `HALT_UNREACHABLE` halt on the failing call.
- **Reasoning effort defaults per entry.** Set it per call via
  `options.extras["reasoning_effort"]` (`none|low|medium|high|xhigh`); omit it
  and the entry's own default applies; pass an explicit `None` to send nothing
  and let the server decide. The plugin ships no effort value of its own.

**Does your unit need a harness at all?** This backend is a plain completions
call, and that is the right shape BECAUSE pipeline units are pure
transformations of fully-supplied context -- summarize, classify, translate,
rewrite, extract, score. A harness (`ClaudeCliBackend`, `CodexCliBackend`,
`OpencodeCliBackend`) adds an agent loop, tools, instruction-file ingestion,
and a working directory; filesystem posture is backend-specific (OpenCode is
unconfined). At roughly 11k-34k tokens of fixed prompt overhead per unit, it turns
a seconds-long call into a minutes-long session. It earns that only when the
information needed is not knowable when the prompt is written: the unit must
discover what to read, verify its own output, iterate, edit in place across
files, or honour instruction files it was not handed. If you can hand the unit
everything it needs, keep it a completion. Run a completion through
`platform.call_llm(backend, system, user, model=..., cache_dir=..., pricing=...)`,
which layers a budget guard, a content-addressed response cache, retry, and
cost accounting over one `backend.complete`.

For a generation that must satisfy validators, use the validate-until-valid
loop `platform.submit_validated`:

```python
from content_pipeline.llm import platform

result = platform.submit_validated(
    backend=backend, system=system, user=user, model="some/model",
    parse_fn=parse_blurb, validators=my_validators, max_attempts=3,
    cache_dir=cache_dir,
)
# result.accepted; result.payload; result.rejections; result.responses (audit trail)
```

Both the in-loop generation site and the post-hoc audit validate through the
SAME `validate.contract` validators (step 8), so the rule set cannot drift
between them. Per-attempt cache-busting is automatic.

For the convergence-loop shape, the stopping gate is `llm.convergence`:
`ProgressEvaluator(stall_window=2, converge_window=1).evaluate(history)` folds
a sequence of `Round(produced, outstanding)` into a `CONVERGED` / `STALLED` /
`CONTINUE` verdict. `CONVERGED` is checked before `STALLED`.

For tests, script a `MockBackend` and route to it:

```python
from content_pipeline.llm.backends import MockBackend
backend = MockBackend(responses=["blurb one", "blurb two"])
# or keyed by prompt substring for order-independent concurrency tests:
backend = MockBackend(keyed_responses={"SKU-1": "first", "SKU-2": "second"})
```

## 7. Pick a delivery mode and a VCS backend

Pick exactly ONE delivery mode from `deliver` -- they are not layered.

**`deliver.inplace`** -- mutate authored content in place. Every
machine-written row carries a do-no-harm `Marker` tag; `classify_ownership`
reads a populated-but-unmarked row as HUMAN and leaves it untouched.
`apply_inplace(rows, store, InplaceSpec(...))` rebuilds only the machine-owned
rows purely from the store (idempotent re-apply), and `revert_marked` strips
the marker and clears the value on marked rows -- first-class revert. The
product-copy generator uses this: it owns the rows it wrote, and a human who
edits a blurb takes ownership of that row forever.

**`deliver.projection`** -- emit append-only artifacts alongside the source,
never overwriting. `apply_projection(path, content, serialize=..., load=...,
validate=...)` writes through a `.bak` backup with reload-validation and
rolls back the backup on any failure. `aggregate_projections` folds many
`(artifact, unit)` pairs into one artifact. The support-macro standardizer
uses this: it never touches the authored macro, it emits a normalized sibling.

Pick exactly ONE `vcs.seam.VcsBackend`:

- **`vcs.git_vcs.GitVcs`** -- the shipped default (git is the implied default
  VCS). `move_into` is `git add` of exact paths only, never a wildcard.
- **`vcs.null_vcs.NullVcs`** -- a no-op backend for CI, tests, and non-VCS
  consumers.
- A Perforce backend for the same seam ships in **p4-kit**, not here.

The delivery mode drives the backend through the seam; `deliver` never
constructs a backend, it takes one by injection. The changeset choreography
(`deliver.inplace.deliver_changeset`) -- placeholder changeset up front,
per-item inline moves, description rebuilt from only the successfully-moved
subset, delete-if-empty -- lives once in `deliver`, driving whichever backend
is configured.

```python
from content_pipeline.deliver import inplace
from content_pipeline.vcs.git_vcs import GitVcs

result = inplace.deliver_changeset(
    items, vcs=GitVcs(repo_root),
    item_id=lambda it: it.id, path_of=lambda it: it.path,
    apply_item=write_one, describe=lambda moved: f"regenerate {len(moved)} blurbs",
)
```

Pass `changeset=` to deliver INTO a changeset you already hold instead of
minting one, so several passes land in one reviewable unit. Adoption means you
own its lifecycle: a pass that moves nothing leaves an adopted changeset
exactly as found (no finalize, no delete-if-empty), rather than blanking your
description or deleting a changelist that holds an earlier pass's files.

## 8. Write validators (Severity tiers) + optional floor guards

A `validate.contract.Validator` is `(candidate, context) -> Sequence[Rejection]`
(empty == accept). Each `Rejection` carries a `Severity`:

- **`HARD`** -- always blocks.
- **`SOFT`** -- blocks by default (an advisory-but-enforced rule); demote with
  `block_soft=False`.
- **`ADVISORY`** -- never blocks (the escape-valve / floor-guard tier).

`run_rules(candidate, context, validators)` concatenates every validator's
output, sorted deterministically. `is_rejecting` / `blocks` are the single
accept/reject predicate every site shares; `assert_valid` raises one
aggregated `ValidationError`; `format_rejections` renders agent-facing
feedback. The SAME validator list feeds both `submit_validated` (step 6) and
the audit (step 11).

```python
from content_pipeline.validate.contract import Rejection, Severity

def no_placeholder(candidate, context):
    if "TODO" in candidate:
        return [Rejection(kind="placeholder", severity=Severity.HARD,
                          detail="blurb contains TODO", rule_id="R1")]
    return []
```

**Floor guards** (`validate.floor_guard`) are opt-in and advisory-only. A
guard is any `item -> bool` (True == suspicious). Before you trust one, gate
it against a known-good corpus: `evaluate_guards({name: guard}, known_good)`
accepts a guard only when its flag rate is strictly under `DEFAULT_THRESHOLD`
(0.10) -- a guard that flags more than 10% of known-good work is a bad signal
and must not ship. An accepted guard's `flag(guard, items)` surfaces items for
human review; it never auto-rejects. Register a floor guard only when you want
that signal.

## 9. Add round-trip (only if humans are in the loop)

`roundtrip` is the default human-in-the-loop component. Two shapes:

- **`roundtrip.questions`** -- machine asks, human answers, answers re-enter
  as context. `ask(questions, id, prompt)` adds/refreshes a question,
  `answer(questions, id, text)` records a reply, `answered_context(questions)`
  yields the `{id, prompt, answer}` fragments that re-enter generation, and
  `merge_questions` carries human answers forward across a regenerated set
  (delegating to the store's do-no-harm merge, retaining an orphaned answered
  question).
- **`roundtrip.returns`** -- batch export/intake. `export_for_review(entities,
  dest, to_row=, serialize=)` snapshots to review rows; `intake_corrections(src,
  parse=, to_correction=)` ingests ONLY the rows a human corrected;
  `apply_corrections` lands each as a `human`-attributed value (so it wins the
  do-no-harm precedence forever). The workbook format stays caller-side.

Skip this whole step if the pipeline is fully automated.

## 10. Stand up the CLI

`cli.scaffold` is the reusable dispatch scaffold a thin per-command CLI wires
onto, instead of a bespoke argparse tree. `dispatch(argv, commands)` maps
`argv[0]` to a `Command` (or a bare handler), renders the result as YAML
(`emit_yaml`), and returns stable exit codes (`EXIT_OK=0` / `EXIT_USAGE=2` /
`EXIT_ERROR=1`). `did_you_mean` backs unknown-command and unknown-scope
recovery; `filter_scope` filters a corpus by a scope value with a
did-you-mean fallback.

```python
import sys
from content_pipeline.cli import scaffold

commands = {"build": scaffold.Command("build", build_handler, help="regenerate blurbs")}
raise SystemExit(scaffold.dispatch(sys.argv[1:], commands))
```

Add, as needed:

- **`cli.budget`** -- the preflight / hard-stop guard. `preflight_check(probe)`
  re-raises an auth/credit halt as `BudgetStop` before any unit runs;
  `guarded_sweep(units, worker)` halts the sweep cleanly on the first
  `PipelineHaltError` (429/401), recording done/remaining for a resume.
- **`cli.bulk`** -- `run_bulk(units, worker, warm=...)`, the two-phase
  cache-warm bulk worker. The `warm` callable owns cache priming; the worker
  phase composes `guarded_sweep`, so a halt stops cleanly with partial
  progress.
- **`cli.unsupported`** -- the sticky-stub registry. An `UnsupportedRegistry`
  (passed by the caller, persistable) records a unit as structurally
  unsupported once, so a pipeline that cannot handle a unit's shape stops
  re-paying the same failing LLM call every run. `stub_record(unit_id, reason)`
  builds a store stub carrying the marker; a designer clears it by deleting
  the record. Prefer an explicit registry over the module-level
  `mark_unsupported` (process-global state does not round-trip).

Wire the sticky gate into `run_single_pass` via its `mark_unsupported` hook
and a `Gate(name, predicate, sticky=True)`.

## 11. Add the audit spec + Recorder (opt-in)

`audit` closes the loop: it classifies every delivered output against policy,
brief, and the store, using the SAME classifiers the runtime used to generate
and validate -- so the audit cannot disagree with the runtime's own judgment.

`auditor.AuditSpec` carries the injected runtime callables (`policy`,
`output_marked`, `store_has_record`, `store_value`, `output_value`);
`audit_corpus(entities, spec, entity_id=...)` emits `Finding`s over the
generalized six-kind taxonomy (`FALSE_NEGATIVE`, `FALSE_POSITIVE`,
`STORE_OUTPUT_MISMATCH`, `MISSING_VALUE`, `ORPHANED_OUTPUT`, `STALE_REF`).
`audit_references` handles the corpus-integrity half (an index entry that no
longer resolves).

`audit.reasoning_chain` records why a candidate was selected. `record_submission(
recorder, entity_id, submit_result)` duck-types a `submit_validated` result
(reads `responses` / `rejections` / `payload`) into a per-attempt trail
without importing `llm`. Pick a `Recorder`: `InMemoryRecorder` for tests,
`SidecarRecorder` for a per-item on-disk sidecar, `NullRecorder` to disable.

`audit.report.coverage_report(states, findings=...)` folds freshness states
(via the same `bucket_counts` the regen set uses) and findings into a coverage
view; `cost_effectiveness_report` combines findings with a plain cost ledger
(the consumer builds it from `llm.platform`'s accounting -- `audit` stays
LLM-free).

## Test with MockBackend

Every test that exercises pipeline logic scripts a `MockBackend` (step 6) and
a `NullVcs` (step 7), so the whole pipeline runs deterministically with no
real LLM call and no real VCS mutation. Because `freshness` is pure, `store`
is data-only, and the LLM and VCS seams are injected, a full single-pass run
is testable end to end in memory:

```python
from content_pipeline.pipeline import single_pass
from content_pipeline.llm.backends import MockBackend
from content_pipeline.vcs.null_vcs import NullVcs

backend = MockBackend(responses=["generated blurb"])
outcomes = single_pass.run_single_pass(
    units, freshness_of=classify_unit,
    generate=lambda u: generate_with(backend, u),
    apply=lambda u, c: apply_with(NullVcs(), u, c),
)
assert [o.disposition for o in outcomes] == [single_pass.Disposition.GENERATED]
```

Reserve `OpenRouterBackend` / `ClaudeCliBackend` and a real `GitVcs` for
actual runs.
