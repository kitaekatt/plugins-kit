# Area config: the runtime contract for an area

Every area has a **runtime contract** -- the structural details that govern how the agent and user interact with entities in the area. The contract has six fields and lives in an `area_config:` block. This doc defines the schema; the attachment patterns (where `area_config` physically lives in a document, depending on whether the area decomposes into sub-areas) live in `area-ownership.md` under "How ownership is expressed."

The same six-field shape applies whether the contract is attached at the area level (single-area document) or per sub-area leaf (multi-area document). The fields belong to **areas**, not specifically to sub-areas -- sub-areas are just the leaf case when an area decomposes.

## The six fields

All fields are optional; declare only what the area actually uses.

### state_terms (optional, list of strings)

Canonical state vocabulary the agent uses verbatim when discussing entities in the area. The agent re-phrases user-loose language ("the in-progress ones") into canonical terms ("the entities in `pending_review` state") to keep usage consistent.

State terms are the names the system uses internally; they are not free to be improvised. When a user asks about a state with a different label, the agent translates to the canonical term in the response.

### operations (optional, list of strings)

The verbs the area supports -- the operations a user or agent invokes against entities in the area. Each operation is one verb or short verb phrase. Define each operation in one sentence; details live in the per-capability record or in the area's reference docs.

Operations are distinct from capabilities. An operation is the verb shape ("transition", "approve", "rebuild"); a capability is the user-objective wrapping an operation with scope, inputs, and gotchas.

### scope_axes (optional, list of objects)

One or more axes that decompose the operation space within the area. Each axis carries a `name` and a list of `values` it can take. Scope axes let the agent and the user disambiguate "I want to X" into "I want to X scoped to Y on axis Z."

Scope-inference rule for the agent: infer scope from context where possible. For broad scopes that touch many entities or that perform expensive operations, confirm with the user before running rather than assuming the broadest scope.

Shape:

```yaml
scope_axes:
  - name: target_axis
    values: [single, batch, all]
  - name: depth_axis
    values: [shallow, deep]
```

### canonical_phrasing (optional, string)

A readback rule for restating a user's request in canonical vocabulary before executing it. The readback doubles as a confirmation prompt: the user sees the agent's interpretation and can correct before any side effect lands.

The string carries placeholders that resolve from context (e.g. `<entity_id>`, `<operation>`, `<scope>`). The agent fills the placeholders, restates the request, and proceeds.

Shape:

```yaml
canonical_phrasing: |
  I'll <operation> <entity_id> at <scope> scope. Confirm?
```

### llm_dependent_content (optional, list of strings)

Fields whose values are produced by an LLM rather than retrieved deterministically. Naming these explicitly helps the agent and an auditor distinguish content that must be regenerated from content that is fetched verbatim. LLM-dependent fields are subject to canonical-phrasing rules; deterministic fields are not.

### dependency_order (optional, list of strings)

Ordering constraints among the area's operations or capabilities. When operation B depends on operation A having been executed, the dependency-order list names that constraint so the agent surfaces the prerequisite when the user asks for B in isolation.

Each entry names a constraint as a short sentence (e.g. "validate before transition", "rebuild index before query") or as a structured `{capability_id: prerequisites}` mapping when the dependencies form a small graph.

## Worked example: single-area settings document

A document owns "the settings system" and does not decompose into sub-areas. The runtime contract attaches at the area level -- `area_config:` is a sibling field of `identity:` and `scope:` in the document's main typed unit.

```yaml
area_config:
  state_terms: [enabled, disabled, pending_save, deprecated]
  operations: [toggle, validate, reset, import, export]
  scope_axes:
    - name: target
      values: [single_setting, group, all]
    - name: profile
      values: [active, named_profile]
  canonical_phrasing: |
    I'll <operation> <setting_name> at <target> scope on <profile>. Confirm?
  llm_dependent_content: [import_diff_summary, validation_explanation]
  dependency_order:
    - validate before import
    - reset clears pending_save before applying defaults
```

This config gives the audit and the agent a single source of truth: state vocabulary is fixed (no improvising "active" instead of "enabled"), operations are enumerated (no `purge` operation if it is not on the list), and scopes are bounded (no operation runs against `all` settings without confirmation per the scope-inference rule).

## Worked example: per-sub-area content-pipeline contract

A multi-area document owns "the content pipeline" with sub-areas for ingestion, transformation, and publication. Each sub-area has its own runtime contract attached per leaf. The transformation sub-area's `area_config` (carried in the per-sub-area reference doc `references/transformation.md`):

```yaml
area_config:
  state_terms: [queued, in_progress, completed, failed, retried]
  operations: [run, retry, abort, inspect_status]
  scope_axes:
    - name: target
      values: [single_item, batch, all_pending]
  canonical_phrasing: |
    I'll <operation> <item_id> at <target> scope. Confirm?
  llm_dependent_content: [failure_explanation]
  dependency_order:
    - inspect_status returns current state regardless of dependency
    - retry only valid when state is failed
    - abort only valid when state is in_progress or queued
```

The dependency-order list captures real constraints from the runtime: `retry` is not a no-op against a `completed` item, it is invalid. The agent's behavior changes accordingly -- when the user asks to `retry` an item the agent checks state first.

The shape is identical to the single-area settings example above. Only the attachment point differs (this config lives in `references/transformation.md`; the settings config lives at the parent document's top level). See `area-ownership.md` for the full attachment-pattern catalog.

## Audit hooks

A document that declares an `area_config:` block (at any attachment point) can be mechanically checked against:

- `state_terms` (if present) contains no duplicates and uses the same casing convention throughout.
- `operations` (if present) contains no duplicates.
- `scope_axes` (if present) name each axis distinctly; values within an axis are unique.
- `dependency_order` (if present) does not reference operations or capabilities that do not exist on the area.
- `canonical_phrasing` (if present) uses placeholders that resolve from context (each placeholder names a field the runtime supplies).

These checks are mechanical; they do not require human judgment. An audit script consuming the area_config record can assert each invariant. The semantic check -- whether the named state vocabulary actually matches the runtime -- is human judgment and not mechanically checkable from the document alone.
