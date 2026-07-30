# Content Authoring

> Content-shape reference owned by the **md-domain** skill (folded in from the former
> standalone `content-authoring` skill via `md-authoring`). Loaded on demand from `md-domain`'s
> index, at `authoring-patterns/content-authoring.md`. Answers the orthogonal question to
> placement: *how* a fact should be shaped. The placement question (*where* it lives) belongs to
> `cohesion-principles.md`.

The discipline of authoring LLM-facing documents: deciding how a document's content should be shaped so Claude reads it the way you intended.

A document Claude reads is composed of three surfaces: a **YAML header** (frontmatter) at the top, **markdown text** in the body, and **embedded YAML** (fenced yaml blocks inside that body). Traditionally documents are written entirely as markdown text. For LLM-facing content, both YAML surfaces are often more efficient: they express meaning through structure and tag-names that prose has to spell out at length. A well-authored document mixes all three.

The YAML body below is the load-bearing surface; the prose above is orientation. Deeper material loads on demand from the co-located docs this reference points at. This document uses three embedded YAML blocks -- the `reference_skill:` contract, the `facts:` content, and the `references:` pointers -- as a worked example of the typed-unit composition discipline it teaches. Each block gets its own section heading and a one-line prose context (the per-block priming discipline named in `wrap_yaml_in_markdown`).

## Contract

The reference-skill contract -- what this skill claims to be.

```yaml
reference_skill:
  _schema_version: "1"
  identity: Theory and pattern catalog for authoring LLM-facing documents -- the three content-form surfaces (yaml header / markdown text / embedded yaml) and the analysis framework for choosing between them.
  scope:
    covers:
      - foundational principles the framework rests on (audience_claude, structure_asserts, area_ownership)
      - the three content-form surfaces and their distinct audiences and parsers (yaml_header_surface, markdown_text_surface, embedded_yaml_surface)
      - the analysis framework for placing a piece of content in the right surface (analysis_framework)
      - disciplines for using the surfaces well (frontmatter_is_metadata_not_content, wrap_yaml_in_markdown, typed_unit_composition)
      - recurring document-level patterns loaded on demand from references/
    excludes:
      - skill-type contracts and per-type schemas
      - audit machinery and finding taxonomies
      - general writing tasks unrelated to LLM-facing documents
```

## Facts

The facts this skill teaches, grouped by category (foundations, surfaces, decisions, disciplines).

