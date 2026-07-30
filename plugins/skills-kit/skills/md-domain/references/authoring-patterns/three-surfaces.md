# The Three Content-Form Surfaces -- Deep Treatment

This is the deeper treatment of the three surfaces named in the parent SKILL.md. Read this when you are choosing between surfaces for a specific piece of content and want the full analysis, not the summary.

## Why three surfaces, not two

The earlier "yaml vs prose" framing collapsed two distinct YAML surfaces -- the frontmatter header and embedded YAML blocks -- into one. They have different parsers, different consumers, different cost models, and different rules for what belongs in them. The 3-way decomposition surfaces those differences and makes the decision auditable.

## Surface 1: YAML header (frontmatter)

**Shape.** YAML between `---` fences at the very top of the file.

**Parser.** The Claude Code harness on document discovery. Some tools that don't load a YAML library use a regex on the first read.

**Audience.** The harness first -- description-as-trigger, skill-type-as-contract-selector, name-as-identity, and any flags governing how the document loads. The reader's first glance second.

**Fits when** the content is metadata about the document itself: its name, its type, its trigger condition, the harness-level flags that govern how it loads. A fixed set of known keys, each consulted at discovery or by a one-glance human scan.

**Does not fit when** the content is load-bearing -- something a reader consults repeatedly during the work the document supports. Frontmatter inflates discovery cost when stuffed; load-bearing content belongs in the body in embedded YAML.

**Worked example.** A SKILL.md's `description:` is frontmatter (metadata, used by the harness for routing). A SKILL.md's `criteria:` block is NOT frontmatter (load-bearing content consulted during the audit); it lives in embedded YAML in the body.

## Surface 2: Markdown text

**Shape.** Prose, headings, lists, code fences, and tables in the body of the document.

**Parser.** None implicit. The reader (LLM or human) does the parsing.

**Audience.** A reader being primed for what comes next, navigating by heading, or consuming small data where readable column layout dominates.

**Fits when:**

- The content is naturally narrative -- an identity sentence, an orientation paragraph, a single-paragraph explanation that does not decompose into discrete records.
- The content's job is priming the reader before they consume structured content that follows.
- The content is a small lookup table where readable column layout dominates and the reader is not routing over it by keyword (markdown table form).
- The content is a hard rule expressed as a single sentence ("Never do X without Y."). Single-sentence rules do not need a record shape.

**Does not fit when:**

- The content is a list of items that share a shape. Markdown bullet lists carry no schema assertion; an embedded YAML record list does.
- The content is an ordered sequence of operations the LLM must execute. Markdown numbered lists drift under edits and carry no structural primitives for `capture` / `tell_user` / `wait_for_user`.

**Worked example.** A skill's one-sentence identity statement ("Knowledge hub for the dialogue system -- pipeline, animation, moods.") is markdown text. The skill's sub-area registry -- a list of records the LLM routes over by keyword cluster -- is NOT markdown text; it lives in embedded YAML, typically in a reference doc named for the registry.

## Surface 3: Embedded YAML

