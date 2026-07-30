# Area ownership

An "area" is the unit of document ownership. This doc defines what an area is, what it means for a document to own one, and how that ownership is expressed structurally in the document body. For the runtime contract carried by an area (state vocabulary, operations, scopes), see `area-config.md`.

## What is an area?

An **area** is a named, scope-bounded body of work, knowledge, conventions, vocabulary, operations, or rules. It is the "thing being owned" in a multi-document system. Areas have:

- A **name** that identifies the area in human and machine surfaces ("the authentication system", "dialog work", "the configuration pipeline").
- **Explicit scope boundaries** -- a `covers` list naming what's in-scope and an `excludes` list naming what's adjacent but out-of-scope.
- **A runtime contract** -- the `area_config:` block declaring state vocabulary, operations, scopes, and the other runtime details. See `area-config.md`.
- **Optional sub-areas** -- when the area is large enough, it decomposes into named slices that the parent document also owns. Each sub-area is itself an area in the recursive sense, with its own runtime contract.

An area is NOT a section heading, a folder, a markdown anchor, or a topic the document happens to discuss in passing. It is what the document declares itself responsible for being the canonical source of truth on.

## What does ownership mean?

A document **owns** an area when it declares itself the **canonical source of truth** for that area. Three consequences follow from ownership:

1. **Other documents defer** to the owning document for questions in the area. The owning document is where decisions, conventions, and runtime contracts live; other documents that touch the area reference back rather than duplicating.
2. **Routing tools use ownership** to pick the right document for a question. An agent presented with a user request matches the request against each candidate document's area declaration and picks the document that owns the matching area.
3. **The exclusion clause is load-bearing.** Without explicit exclusions, adjacent areas drift into the document's claimed scope; ownership erodes; routing decays.

Ownership is what makes a multi-document system navigable. Without it, every question requires guessing which document to load.

## How ownership is expressed

A document expresses ownership through four structural elements. Two are required for any owned area; the other two apply when ownership decomposes or has runtime detail.

### 1. Identity (required)

One sentence stating what the document is the source of truth for. Lives in the `identity:` field of the document's top-level typed unit. Read aloud, it answers "what does this document own."

```yaml
identity: Source of truth for the authentication system -- login flows, token issuance and refresh, and password reset.
```

The identity sentence is short, descriptive, and names the area. It is NOT a feature list, a goal statement, or a tagline.

### 2. Scope (required)

The boundaries of the owned area, declared as `covers` (in-scope) and `excludes` (out-of-scope) lists. Lives in the `scope:` field of the document's top-level typed unit.

```yaml
scope:
  covers:
    - login flow including session establishment
    - token issuance and refresh
    - password reset and recovery
  excludes:
    - authorization rules (owned by the policy-engine doc)
    - user-profile management (owned by the profile doc)
    - audit logging (owned by the observability doc)
```

The `excludes` list is what prevents adjacent areas from drifting into the document's ownership claim. Each excluded item ideally names where that ownership DOES live ("owned by ..."), so a reader who follows the exclusion can find the canonical source.

### 3. Area config (the runtime contract)

The runtime contract for the area -- state vocabulary, operations, scopes, canonical phrasing, LLM-dependent fields, dependency ordering. See `area-config.md` for the schema.

The runtime contract attaches at whatever the area-tree leaves are. There are three attachment patterns, picked by document shape:

#### Pattern 3a: Single-area inline

For documents that own ONE area (no sub-area decomposition), `area_config:` is a sibling field of `identity:` and `scope:` in the document's top-level typed unit.

```yaml
reference_skill:
  identity: ...
  scope: ...
  area_config:
    state_terms: [...]
    operations: [...]
    ...
```

#### Pattern 3b: Multi-area inline

For documents whose sub-areas are small enough to keep all their configs inline, the parent document carries `area_configs:` as a list -- each entry adds a `name` field identifying which sub-area the config belongs to.

```yaml
reference_skill:
  identity: ...
  scope: ...
  sub_areas:
    - name: ingestion
      description: ...
      keyword_cues: [...]
    - name: transformation
      description: ...
      keyword_cues: [...]
  area_configs:
    - name: ingestion
      state_terms: [...]
      operations: [...]
    - name: transformation
      state_terms: [...]
      operations: [...]
```

