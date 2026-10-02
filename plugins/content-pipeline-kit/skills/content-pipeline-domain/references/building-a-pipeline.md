# Building a Pipeline

A stepped procedure for building a NEW pipeline on `content_pipeline`. Each
step names the sub-package it composes from, states the decision it forces,
and shows the real API. Steps 1-6 plus the tracked run loop (see "The tracked
path" in step 10) build a minimal working pipeline; the rest of steps 7-11
add the opt-in guardrails (each is a component you register only when you
want its signal -- a minimal pipeline needs none of them).

Illustrative domains used throughout, both neutral: a **product-copy
generator** that regenerates catalog blurbs by mutating authored rows in
place, and a **support-macro standardizer** that emits normalized standalone
artifacts alongside authored macros without overwriting them.

Every import below is `from content_pipeline.<subpackage> import ...`. The
package re-exports nothing eagerly -- import the submodule you need.

## 1. Pick the pipeline shape

Two shapes ship:

- **The regenerate-on-stale, two-phase generate/apply shape** -- each unit is
  classified for freshness, generated once if stale, then applied. It runs on
  the tracked path: `ExecutionStore.register_units` +
  `execution.controller.prepare_run` + `execution.drivers.inline.run_wave` +
  `execution.controller.finalize_run` (see "The tracked path" in step 10). The
  gate, freshness and generate seams plug into `prepare_run`.
- **`convergence_loop.run`** -- the `grade -> select -> apply -> fill` cycle,
  driven to a `CONVERGED` / `STALLED` verdict. Use it only when a unit needs
  multiple candidate values graded against a signal before a winner is picked.

Pick the single-pass shape unless you genuinely iterate candidates against a grader.
Convergence-loop is heavier (a candidate store, a grading stage, a progress
measure) and pays off only when "generate once and apply" cannot express the
work. The product-copy generator that writes one blurb per product is
single-pass; a variant that generates three blurbs per product and grades
them for tone before selecting is convergence-loop.

For convergence-loop, `run(store, grade=, select=, apply=, fill=, measure=,
max_cycles=)` drives the four stages in the fixed order grade -> select ->
apply -> fill (grade precedes fill so a cold-start store's empty seed is
baked gradeable before fill runs), reads `measure(store) -> (produced,
outstanding)` after each cycle, and stops the instant the gate (step 6's
`ProgressEvaluator` by default) returns `CONVERGED` or `STALLED`.

Observers receive a `LoopEvent` at each boundary. A `STAGE_FINISHED` event
carries the stage's raw return value in `event.result` (`None` when the stage
returned nothing), so a binding can pass one stage's result to the next without
a side dict. When `measure` is built with `cell_policy.measure_from(cells_of,
policy, detail_of=fn)`, `fn(store)` is attached to every `Round.detail`, so
per-cycle bookkeeping reads the round and does not reload the store.

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
to document intent); only `conditional_fields` gates on `unchanged`.
Presence is decided by a `present` predicate (keyword-only on `MergePolicy`
and `CollectionMerge`) whose default treats `None`, `""`, and an empty
container as absent and everything else -- including `False` and `0` -- as
present, so a human override of `False` or a carried count of `0` is never
mistaken for absence and dropped. Keyed
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

`route(openrouter=, mock=)` reads the `CONTENT_PIPELINE_LLM_MODELS` model
declaration (llm-scripting-kit registry ids, comma-separated) and returns the
backend for its first usable entry: a `claude`, `codex` or `opencode` harness
entry gets that CLI backend, the `openrouter` entry gets `OpenRouterBackend`,
and any other transport entry gets `ModelEndpointBackend`. Unset, it returns
`OpenRouterBackend`. A supplied `mock` always wins so tests never reach a live
transport. `CONTENT_PIPELINE_LLM_MODELS` is the only routing setting. `CONTENT_PIPELINE_LLM_BACKEND`, `CONTENT_PIPELINE_LLM_MODEL` and `CONTENT_PIPELINE_LLM_ENDPOINT` select nothing; if one is set while `CONTENT_PIPELINE_LLM_MODELS` is not, routing raises `ConfigurationError` naming it, rather than falling back to OpenRouter. When `CONTENT_PIPELINE_LLM_MODELS` is set, any of those three is ignored.

### The model-endpoint backend