```yaml
facts:

  # --- foundations -----------------------------------------------------

  - id: audience_claude
    category: foundations
    summary: The audience for these documents is Claude at runtime, not a human reading the source; structure aids Claude's comprehension while prose is what Claude generates on demand for the human user.
    keywords: [audience-claude, llm-facing, runtime context, not human documentation, structure aids comprehension]
    detail: |
      A document written for human ergonomics first leans prose-heavy and loses the routability
      and assertion-carrying that structure provides. The reader of a SKILL.md, a CLAUDE.md, or
      an in-skill reference doc is Claude at runtime -- author for that reader. Human-facing
      prose is what Claude produces on demand from the structured content; it is not what the
      source files should contain by default.

  - id: structure_asserts
    category: foundations
    summary: An embedded YAML record list carries an implicit assertion that prose cannot -- every entry conforms to the record shape -- and that assertion is auditable, routable, and visible.
    keywords: [structure asserts, implicit assertions, record shape, list of records, contract floor, completeness signal]
    detail: |
      Consider two encodings of "anti-patterns this skill should warn against." A prose form
      ("common anti-patterns: skipping validation, using the wrong tool, treating the threshold
      as a verdict") lists three items and says nothing about completeness or shape. A future
      reader has no way to know whether "skipping validation" is fully characterized or just
      named.

      An embedded YAML form (`anti_patterns:` list, one record per pattern, each carrying id /
      name / why_it_seems_right / why_it_is_wrong / alternative) makes a structural commitment
      every entry must satisfy. An entry missing a field is visibly incomplete. The structure
      asserts completeness; the prose does not.

      The assertion's value scales with record count. A single-entry list rarely earns the
      structural overhead.
    example:
      input: A skill collects six gotchas. Encoded as prose, the document reads "watch out for X, Y, Z..." -- a future audit cannot check whether each gotcha names its detection signal.
      output: Encoded as `gotchas:` records with id / summary / detection, the audit can mechanically verify every gotcha carries a detection field; missing fields surface as incomplete entries.

  - id: area_ownership
    category: foundations
    summary: An "area" is the unit of document ownership -- a named, scope-bounded body of work that a document declares itself the canonical source of truth for. Ownership is expressed structurally through four elements -- identity, scope, area_config (the runtime contract), and an optional sub-area registry when the area decomposes.
    keywords: [area, area ownership, canonical source, ownership declaration, scope, area config, runtime contract, sub-areas, multi-area, single-area]
    detail: |
      An area is the unit of document ownership -- a named, scope-bounded body of work,
      knowledge, conventions, vocabulary, operations, or rules. A document owns an area
      when it declares itself the canonical source of truth for that area. Other documents
      defer; routing tools pick the right document by matching questions to the areas
      each document claims to own. The exclusion clause in `scope` is load-bearing -- it
      is what prevents adjacent areas from drifting into the document's ownership claim.

      Ownership is expressed structurally through four elements:

      1. **Identity** (required) -- one sentence stating what the document is the source
         of truth for.
      2. **Scope** (required) -- `covers` and `excludes` lists declaring the boundaries.
      3. **Area config** -- the runtime contract for the area (state vocabulary, operations,
         scopes, canonical phrasing, LLM-dependent fields, dependency order). Attaches at
         the area level for single-area documents and per sub-area for multi-area documents.
         See `area-config.md` for the six-field schema.
      4. **Sub-area registry** (optional) -- a list of named sub-areas with routing keywords
         and per-sub-area reference docs, used when the area decomposes.

      The deep treatment -- the three attachment patterns for area_config, the registry
      schema, the greeting menu derivation, single-area vs multi-area, audit hooks, worked
      examples -- lives in `area-ownership.md`.
    example:
      input: A document is being authored to own "the authentication system." The author needs to express ownership in the document.
      output: |
        Identity: "Source of truth for the authentication system."
        Scope: `covers:` lists what's in (login flows, token management, password reset); `excludes:` lists what's adjacent but out-of-scope, ideally naming where the excluded ownership lives.
        Area config: the six-field runtime contract. If single-area, attached at the document top level; if multi-area, per sub-area in each per-sub-area reference doc.
        Optional sub-area registry: if the area decomposes (login, token, password reset), each gets a registry entry with keywords and a reference. See `area-ownership.md` for the full mechanics.

  # --- surfaces ---------------------------------------------------------

  - id: yaml_header_surface
    category: surfaces
    summary: The YAML header (frontmatter) is sparse metadata between `---` fences at the top of the file; the harness parses it on every document discovery.
    keywords: [frontmatter, metadata, fence, name field, description field, skill-type, harness routing, discovery cost, first surface]
    shape: YAML between `---` fences at the very top of the file.
    parser: The Claude Code harness on document discovery.
    audience: The harness machinery first (description-as-trigger, skill-type-as-contract-selector, name-as-identity); a one-glance human scan second.
    fits_when:
      - the content is metadata about the document itself (name, type, trigger condition, harness flags)
      - the set of keys is fixed and known
      - the field is consumed at discovery or by a one-glance scan
    avoid_when:
      - the content is something a reader consults repeatedly during work
      - the shape is open-ended -- load-bearing content belongs in the body
    detail: Frontmatter is the document's metadata surface. Keep it small; load-bearing content belongs in the body, not the header.

  - id: markdown_text_surface
    category: surfaces
    summary: Markdown text is the natural-language surface in the document body -- prose, headings, lists, code fences, tables -- with no implicit parser; it carries no structural assertion.
    keywords: [prose, narrative, identity sentence, orientation, headings, tables, code fences, priming, natural language, second surface]
    shape: Prose, headings, lists, code fences, and tables in the document body.
    parser: None implicit -- the reader (LLM or human) does the parsing.
    audience: A reader being primed for what comes next, navigating by heading, or consuming small data where readable column layout dominates.
    fits_when:
      - the content is naturally narrative (identity sentence, orientation paragraph, single-paragraph explanation)
      - the content's job is priming the reader before structured content follows
      - the content is a small lookup table where readable column layout dominates and the reader is not routing by keyword
      - the content is a hard rule expressed as a single sentence
    avoid_when:
      - the content is a list of items that share a shape (use embedded YAML)
      - the content is an ordered sequence of operations (use the actions pattern in embedded YAML)
    detail: Markdown text primes the reader. It carries identity, orientation, and pointers. It does not carry the load-bearing contract.

  - id: embedded_yaml_surface
    category: surfaces
    summary: Embedded YAML is one or more fenced yaml blocks inside the markdown body, carrying load-bearing structured data the LLM reads as data rather than prose.
    keywords: [fenced yaml, structured data, records, lookup tables, indexes, contract data, schema-validated, load-bearing, routable, third surface]
    shape: One or more fenced yaml blocks inside the markdown body. Each block is one YAML document; each top-level key in a block is a typed unit validated by its schema.
    parser: A YAML parser at consumption time. Optionally validated against a schema when one exists for the root key.
    audience: The LLM at runtime; audit tooling when the YAML has a schema; a human reviewer second.
    fits_when:
      - the content is a list of records that share a shape (anti-patterns, capabilities, rules, steps, criteria, gotchas)
      - the content is contract data the reader consumes mechanically
      - the content is a lookup table the LLM routes by key or by a keyword cluster
      - the content is an ordered sequence of operations the LLM must execute in order
      - the content benefits from an implicit assertion the prose cannot carry
    avoid_when:
      - the content is a single sentence or paragraph that does not decompose into discrete records
      - the content is conversational priming meant to set context before the next section
    detail: Embedded YAML carries the load-bearing content. It is the place for records, lookup tables, contracts, and step sequences.
    example:
      input: A SKILL.md author has six related anti-patterns to encode, each with a name, a rationalization, a failure mode, and an alternative.
      output: Encode them as an embedded YAML `anti_patterns:` list with one record per pattern (id, name, why_it_seems_right, why_it_is_wrong, alternative). The structure asserts every entry is complete; a markdown bullet list would not.
    gotchas:
      - Plain (unquoted) YAML scalars fail to parse when they contain backticks. Use double-quoted strings for short content or YAML folded block scalars for multi-sentence content.
      - Schema-flexible content forced into YAML is anti-shape. A single narrative paragraph broken into three block-scalar fields (description / summary / detail) is not better than the paragraph -- it manufactures structure without the assertion that justifies the overhead.

  # --- decisions ---------------------------------------------------------

  - id: analysis_framework
    category: decisions
    summary: For any piece of content you are encoding, walk a fixed decision sequence to pick its surface; the default for LLM-facing content is structured, and prose is the documented exception.
    keywords: [decision framework, where does this go, choose a surface, default to structured, when in doubt, walk down the list]
    detail: Walk the decision_sequence in order; stop at the first match. If nothing matches, the bias rule applies.
    decision_sequence:
      - q: Is this metadata about the document itself (name, type, trigger, harness flags)?
        then: YAML header.
      - q: Is this a list of records that share a shape?
        then: Embedded YAML. Structure asserts each entry conforms; a markdown bullet list does not.
      - q: Is this contract data, a lookup table the LLM will route over, or an ordered step sequence?
        then: Embedded YAML.
      - q: Is this a small lookup table where readable column layout dominates and routing is not needed?
        then: Markdown table inside markdown text.
      - q: Is this naturally narrative -- an identity sentence, orientation paragraph, priming for what comes next?
        then: Markdown text.
      - q: Still unclear -- would the structure carry an assertion the prose cannot?
        then: Default to embedded YAML.
    bias: The default for LLM-facing content is structured. Prose is the exception, used when the content is naturally narrative or hierarchy carries no meaning over flat prose. When in doubt, choose structure.
    gotchas:
      - Stuffing load-bearing content into frontmatter inflates discovery cost. If a field is consulted during work, it belongs in the body.
      - A markdown bullet list of items that share a shape (anti-patterns, criteria, capabilities) carries no schema assertion. The reader cannot tell whether the list is complete or whether each item is fully characterized.
      - A markdown numbered list for ordered operations drifts under edits and carries no structural primitives. Use the actions pattern (embedded YAML steps).

  # --- disciplines --------------------------------------------------------

  - id: frontmatter_is_metadata_not_content
    category: disciplines
    summary: Frontmatter is for metadata the harness or a quick scan consumes; load-bearing content -- even when structured -- belongs in the body, in embedded YAML.
    keywords: [frontmatter discipline, metadata only, no load-bearing fields, discovery cost, body vs header]
    detail: |
      Frontmatter is parsed on every document discovery. Putting load-bearing content there
      inflates discovery cost and conflates metadata with content; both lose. The frontmatter
      surface is for a fixed set of known keys, each consulted at discovery (name, description,
      skill-type, harness flags) or by a one-glance human scan (author, _schema_version).
      Everything else -- contracts, lookup data, records -- belongs in the body.

  - id: wrap_yaml_in_markdown
    category: disciplines
    summary: Every embedded YAML block lives inside a markdown wrapper -- a section heading and an optional one-line context primes the reader for what the block carries. The discipline applies at two scales -- the document grain and the per-block grain.
    keywords: [embedded not pure yaml, markdown wrapper, orientation surface, identity sentence, priming, wrapper rule, per-block heading, one-line context, bare yaml block]
    detail: |
      The principle has two scales:

      **Document grain.** The SKILL.md (or any document) wraps its embedded YAML in markdown.
      The document-level wrapper carries the identity sentence (one-line statement of what the
      document owns), the orientation paragraph (priming for what the YAML carries), behavioral
      guardrails (hard rules expressed as prose), and pointers to related documents.

      **Block grain.** When a document has multiple embedded YAML blocks (typed-unit
      composition), each block gets its own wrapper too: a section heading (`## Contract`,
      `## Facts`, `## Conditional Loading`) and an optional one-line prose context above the
      fence. The heading anchors the reader's navigation; the one-liner primes them for what
      the block carries.

      Both wrappers are short -- a heading plus one line per block, plus a short document-level
      intro. Together they make a multi-block document scannable; without them a long file is
      a wall of fenced YAML.
    gotchas:
      - A pure-YAML SKILL.md has no markdown text between the frontmatter close and the first fenced YAML block. The document-level orientation surface is gone; the reader lands cold.
      - A bare YAML block (no markdown section heading and no one-line context above the fence) is an anti-pattern at the block grain even when the document-level wrapper exists. The reader cannot tell where they are in the document or what the block is for.
      - When markdown above an embedded YAML block re-states what the YAML carries, the reader pays twice. The wrapper should prime; the YAML should carry.

  - id: typed_unit_composition
    category: disciplines
    summary: A document's embedded YAML is one or more "typed units" -- top-level keys with a registered schema -- and the author chooses whether units share a block, get their own blocks, or both.
    keywords: [typed unit, yaml unit, unit registry, multi-block, multi-unit, encoding flexibility, portable surfaces, schema registry, layout choice]
    detail: |
      A typed YAML unit is a top-level key with a registered schema (e.g. `reference_skill`,
      `references`, `facts`). The schema layer validates each unit independently. A document
      body contains one or more fenced yaml blocks; each block is one YAML document containing
      one or more top-level keys; each recognized key is a unit validated by its schema.

      The author picks the physical encoding from three equivalent shapes:

      - one block, one unit (compact -- works for small documents)
      - separate blocks per unit (visual separation; portable units stand alone)
      - one block with multiple top-level keys (merge units that share lifecycle)

      All three are semantically equivalent. The schema sees the union of typed units
      across all blocks; layout is presentation.

      Portable units (e.g. `references`, `facts`) may live either as a sub-field of a
      skill-type unit (the original convention) or as their own top-level unit elsewhere
      in the document. Both are valid; the schema accepts whichever the author chooses.
    example:
      input: A reference-skill grows past 200 lines. The `facts:` list is conceptually distinct from the contract identity but sits nested inside the `reference_skill:` block, making the contract definition hard to see at a glance.
      output: Extract `facts:` to its own fenced yaml block. The `reference_skill:` block now carries just identity / scope (small, visible at the top); the `facts:` block carries the content (the bulk). Two blocks, two units, equivalent semantic content, easier to navigate.
    gotchas:
      - More than one skill-type unit in a document signals mixed-type drift (e.g. `reference_skill` and `discipline_skill` together, in any layout). Portable units like `references` and `facts` are NOT skill-type roots and coexist freely with any skill-type unit.
      - Splitting units across blocks does not partition validation -- the schema sees every recognized unit in every block. Do not expect "this block is private" semantics.
      - Existing documents that keep portable units nested inside their skill-type unit continue to validate without change. Migration to a separate block is optional, not required.