#### Pattern 3c: Multi-area extracted

For documents whose sub-areas are large enough to warrant per-sub-area reference docs, the registry's `reference:` field points at a doc that carries the sub-area's `area_config:` at its top level.

In the parent document:
```yaml
reference_skill:
  identity: ...
  scope: ...
  sub_areas:
    - name: transformation
      description: ...
      keyword_cues: [...]
      reference: references/transformation.md
```

In `references/transformation.md`:
```yaml
area_config:
  state_terms: [...]
  operations: [...]
  ...
```

The three patterns are equivalent semantically; layout is the author's choice based on document size and how independently each sub-area's content evolves.

### 4. Sub-area registry (optional)

When the owned area decomposes into multiple related sub-areas, the document extends ownership into named sub-areas via a structured registry. The registry is a list of records under a top-level `sub_areas:` key.

```yaml
sub_areas:
  - name: <canonical-sub-area-id>
    description: <one-sentence scope>
    keyword_cues: [phrase-1, phrase-2, alt-phrase]
    reference: <path/to/per-sub-area/doc.md>   # for Pattern 3c only
```

Field semantics:

- **`name`** -- canonical sub-area identifier. Stable across versions; used as the dispatch token in argument-dispatch invocations and as the index key for cross-references.
- **`description`** -- one sentence stating the sub-area's scope. Read aloud, it should answer "what does this sub-area cover."
- **`keyword_cues`** -- list of phrases that route a user request to this sub-area. The agent matches the user's request against the union of all sub-areas' keyword_cues and picks the best match.
- **`reference`** -- path to the sub-area's deeper documentation. Required when attachment pattern 3c is in use; omitted for patterns 3a and 3b (where the runtime contract is inline).

The structure asserts every sub-area is complete: each entry must carry name, description, and keyword_cues (with at least one cue), plus `reference` when pattern 3c is in use. A bullet list of sub-area names in prose carries none of these assertions.

Each sub-area still belongs to the parent document. Decomposition is internal organization, not a transfer of ownership -- a reader looking for "anything about authentication" still routes to this document; the sub-area registry then disambiguates internally.

## Greeting menu format

When the user invokes a document's owner with no follow-up request, the system can derive a greeting menu directly from the sub-area registry. The menu shape:

```
How can I help you with <area>?
 - <sub-area-A description> (`<invocation> <keyword>`)
 - <sub-area-B description> (`<invocation> <keyword>`)
 - ...

Or can I help you with something else?
```

Each menu line shows the sub-area's description and a representative keyword the user can use to dispatch directly. The menu lists every registered sub-area. It does not show operations tables, references, or other detail -- the menu is the entire response, and the user picks next.

The menu format is markdown text (priming the user); the underlying registry is embedded YAML (structured for routing). Both surfaces carry the same content, expressed at different audiences -- the menu for the human user, the registry for the agent's routing logic.

## Single-area vs multi-area ownership

A document is **single-area** when its ownership does not decompose. The expression uses elements 1, 2, and 3 (attachment pattern 3a -- inline `area_config:`). No sub-area registry. Common case for tightly-scoped reference docs.