`CONTENT_PIPELINE_LLM_MODELS=<entry id>` naming a transport entry of the
registry at `~/.claude/config/model-endpoints.yaml` (or whichever file
`MODEL_ENDPOINTS_REGISTRY` names) talks to that OpenAI-compatible endpoint --
typically a locally hosted keyless server, though keyed entries are supported
too. A `ModelEndpointBackend()` constructed with no `endpoint` uses the
registry's own `default`.

Two things differ from the other transports:

- **Availability is checked, not assumed.** `route()` pings the selected entry
  and raises `LLMUnavailableError` if it is down, so a bulk run fails at
  selection instead of rediscovering the same dead host once per unit. Only the
  selected entry is pinged, and only there. A server that dies mid-run surfaces
  as a `HALT_UNREACHABLE` halt on the failing call.
- **Reasoning effort defaults per entry.** Set it per call via
  `options.effort` (`none|low|medium|high|xhigh`); omit it and the entry's own
  `reasoning_effort` applies. Either one is sent in the entry's effort style
  (`top-level`, `ninfer`, which sends `high` as `xhigh`, or
  `chat_template_kwargs`); an entry that resolves no style is sent no effort.
  `llm-scripting-kit resolve --models <entry>` reports the style under
  `effort_delivery`. An effort in `options.extras` --
  `extras["reasoning_effort"]`, or nested under
  `extras["chat_template_kwargs"]` -- is sent verbatim instead; an explicit
  `None` there sends nothing and lets the server decide. The plugin ships no
  effort value of its own.

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
cost accounting over one `backend.complete`. For a cap that two OS processes
share rather than an in-process `CostBudget`, pass `spend=` as well -- see "The
cross-process spend cap" in step 10.

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

**Structured output.** A caller that needs a JSON object of a known shape
declares it with an `llm_scripting_kit.completion.OutputContract` (an `id`, a
`policy`, a JSON Schema, and an optional `schema_version`) and passes it as
`submit_validated(..., output_contract=contract)`, or on
`BackendOptions.output_contract`. A schema-policy contract (`native-required`
or `validated-result`) takes no `parse_fn`: the validated object is
`result.payload`. Two questions stay separate:

- Structural validity: does the output conform to the declared schema?
  llm-scripting-kit answers it, before any validator runs.
- Domain validity: is the conforming object acceptable content? Your
  `validate.contract` validators answer it, unchanged, and they see only a
  structurally valid object.

A schema failure is one HARD `schema_violation` Rejection. Its payload keeps
the contract identity, the disposition, the schema errors as `(path, keyword)`
pairs, and the raw output. It feeds the same retry loop as any rejection. A
`text-only` contract records the report and keeps `parse_fn`. The contract is
part of the cache key, and a cache hit is served only with a report for the
same contract identity and a success disposition.

Delivery follows the backend. A backend may enforce the schema natively, or
deliver it in the prompt; a text-only contract delivers no schema (`delivery: none`); a response whose backend reported nothing carries
`delivery: unreported` and is judged locally by llm-scripting-kit's validator.
`native-required` refuses a backend that cannot deliver natively rather than
downgrading. Per-adapter schema rules belong to llm-scripting-kit: the codex
adapter requires OpenAI strict-mode schemas (`additionalProperties: false` and
a full `required` list at every object level), while prompt-delivered adapters
accept the package's schema subset. The contract path refuses without
llm-scripting-kit >= 0.56.0 and never substitutes a local parse.

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

### Durable waits (opt-in)

A durable wait holds ONE unit of a tracked run while a person answers a typed
question, and records the question and the answer in the execution store. It
is opt-in per unit. A pipeline that never calls an interrupt verb and never
raises the signal below is not changed: `roundtrip` is untouched, and there is
nothing to configure. The one visible difference is the store file, which gains
two tables (`interrupts`, `interrupt_resolutions`) that stay empty until a wait
is requested.

Pick the shape by what the answer is for:

| The need | Use |
| --- | --- |
| A question about an entity, answered between runs (a workbook, a review screen), re-entering as context | `roundtrip.questions` or `roundtrip.returns`, above |
| A unit that cannot finish without a person's answer, while the rest of the run continues, and the answer is a schema-checked value the run records | a durable wait |

The two do not exclude each other, and a pipeline can use both.

**Which lanes can ask.**

- The inline lane can ask, and so can a consumer's own loop over the store.
- The background lane refuses a wait under its open dispatch: `store.request_interrupt`
  raises `WaitUnderDispatchError` and writes nothing.