```

## Conditional Loading

Pointers to deeper material loaded on demand by keyword match.

```yaml
references:
  - id: area_ownership
    path: area-ownership.md
    keywords: [area definition, ownership expression, identity, scope, covers excludes, sub-area registry, single-area, multi-area, audit hooks, ownership composition]
    summary: Defines what an area is, what document ownership of an area means, and how ownership is expressed structurally through identity + scope (single-area) and an optional sub-area registry (multi-area). Worked example with the authentication-system document; audit hooks for ownership declarations.

  - id: three_surfaces
    path: three-surfaces.md
    keywords: [parser model, surface comparison, structure asserts deep, wrapper rule deep, common authoring mistakes, per-surface analysis]
    summary: Deep treatment of each of the three surfaces -- parser, audience, fits-when / does-not-fit-when -- plus the structure-asserts deep dive, the wrapper rule with worked examples, and common authoring mistakes.

  - id: actions_pattern
    path: actions-pattern.md
    keywords: [actions pattern, ordered operations, multi-step recipe, steps schema, capture, tell_user, facade script, narration discipline, audit hooks]
    summary: The ordered-list-of-operations shape -- a YAML `actions:` schema carrying steps with tool / capture / tell_user fields, the facade-script convention for 3+ co-occurring tool calls, the narration discipline for single tool calls, and worked examples.

  - id: area_config
    path: area-config.md
    keywords: [area config, runtime contract, state_terms, operations, scope_axes, canonical_phrasing, llm_dependent_content, dependency_order, area vocabulary contract, single-area attachment, per-sub-area attachment]
    summary: The schema for an area's runtime contract -- the six fields (state_terms, operations, scope_axes, canonical_phrasing, llm_dependent_content, dependency_order) that govern how the agent and user interact with entities in the area. Same shape whether attached at the area level (single-area document) or per sub-area (multi-area document). Worked examples for both attachment cases.

  - id: query_tool_pattern
    path: query-tool-pattern.md
    keywords: [query tool facade, catalog lookup, YAML output discipline, did-you-mean, spelling discovery, gazetteer, inventory, CLI mode pattern]
    summary: The facade pattern for wrapping a catalog with a single CLI offering multiple lookup modes; the YAML-output discipline; did-you-mean records on misses; the spelling-discovery agent behavior the pattern enables.
```