A document is **multi-area** when its ownership extends to a sub-area registry. The expression uses all four elements -- identity, scope, area_config (per the registry's attachment pattern), and the sub-area registry itself. Common case for larger documents that own a body of work big enough to need internal navigation.

The transition from single-area to multi-area is a refactor signal: if a single-area document grows large enough that readers struggle to find the right section, the area has probably been decomposing implicitly already. Naming the sub-areas explicitly (by adding a registry and attaching area_configs per sub-area) makes the internal structure tractable.

## Worked example: project-management document

A `/project-mgmt` document owns "project management" with three sub-areas -- tickets, milestones, approvals -- with each sub-area's runtime contract extracted to its own per-sub-area reference doc (Pattern 3c).

**Identity + scope (in the parent document):**

```yaml
reference_skill:
  identity: Source of truth for project management -- ticket lifecycle, milestone planning, and approval routing.
  scope:
    covers:
      - ticket creation, triage, status transitions
      - milestone planning, dependency tracking, due-date management
      - approval routing, sign-off tracking, escalation
    excludes:
      - billing and invoice approval (owned by the finance doc)
      - vendor onboarding (owned by the vendor-mgmt doc)
      - personnel changes (owned by the HR doc)
```

**Sub-area registry (in the parent document):**

```yaml
sub_areas:
  - name: tickets
    description: ticket creation, triage, status transitions
    keyword_cues: [ticket, issue, bug report, task, triage]
    reference: references/tickets.md
  - name: milestones
    description: milestone planning, dependency tracking, due-date management
    keyword_cues: [milestone, sprint, release, deadline, schedule]
    reference: references/milestones.md
  - name: approvals
    description: approval routing, sign-off tracking, escalation
    keyword_cues: [approval, sign-off, review request, escalate]
    reference: references/approvals.md
```

**Derived greeting menu (rendered to the user on bare invocation):**

```
How can I help you with project management?
 - ticket creation, triage, status transitions (`/project-mgmt tickets`)
 - milestone planning, dependency tracking, due-date management (`/project-mgmt milestones`)
 - approval routing, sign-off tracking, escalation (`/project-mgmt approvals`)

Or can I help you with something else?
```

**Per-sub-area runtime contract (in `references/tickets.md`):**

```yaml
area_config:
  state_terms: [open, triaged, in_progress, blocked, resolved, closed]
  operations: [create, triage, assign, transition, comment, close]
  scope_axes:
    - name: target
      values: [single, batch]
  canonical_phrasing: |
    I'll <operation> ticket <id> at <target> scope. Confirm?
  llm_dependent_content: [triage_recommendation, summary]
  dependency_order:
    - triage before assign
    - resolve before close
```

The document owns one area (project management) which decomposes into three sub-areas. Each sub-area has its own runtime contract declared in its reference doc per pattern 3c.

## Worked example: routing across documents

A user asks "how do I escalate this approval?" The agent matches against each candidate document's area declaration:

- `/project-mgmt` -- `scope.covers` lists "approval routing, sign-off tracking, escalation"; `sub_areas` has an `approvals` entry with `keyword_cues` containing "escalate". Match.
- `/finance` -- excluded by `/project-mgmt`'s exclusion clause ("billing and invoice approval -- owned by the finance doc"); not a match for THIS question.

The agent routes to `/project-mgmt`, then within `/project-mgmt` the `approvals` sub-area's keyword cue resolves the request to the approvals reference doc. Routing took two scope reads, not three full document reads.

## Audit hooks

A document that claims area ownership can be mechanically checked against:

**Ownership-level checks:**

- The `identity:` field is present and a single sentence.
- The `scope:` field is present with non-empty `covers` and `excludes` lists.
- Each `excludes` entry ideally names where the excluded ownership lives ("owned by X"), though this is judgment-flavored rather than mechanical.

**Sub-area registry checks (when present):**

- Each `sub_areas:` entry has all required fields (name, description, keyword_cues; reference required for Pattern 3c).
- `keyword_cues` contains at least one phrase per entry.
- `name` values are unique within the registry.
- When `reference:` is present, the path resolves to a file that exists on disk.
- Cross-entry `keyword_cues` overlaps are reported (not failures -- shared cues are legitimate when surrounding context disambiguates -- but a reviewer should see them).

**Area config attachment checks:**

- Pattern 3a: `area_config:` is a sibling of `identity:` and `scope:` in the top-level typed unit; no `sub_areas:` is present.
- Pattern 3b: `area_configs:` (plural) is a list; each entry has a `name` matching an entry in `sub_areas:`.
- Pattern 3c: each `sub_areas:` entry's `reference:` resolves to a file that carries its own top-level `area_config:` block.

The semantic check -- whether the document actually IS the canonical source for the named area -- is human judgment and not mechanically checkable.

## Composition with other patterns

Area ownership is the foundation; other document-shape patterns build on top:

- The **runtime contract schema** (`area-config.md`) is the per-area structural detail referenced by the area_config attachment.
- The **actions pattern** can apply within any area or sub-area where multi-step recipes fit.
- The **query-tool facade** can apply within any area or sub-area that has a catalog of entities to look up.