- The workflow lane has no supported request surface. Its worker protocol has
  no wait verb, nothing in the library handles a wait requested through a verb
  a consumer mounts itself, and the store does not refuse a request for a
  claimed unit that has no dispatch row.

**Asking from the inline lane.** Raise `InterruptRequested` from `generate`.
`run_wave` turns the signal into `store.request_interrupt` under the claim's
own fencing token, leaves the unit `waiting` (no claimant, no lease), keeps it
out of the returned list, and goes on with the next unit of the wave. The
signal can also come from `adapter.build_request` or
`adapter.validation_spec_for` on the backend path. It cannot come from
`parse_fn` or a validator, which the validate loop treats as a rejection, nor
from `adapter.unit_for`, which runs before the claim. A consumer that decides
after generation asks from `generate`.

```python
from content_pipeline.execution.interrupts import unit_resolutions
from content_pipeline.execution.model import InterruptRequest, InterruptRequested

APPROVAL = {
    "type": "object",
    "required": ["approved"],
    "properties": {"approved": {"type": "boolean"}},
    "additionalProperties": False,
}


def generate(work_unit):
    seen = unit_resolutions(store, run_id, work_unit.id)
    if not seen:
        raise InterruptRequested(
            InterruptRequest(
                kind="approval",
                request_schema=APPROVAL,
                payload={"question": f"Publish {work_unit.id}?"},
            ),
            on_rejected="release",
            on_expired="release",
        )
    last = seen[-1]
    if last["outcome"] == "answered" and last["input"]["approved"]:
        return f"published copy for {work_unit.id}"
    return f"draft copy for {work_unit.id}"
```

**Asking from your own loop.** Claim the unit, then call
`store.request_interrupt(run_id, unit_id, claim.fencing_token, request)` with
the same `InterruptRequest` and the same `on_rejected` and `on_expired`
keywords. It returns an `InterruptRecord`. A request carries `kind` (lower-case
letters, digits and hyphens, at most 64 characters), a JSON-schema
`request_schema` for the answer, a `payload` shown to the person, and
optionally `expires_in_s`.

**Recording the answer.** Mount `store.resolve_interrupt` on your own command,
spreadsheet intake or review screen; the package ships no console script. Show
`store.list_interrupts(run_id)` (or `store.open_interrupt(run_id, unit_id)`) to
the person. `decision="answer"` validates `input` against the request's schema
and raises `ResolutionInputError` when it does not conform; `decision="reject"`
takes an optional `reason`. An identical replay returns the stored resolution
with `replayed=True`; a different second resolution raises
`ResolutionConflictError`.

**Policies, and what the unit does next.** `on_rejected` and `on_expired` each
take `stop` or `release`, and `stop` is the default. The resolution row records
the outcome either way; the policy decides the unit.

| Outcome | Policy | Unit | How the consumer proceeds |
| --- | --- | --- | --- |
| answered | not applicable | `pending` | the next attempt reads the typed answer from `unit_resolutions` and generates with it |
| rejected or expired | `stop` | terminal (`UnitState.OPERATOR_REJECTED` or `UnitState.INTERRUPT_EXPIRED`) | nothing more runs for the unit; a graph chain behind it is blocked, as behind a failed unit |
| rejected or expired | `release` | `pending` | the next attempt reads the outcome and continues without the answer, skips the unit, fails it, or asks again with another request |

To skip a released unit, either add a gate to `prepare_run` that fires on the
outcome, or call `store.fail_unit` with `terminal=True` and
`terminal_state=UnitState.SKIPPED` from your own loop.

The attempt after a release MUST read `unit_resolutions`. A consumer that
releases and then asks again without reading the outcome asks forever.
`unit_resolutions(store, run_id, unit_id)` returns the resolved interrupts of
one unit, oldest first, as dicts with `interrupt_id`, `kind`, `outcome`
(`answered`, `rejected` or `expired`), `input`, `reason` and `payload`; an open
interrupt is not in it.

**Draining a run that has a waiting unit.** A waiting unit is not claimable, so
a wave can be empty while the run is unfinished. A pass ends when the wave is
empty, `finalize_run` applied nothing, and `waiting_units` is non-empty; the
run is waiting, which is a healthy state. Run the loop again after an answer:
the answered unit is `pending` and is offered in the next wave. The loop below
calls `finalize_run` before it checks `unfinished_units`, because an accepted
unit is already terminal and `unfinished_units` does not list it: checking
first would return "complete" before the accepted units are applied.

