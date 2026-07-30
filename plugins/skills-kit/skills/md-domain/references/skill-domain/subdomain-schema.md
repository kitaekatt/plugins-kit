# Sub-domain config schema

When a capability-skill or domain-skill decomposes into sub-areas, each sub-area has a runtime configuration that contracts the agent's and designer's shared vocabulary. The schema names the structural fields a sub-area carries so that audit tooling can verify each sub-area declares what it operates on, what verbs it supports, what scopes are valid, and how the agent should phrase requests in canonical terms.

Use this schema when:

- A capability-skill has 2+ sub-areas with distinct runtime states (e.g. one sub-area operates on "draft" / "scheduled" / "live"; another operates on "open" / "in-review" / "closed").
- The sub-area has canonical vocabulary the agent must use verbatim rather than paraphrase from the user's loose phrasing.
- Designer and agent need a single source of truth for "what does this sub-area let me do, and in what shapes".

Do not use for single-area capability-skills; the sub-area record is overhead when there is no decomposition to disambiguate.

## Schema fields

A sub-area config is a record carrying the following fields. All fields except `name` are optional; declare only the fields the sub-area actually uses.

### name (required, string)

Canonical sub-area identifier. Stable across versions; used as the dispatch token in argument-dispatch invocations and as the index key in the sub-domain registration index.

### state_terms (optional, list of strings)

Canonical state vocabulary the agent uses verbatim when discussing entities in this sub-area. The agent re-phrases user-loose language ("the in-progress ones") into canonical terms ("the entities in `pending_review` state") to keep usage consistent with the rest of the system.

State terms are the names the system uses internally; they are not free to be improvised. When a user asks about a state with a different label, the agent translates to the canonical term in the response.

### operations (optional, list of strings)

The verbs the sub-area supports -- the operations a user or agent invokes against entities in this sub-area. Each operation is one verb (or short verb phrase). Define each operation in one sentence; details live in the per-capability record or in the sub-area's reference doc.

Operations are distinct from capabilities. An operation is the verb shape ("transition", "approve", "rebuild"); a capability is the user-objective wrapping an operation with scope, inputs, and gotchas.

### scope_axes (optional, list of objects)

One or more axes that decompose the capability space within the sub-area. Each axis carries a `name` and a list of `values` it can take. Scope axes let the agent and the user disambiguate "I want to X" into "I want to X scoped to Y on axis Z".

Scope-inference rule for the agent: infer scope from context where possible. For broad scopes that touch many entities or that perform expensive operations, confirm with the user before running rather than assuming the broadest scope.

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

```yaml
canonical_phrasing: |
  I'll <operation> <entity_id> at <scope> scope. Confirm?
```

### llm_dependent_content (optional, list of strings)

Fields whose values are produced by an LLM rather than retrieved deterministically. Naming these explicitly helps the agent (and an audit) distinguish content that must be regenerated from content that is fetched verbatim. LLM-dependent fields are subject to canonical-phrasing rules; deterministic fields are not.

### dependency_order (optional, list of strings)

Ordering constraints among the sub-area's capabilities. When capability B depends on capability A having been executed, the dependency-order list names that constraint so the agent surfaces the prerequisite when the user asks for B in isolation.

The list shape: each entry names a constraint as a short sentence (e.g. "validate before transition", "rebuild index before query") or as a structured `{capability_id: prerequisites}` mapping when the dependencies form a small graph.

## Worked example: hypothetical settings-management sub-area

A capability-skill `/config-mgmt` has sub-areas for user-settings and project-settings. The user-settings sub-area config:

```yaml
subdomain_config:
  - name: user_settings
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

This config gives the audit and the agent a single source of truth: state vocabulary is fixed (no improvising "active" instead of "enabled"), operations are enumerated (no `purge` operation if it isn't on the list), and scopes are bounded (no operation runs against `all` settings without confirmation per the scope-inference rule).

The reference doc for `user_settings` describes each capability in detail; the sub-area config is the structural floor that the reference doc populates.

## Worked example: hypothetical content-pipeline sub-area

A capability-skill `/content-pipeline` has sub-areas for ingestion, transformation, and publication. The transformation sub-area config:

```yaml
subdomain_config:
  - name: transformation
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

## Schema extension on capability-skill

A capability-skill carries this configuration in an optional `subdomain_config:` list at root, alongside `capabilities:` and `external_capability:`. Each list entry is a sub-area record matching the schema fields above.

```yaml
capability_skill:
  _schema_version: "1"
  identity: ...
  external_capability: ...
  layering: ...
  capabilities:
    - id: ...
      ...
  subdomain_config:
    - name: subdomain-A
      state_terms: [...]
      operations: [...]
      ...
    - name: subdomain-B
      ...
  gotchas:
    - ...
```

The schema validator accepts the field as optional. Capability-skills with a single sub-area or with no canonical-vocabulary requirement may omit `subdomain_config:` entirely.

## Audit hooks

A capability-skill that declares `subdomain_config:` must satisfy:

- Each sub-area record has a `name` matching an entry in the sub-domain registration index (consistency between the index and the config).
- `state_terms` (if present) contains no duplicates and uses the same casing convention throughout.
- `operations` (if present) contains no duplicates.
- `scope_axes` (if present) name each axis distinctly; values within an axis are unique.
- `dependency_order` (if present) does not reference operations or capabilities that do not exist on the sub-area.

These checks are mechanical; they do not require human judgment. An audit script consuming the sub-area record can assert each invariant.