**Shape.** Fenced ` ```yaml ` blocks inside the markdown body.

**Parser.** A YAML parser at consumption time. Optionally validated against a schema when one exists for the root key.

**Audience.** The LLM consuming the document at runtime. Audit tooling when the YAML has a schema. A human reviewer second.

**Fits when:**

- The content is a list of records that share a shape (anti-patterns, capabilities, rules, steps, criteria, gotchas, examples).
- The content is contract data -- a schema-validated declaration the reader consumes mechanically.
- The content is a lookup table the LLM routes by key or by a keyword cluster.
- The content is an ordered sequence of operations the LLM must execute in order (the actions pattern).
- The content benefits from an implicit assertion the prose cannot carry (see "Structure asserts" below).

**Does not fit when:**

- The content is a single sentence or paragraph that does not decompose into discrete records. Forcing it into `description: |` plus `summary: |` plus `detail: |` is not better than the paragraph.
- The content is conversational priming meant to set context before the next section. The YAML reads cold without the priming.

**Worked example.** Consider a skill that ships a quality-evaluation contract -- a set of criteria, a taxonomy of findings, ordered procedures, and remediation buckets. The SKILL.md embeds those as a single YAML contract block. Each entry in `criteria:` shares the same shape (id, name, keywords, summary, severity, detail). The structure asserts every entry is a complete criterion; a markdown list under "## Criteria" would not.

## Structure asserts -- the deep version

The phrase "structure asserts" names a property of embedded YAML record lists that prose does not have. Consider two encodings of "anti-patterns this skill should warn against."

**Prose form:**

> Some common anti-patterns: skipping the validation step, using the wrong tool, treating the threshold as a verdict.

**Embedded YAML form:**

```yaml
anti_patterns:
  - id: skip_validation
    name: Skipping the validation step
    why_it_seems_right: ...
    why_it_is_wrong: ...
    alternative: ...
  - id: wrong_tool
    name: Using the wrong tool
    why_it_seems_right: ...
    why_it_is_wrong: ...
    alternative: ...
```

The prose form lists three items. It says nothing about whether the items are complete, whether they share a shape, or whether anything is missing. A future reader has no way to know whether "skipping the validation step" is fully characterized or just named.

The YAML form makes a structural commitment: every entry must have id, name, why_it_seems_right, why_it_is_wrong, alternative. An entry missing a field is visibly incomplete. The structure asserts completeness; the prose does not.

The assertion matters when:

- The content will be audited (a schema can check the assertion mechanically).
- The content will be routed over (keyword clusters per record enable navigation).
- The content has more than one entry -- the assertion's value scales with item count. A single-entry list rarely earns the structural overhead.

## The wrapper rule

Embedded YAML lives inside a markdown wrapper. Don't author pure-YAML documents.

The markdown wrapper carries:

- The identity sentence (one-line statement of what the document owns).
- The orientation paragraph (priming for what the embedded YAML carries).
- Behavioral guardrails (hard rules expressed as prose).
- Pointers to related documents.

The embedded YAML carries:

- The load-bearing contract or data.
- Records the reader routes over or validates against a schema.

Collapsing the markdown wrapper into the YAML loses the priming surface. A reader landing on a pure-YAML SKILL.md gets cold structured data with no place for "read this first" context. The wrapper does not need to be long -- a two-paragraph identity-plus-orientation, a fenced YAML block, and a Conditional Loading section is a complete document. The cost is small; the priming it provides is not.

## Common authoring mistakes

- **Stuffing load-bearing content into frontmatter.** Frontmatter inflates discovery cost. If a field is consulted during work, it belongs in the body.

- **Authoring a pure-YAML SKILL.md.** Loses the orientation surface. The wrapper is cheap; the priming it provides is not.

- **Bullet-list anti-patterns or criteria or capabilities in markdown.** No schema assertion, no keyword routing, no audit hook. If you have a list of records that share a shape, use embedded YAML.

- **Markdown numbered list for ordered operations.** Drifts under edits; carries no `capture:` / `tell_user:` / `wait_for_user:` structural primitives. Use the actions pattern (embedded YAML `steps:`).

- **Backticks inside embedded YAML strings.** Plain YAML scalars fail to parse with backticks. Use double-quoted strings for short content or YAML folded block scalars (`>-`) for multi-sentence content.

- **Schema-flexible content forced into YAML.** A single narrative paragraph encoded as `description: |` plus `summary: |` plus `detail: |` is not better than the paragraph. Use embedded YAML when the content has a shape worth asserting; not as a stylistic default.

- **Embedded YAML duplicating its markdown wrapper.** When a markdown section above an embedded YAML block re-states what the YAML carries, the reader pays twice. The wrapper should prime; the YAML should carry. Each surface does its own job.