```python
from content_pipeline.execution.controller import finalize_run, unfinished_units
from content_pipeline.execution.drivers.inline import run_wave
from content_pipeline.execution.interrupts import waiting_units
from content_pipeline.execution.wave import ready_wave


def drain(store, run_id, strategy, adapter, generate):
    """Run waves until the run is complete, waiting, or blocked."""
    while True:
        wave = ready_wave(store, run_id, strategy)
        if wave and store.get_run(run_id).halted_kind is None:
            run_wave(store, run_id, wave, adapter, generate=generate)
            continue
        applied = finalize_run(store, run_id, adapter)
        if not unfinished_units(store, run_id):
            return "complete"
        if applied:
            continue
        if waiting_units(store, run_id):
            return "waiting"
        return "blocked"
```

`"blocked"` covers any other reason work stays unfinished with nothing to run,
such as a unit claimed by another worker. A halted run still offers its
`pending` units, but `run_wave` claims none while the halt stands, so the loop
does not call `run_wave` then and falls through to `"waiting"` or `"blocked"`.
Clear the halt with `controller.resume_run`, then run the loop again.

**Expiry.** `expires_in_s` (an integer from 1 to 2147483647) sets a deadline.
No timer records it. A lapse is recorded by `store.resolve_interrupt` when it
observes one, and by `store.expire_interrupts(run_id)`, which your own
scheduler or loop calls; `InterruptRecord.lapsed(now)` reports a lapse without
writing. An interrupt lapses at its `expires_at`, and one with no expiry does
not lapse. `prepare_run(reclaim_at=...)` reclaims claimed units only, so it
neither offers a waiting unit nor records an expiry.

**What is recorded in events.** `execution.events.project_run` projects each
interrupt row as an `interrupt` event with phase `requested`, `resolved`,
`rejected` or `expired`, carrying the interrupt id, the kind and the claim's
fencing token as the attempt id. A `terminal` event follows a rejection or a
lapse only under `stop`. The request payload, the request schema and the answer
do not enter an event; a rejection's reason appears only in the `terminal`
event, cut to 1000 characters.

**The two libraries the verbs need.** `request_interrupt`, `resolve_interrupt`
and `expire_interrupts` use two shared libraries, probed inside the verb and
not at import: `bootstrap_lib` (the interrupt contract, bootstrap 0.137.0 or
later) and `llm_scripting_kit` (the schema validator, llm-scripting-kit 0.56.0
or later). They are libraries this package reaches, not plugin dependencies,
and no manifest entry is added for them. When one is missing or too old the
verb raises `InterruptSupportError` (an `ImportError`) before it writes, with a
message that names the plugin to install or update. Reads, status reports,
waves, `waiting_units` and `unit_resolutions` need neither. A run that has
interrupt rows also needs bootstrap 0.136.0 or later to project events.

Limits: an answer is at most 65536 bytes of canonical JSON, and a reason is cut
to 2000 characters.

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
  `check_response` raises `PipelineHaltError` on a hard-stop response text;
  `spend_stop(...)` is a context manager that turns a spend-ledger verdict into
  a `BudgetStop` ("The cross-process spend cap" below).
  The tracked `run_wave` records a halt itself (see "The tracked halt
  contract" below).
- **`cli.unsupported`** -- the sticky-stub registry. An `UnsupportedRegistry`
  (passed by the caller, persistable) records a unit as structurally
  unsupported once, so a pipeline that cannot handle a unit's shape stops
  re-paying the same failing LLM call every run. `stub_record(unit_id, reason)`
  builds a store stub carrying the marker; a designer clears it by deleting
  the record. Prefer an explicit registry over the module-level
  `mark_unsupported` (process-global state does not round-trip).

Wire the sticky gate into `prepare_run` via its `mark_unsupported` hook
and a `Gate(name, predicate, sticky=True)`. `Gate` and `run_gates` live in
`pipeline.gate` (also importable from `pipeline.single_pass`).

### The cross-process spend cap

For a paid pipeline whose work runs in more than one OS process, `CostBudget` is
not the guard: it is an in-process float accumulator, and two processes each hold
their own. `llm.spend_ledger` is the shared one -- one SQLite file in WAL mode,
integer nano-USD with ceiling rounding, and every admission summing the
outstanding total, comparing it to the cap and inserting its row inside one
`BEGIN IMMEDIATE` transaction, so two admissions never interleave.

```python
from content_pipeline.cli.budget import BudgetStop, spend_stop
from content_pipeline.llm.spend_ledger import (
    create_ledger, open_ledger, spend_ledger_from_env,
)

ledger = create_ledger(run_dir / "spend.sqlite", cap_usd=5.00, run_id=run_id)
# in every worker process on the same run:
ledger = open_ledger(run_dir / "spend.sqlite")
# or, for a worker that is handed the path through its environment:
ledger = spend_ledger_from_env()   # None when CONTENT_PIPELINE_SPEND_LEDGER is unset or empty

try:
    for unit_id in wave:
        with spend_stop(done, remaining, unit_id=unit_id):
            platform.call_llm(backend, system, user, model=model,
                              pricing=pricing, spend=ledger)
        done.append(unit_id)
except BudgetStop as stop:          # reason is "spend_cap" or "spend_halt"
    report_partial(stop.reason, stop.done, stop.remaining)
```

Six things to get right when composing one:

1. **The reservation unit is one provider ATTEMPT.** `call_llm(spend=...)`
   reserves immediately before each `backend.complete` and settles in a
   `finally`, so a call with `retries=3` takes four reservations, not one. Size
   the cap for attempts, not for calls: `submit_validated` forwards `spend=`
   verbatim and can admit `max_attempts * (retries + 1)` of them.
2. **Pass `pricing=` or `spend_reserve_usd=`.** `spend=` with neither is a
   `ValueError` before any provider call. Priced automatically, the reservation
   covers the request plus the full `options.max_tokens`, because a reservation
   has to cover what the attempt may actually bill.
3. **Wrap the call site, not the loop body, in `spend_stop`.** It is a context
   manager (not a probe-taking function like `preflight_check`), and it
   translates exactly `SpendCapExceeded` and `SpendLedgerHalted`.
   `sqlite3.OperationalError` from an exhausted busy timeout propagates
   untranslated and admits nothing -- fail-closed, and not a budget verdict.
4. **Decide the reclaim posture deliberately.** `orphan_reclaim` defaults to
   `"manual"`, so a process killed between reserve and settle holds its headroom
   until the consumer calls `reclaim_orphans()`. Open with
   `orphan_reclaim="lease"` to sweep inside every `reserve` instead. Either way
   the overshoot bound of a pass is the SUM of `reserved` over every row that
   pass reclaimed, so leave `reclaim_batch_limit` at its default of 1 unless a
   wider bound is acceptable. `call_llm` gives a reservation a lease of
   `2 * options.timeout_s + 60` seconds; with no `options.timeout_s` the lease is
   NULL and that reservation is never swept at all. A caller running its own
   watchdog extends a lease with `ledger.renew(reservation, ttl_s=...)`, which
   returns a new `Reservation` and also gives a NULL lease its first deadline.
5. **Report `status()` honestly.** `outstanding_usd` is what admission compares
   to the cap; `remaining_usd` may be negative; `written_off_usd` is money a
   reclaim stopped charging against the cap because its settle never arrived.
   The `SpendStatus` fields are `run_id`, `cap_usd`, `settled_usd`,
   `reserved_usd`, `unknown_usd`, `leaked_usd`, `reclaimed_usd`,
   `written_off_usd`, `outstanding_usd`, `remaining_usd`, `reservations_open`,
   `calls_settled`, `requests`, `halted`, `halt_reason`, `halt_detail` and
   `as_of`. `outstanding_usd` is `settled + reserved + unknown + leaked`;
   `reclaimed_usd` and `written_off_usd` are disclosure only.
6. **Know the halt lifecycle.** `settle(reservation, None)` means the spend is
   unreadable: the row is held as `unknown` at its reserved amount, never
   released to zero. A settle with a cost above its reservation records
   `overbilled`, counts the excess as leaked money, sets a halt when
   `halt_on_overbilled` is true (the default for `create_ledger` and
   `open_ledger`), and raises `SpendCapExceeded`. `ledger.halt(reason, detail)`
   stops every later admission across processes; each `reserve` reads the halt
   and raises `SpendLedgerHalted`, and `settle` is never gated by it.
   `ledger.check_halted()` raises the same error between units, as an early
   stop and not the enforcement. `ledger.resume()` clears a halt, but an
   `overbilled` halt raises `ValueError` unless called as `resume(force=True)`,
   and a forced resume is recorded in `halt_history` with `forced = 1`.

The env-var route is explicit: `spend_ledger_from_env()` opens the ledger named
by `CONTENT_PIPELINE_SPEND_LEDGER`, or returns `None`. `call_llm` never reads
that variable itself. With `spend=None` it reads no ledger at all, so a cap
must be passed in deliberately and never materializes mid-run out of an
inherited environment variable.

What this cannot do, and what a consumer therefore owns: a `call_llm` without
`spend=` bills the provider with no ledger row, and nothing in the ledger can
see it. Reserve-before-pay is a property of the call site. A pipeline that wants
it mechanically gives itself one wrapper around `call_llm` that always supplies
`spend=` and calls nothing else.

### The tracked path

The untracked loop helpers `single_pass.run_single_pass`,
`cli.budget.guarded_sweep` and `cli.bulk.run_bulk` were removed in
content-pipeline-kit 0.28.0. A caller migrating off them uses the tracked path:

1. `ExecutionStore.create_run`, then `ExecutionStore.register_units(run_id,
   unit_ids)` -- the store records the run and its units (the CLI's
   `create-run` and `register-units` commands do the same). `prepare_run`
   does NOT register units: on a run with none registered, a flat strategy
   returns an empty wave and does nothing, and a graph strategy whose
   `order()` yields ids raises `GraphOrderMismatchError`.
2. `execution.controller.prepare_run` -- evaluate gates and freshness over
   the registered units and return the wave.
3. `execution.drivers.inline.run_wave` -- claim and generate a wave.
4. `execution.controller.finalize_run` -- apply what was accepted.
5. `execution.controller.unfinished_units` -- the units without a terminal
   state, after a halt or at the end.

**Draining a run.** Repeating prepare and run until the wave is empty is not
enough, because an empty wave has causes other than completion. When a wave
comes back empty, finalize, then read the run: if it is halted, clear the
halt with `execution.controller.resume_run` once its condition has cleared
(a halted run yields no claims, so looping without it never makes progress);
if `unfinished_units` still lists a unit in the CLAIMED state, it is held
by an earlier crashed or refused attempt and is not offered again by default.
Pass `reclaim_at=time.time()` to `prepare_run`: a unit whose lease has
expired is then offered again and `run_wave` reclaims it (fence + 1). A live
lease, or a unit under an open dispatch, is never offered, and a unit already
reclaimed twice is failed terminally as `reclaim_exhausted`. Otherwise wait
out the lease, or stop and report it; if the run is not halted
and `unfinished_units` is empty, it is complete. Cap the loop, and stop when
one full pass changes nothing.

**Migrating a `BudgetStop` caller.** The removed `guarded_sweep` recorded the
tripping unit as `BudgetStop.unit_id` and built `remaining` as the units AFTER
it, so a resuming caller had to rebuild the unfinished set as
`[trigger] + remaining`. On the tracked path there is no such reassembly:
`unfinished_units(store, run_id)` returns every unit without a terminal state,
in original ordinal order, and the halt-triggering unit is already in it
(it was returned to `PENDING`).

**The tracked halt contract.** When generating a unit raises
`PipelineHaltError` (from `generate` itself, or from the backend inside
`submit_validated`), `run_wave` calls `controller.record_halt`: the run is
marked halted with the halt kind and detail, and the triggering unit goes back
to `PENDING` -- unfinished work, not a permanent failure. `run_wave` then
stops claiming units and returns the ids it accepted before the halt. A
halted run refuses new claims (a later `run_wave` on it claims nothing) until
`controller.resume_run` clears the halt. Units already accepted stay
accepted, and a claim already in flight with a valid fencing token can still
be accepted. Any other exception from `generate`, except the
`InterruptRequested` signal of a durable wait (step 9), propagates out of
`run_wave` and leaves that unit `CLAIMED` until its lease expires. Halt kinds are
the `PipelineHaltError.kind` values, plus `"pause"` for an operator pause.

### Execution events

`execution.events.project_run(store, run_id)` projects one tracked run's
attempt log into the shared execution-event envelope
(`plugins-kit.execution-event/v1`, specified by bootstrap's plugin-dev
reference `execution-events.md`). It only reads: the store is not changed,
and `write_run_events(store, run_id, sink)` writes the events to a sink you
supply (`InMemorySink`, or `JsonlSink` from `bootstrap_lib.execution_event`).

- Each event's `seq` is `attempts.id * 4 + phase`, so it follows commit order
  and is unchanged when you project again after more rows were appended. The
  run-created event is `seq` 0. `at` is informational; do not sort by it.
- Identity: `run_id`, `unit_id`, and `attempt_id` = the claim's fencing token.
- A claim is `call-started`. An accept or fail row yields `usage` (only when a
  count is known; unknown stays null, never 0), then `result`, then `terminal`
  when the unit reached accepted, failed or skipped. A retryable fail has no
  `terminal`. An expired lease is a `result` with status `expired` for the old
  attempt.
- Renewals, superseded submissions and the apply steps appear as
  `content-pipeline-kit:` extension events.
- Not projected: background `dispatches` (they have their own sequence, so
  there is no `dispatch-selected` event) and the audit reasoning chain (no run
  or attempt identity; its payload is model content).
- The events functions need `bootstrap_lib.execution_event`. Where it is not
  importable they raise `ExecutionEventSupportError` (an `ImportError`) with
  the install or update command; no other part of the package needs it.

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
without importing `llm`. Under an output contract each attempt event also
carries `contract` = `{id, schema_version, schema_digest, policy, delivery,
disposition}`, read from that attempt's stored response, and the final event
carries the last attempt's `contract`. The schema body is never copied: the
digest and version identify the schema. Pick a `Recorder`: `InMemoryRecorder` for tests,
`SidecarRecorder` for a per-item on-disk sidecar, `NullRecorder` to disable.

`audit.report.coverage_report(states, findings=...)` folds freshness states
(via the same `bucket_counts` the regen set uses) and findings into a coverage
view; `cost_effectiveness_report` combines findings with a plain cost ledger
(the consumer builds it from `llm.platform`'s accounting -- `audit` stays
LLM-free).

## Record a run and replay one stage (opt-in)

`content_pipeline.provenance` answers "what produced this output, and what
would stage K do with a different input". Stdlib only; the library writes only
under the bundle directory you pass.

```python
from content_pipeline.provenance.record import start_run
from content_pipeline.provenance.snapshot import EventLog, StageSnapshotter
from content_pipeline.provenance.call_audit import CallAuditor
from content_pipeline.provenance.replay import replay_stage
from content_pipeline.pipeline import convergence_loop

with start_run(bundle, root=project_root, params={...},
               inputs={"config": config_path}) as rec:
    snap = StageSnapshotter(bundle, write=write_store)      # write(store, path)
    log = EventLog(bundle / "events.jsonl")
    audit = CallAuditor(bundle / "calls")                    # pass as on_attempt=
    result = convergence_loop.run(store, grade=..., select=..., apply=...,
                                  fill=..., measure=..., max_cycles=5,
                                  observers=[snap, log])
    rec.finish(result={"verdict": result.verdict.value})

replay_stage(bundle, cycle=2, stage="fill", run=fill, out_bundle=out,
             root=project_root, materialize=load_store_from_snapshot,
             write=write_store, edit=edit_store, edits=["glossary entry X"])
```

The context manager records `error` when its block raises; call `finish` for
`ok`. `replay_stage` only reads the source bundle, refuses an `out_bundle`
inside it, and writes the edited store, the stage's output store, and a record
whose `source` names the source run, cycle, stage and the snapshot's sha256.
A stage that reads inputs the record did not hash (a global glossary, a
template directory) yields a link that looks complete and is not: pass those
files through `inputs`. A response-cache hit replays an old answer; each
`CallAttempt` carries `from_cache`, and cache policy stays yours.

## Test with MockBackend

Every test that exercises pipeline logic scripts a `MockBackend` (step 6) and
a `NullVcs` (step 7), so the whole pipeline runs deterministically with no
real LLM call and no real VCS mutation. Because `freshness` is pure, `store`
is data-only, and the LLM and VCS seams are injected, a full run through the
tracked inline driver (`prepare_run`, `run_wave`, `finalize_run`) is testable
end to end in memory.

Reserve `OpenRouterBackend` / `ClaudeCliBackend` and a real `GitVcs` for
actual runs.
