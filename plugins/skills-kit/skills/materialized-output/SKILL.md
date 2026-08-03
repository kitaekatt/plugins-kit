---
_schema_version: 1
name: materialized-output
author: christina
skill-type: pattern-skill
description: Use when designing a tool that produces a materialized insight from deep scans or inference over project data. Do NOT use for one-shot print-and-exit scripts.
---

# Materialized Output

A pattern-skill for tool design. Teaches recognition of when a tool should produce a structured artifact (a *materialized insight*) instead of an ad-hoc answer, and the discipline (*insight engineering*) of building tools that way by default.

The primary contribution is the mental model — once you recognize the shape, the design implications follow.

## Contract

```yaml
pattern_skill:
  _schema_version: "1"
  identity: Pattern skill that names the materialized-insight shape and the insight-engineering discipline of producing one by default.
  scope:
    covers:
      - recognizing when a deep scan or inference should emit a materialized insight
      - distinguishing a materialized insight from metadata, a cache/index, and a plain build artifact
      - choosing a refresh discipline as part of the design
    excludes:
      - one-shot print-and-exit script design
      - the mechanics of any particular scanner, parser, or build system
      - schema authoring for the emitted artifact
  patterns:
    - id: materialized-insight
      name: Materialized insight
      keywords: [materialized insight, insight engineering, deep scan, refresh discipline, structured artifact, precomputed reasoning, derived artifact, staleness detection]
      problem: Expensive reasoning over project data is recomputed per query, so every future consumer pays the full scan again and the reasoning leaves nothing behind.
      mechanic: Shape the reasoning product around emitting a structured artifact on disk -- computed from canonical project data, carrying inferences and lineage the source does not, refreshed by an owning build step when its sources change -- and let consumers do a file read plus a lookup.
      why: The deep scan is paid once per source change rather than once per query, and the reasoning becomes testable, queryable in unanticipated ways, and composable with other insights instead of dying in a transient stdout answer.
      apply_when:
        - signal: The reasoning is expensive -- a deep scan, model inference, or multi-pass analysis.
          example: An LLM pass over every module to summarize what each one does.
        - signal: Multiple downstream tools or workflows need the same result.
          example: A symbol reference map consumed by an IDE, a refactoring tool, and a dead-code report.
        - signal: The result is stable enough across runs that staleness detection is tractable.
          example: A resolved config graph whose inputs are a known set of layered files, so any write to them marks it stale.
      do_not_apply_when:
        - signal: The artifact would be metadata -- a descriptor about the data rather than a reasoned statement about the project.
          counter_example: A column-type listing or file annotation table; it describes shape, it infers nothing.
        - signal: The artifact would be a cache or index -- a restructuring of the source with no reasoning added.
          counter_example: A sorted key-to-offset map over the same records; the canonical source already carries every fact in it.
        - signal: The artifact is an ordinary deterministic build output whose value is the data, not encoded reasoning.
          counter_example: A compiled binary or a minified bundle -- a build artifact, but not an insight.
        - signal: Exactly one consumer needs the result, and no second consumer is in sight.
          counter_example: A one-off migration script that scans once to decide its own next step; inline the scan.
        - signal: Staleness cannot be detected, so a wrong answer would outlive its validity silently.
          counter_example: An insight derived from an external system that reports no change signal -- consumers keep reading a confidently wrong record.
        - signal: The scan is cheap enough that a lookup and a recompute cost about the same.
          counter_example: A glob over a few dozen files; the refresh discipline costs more than the scan it removes.
      examples:
        - title: Documentation search index
          before: Each consumer re-walks the doc tree to resolve headings, anchors, and cross-doc links.
          after: A pre-commit-refreshed index of headings, anchors, and validated links; the search UI and link checker each do a dict lookup.
        - title: Layered config resolution
          before: Debugging "why does this key have this value" means manually replaying defaults and overlays by hand.
          after: The config build step emits the resolved graph plus per-key provenance; deploy tooling and linters read it directly.
        - title: Throwaway deep scan converted
          before: A minutes-long script recomputes the same reference map on every invocation and prints it.
          after: The script's producer emits a structured reference map on commit; the script becomes a lookup over it.
```

## Materialized insights

**Materialized insights** (formal: *structured calculated insights on top of project data*) are auto-generated, structured artifacts produced by deep scans plus reasoning over canonical project data, stored on disk, refreshed when their canonical sources change, and consumed on demand by tooling and developers as fast lookups in place of re-running the underlying scan.

A materialized insight is distinguished from:

- **Metadata** — descriptors *about* data (column types, file annotations). Materialized insights are *about* the project; they are not descriptors.
- **A cache or index** — restructures data without adding reasoning. Materialized insights add inferences, lineage, and relationships that the canonical source does not carry.
- **A build artifact in general** — materialized insights are a specific kind of build artifact whose value is the reasoning encoded alongside the data.

Each materialized insight has a *refresh discipline* — the rule for staleness detection and rebuild, typically owned by a build step or hook.

### Generic examples

- **A search index over a documentation tree** — gathers headings, anchors, structural metadata, and cross-doc references. Refreshed on doc save or pre-commit hook. Consumers: search UI, broken-link checker, "where is X documented" lookups. Adds reasoning beyond the raw doc files (anchors-resolved-against-headings, link validity).
- **A pre-expanded config graph showing variant lineage** — walks a layered config tree (defaults / per-environment overlays / per-cluster overlays) and emits the resolved post-expansion graph plus per-key provenance (which file contributed which value, in which order). Refreshed by the config build step. Consumers: deployment tooling, config-validation linters, "why does this key have this value" debugging.
- **A precomputed reference map for a codebase** — walks source files and emits `{symbol → defined-at, called-from, imports}` plus reasoned-about aggregates (unused symbols, cycle detection). Refreshed on commit or as part of the build. Consumers: IDEs, refactoring tools, dead-code reports.
- **A model-derived summary cache** — runs an LLM once over a body of work to produce a structured "what does each module do" or "what does each PR change" record. Refreshed when the underlying body changes. Consumers: navigation aids, onboarding docs, automated PR reviewers.
- **A pricing / rate-card lookup derived from vendor APIs** — periodically polls a third-party API and emits a flat, query-friendly table with timestamps and sourcing. Refreshed on schedule. Consumers: cost estimators, billing tools.

### Cross-references

- **Semantic layer** (data architecture, e.g. dbt, Cube, Looker) — usually denotes a query-time semantic model rather than a pre-materialized file, but the conceptual goal is the same: a reasoning layer above raw data.
- **Materialized view** (databases) — a query result stored physically and refreshed on schedule or demand. The shared "materialized" framing is intentional.
- **Derived artifact** (build systems) — output of a deterministic build step, regenerated when inputs change.
- **Feature store** (ML pipelines) — precomputed features stored for trivial lookup, computed via heavy ETL.
- **Forward-chaining cache** (knowledge graphs) — facts derived by running a reasoner once and storing the implications.

### When to introduce a new materialized insight

Reach for this pattern when:

1. The reasoning is expensive (deep scan, model inference, multi-pass analysis), and
2. Multiple downstream tools or workflows need the result, and
3. The result is stable enough across runs that staleness detection is tractable.

The cost of a materialized insight is its refresh discipline — every new artifact adds a build-step dependency that has to be wired into CI and developer workflows. The benefit is that every consumer becomes trivial; the deep scan is paid for once per source change, not once per query.

### When NOT to apply the pattern

Three cases where materializing costs more than it returns, each with its counter-example:

- **A single consumer, with no second one in sight.** Counter-example: a one-off migration script scans the tree once to decide its own next step -- inline the scan; the artifact would have exactly one reader, once.
- **Staleness that cannot be detected.** Counter-example: an insight derived from an external system that emits no change signal -- the record goes wrong silently and every consumer reads a confidently wrong answer past its validity.
- **A scan cheap enough that lookup and recompute cost the same.** Counter-example: a glob over a few dozen files -- the refresh discipline is more machinery than the scan it removes; just recompute.

Beyond these, the conceptual distinctions above are also exclusions: if the artifact would be metadata, a plain cache or index, or an ordinary build output, it is not a materialized insight and does not need one's ceremony.

## Insight engineering

**Insight engineering** is the practice of identifying questions the project asks repeatedly or expensively, then producing materialized insights that answer those questions cheaply for the future. It is the discipline that creates and maintains the project's stock of materialized insights.

The unit of insight engineering is not a one-off script. A one-off script that does a deep scan, prints an answer, and exits leaves no value behind for the next consumer — every future caller pays the same cost. Insight engineering converts that scan into a structured artifact with a refresh discipline so the next consumer pays only a lookup.

### Insights are produced as the byproduct of building reasoning products

The default mode for building any tool, script, or pipeline that performs deep scans or inference over project data is to emit a materialized insight as part of its operation — not as an optional optimization, but as how the reasoning product is shaped from the start. The reasoning the product performs is inherently valuable to the project beyond any single invocation; the materialized insight is the durable form of that reasoning, made available to every future consumer.

In this mode, insights are automatically created and maintained:

- **Created** because every new reasoning product is designed around producing a structured artifact, not around printing an answer to stdout.
- **Maintained** because the producer's refresh discipline is wired into a build step that already runs on the relevant trigger (config build, asset save, gather, deploy), so the artifact is current whenever its source data is current.

A reasoning product designed this way is also more useful to its first consumer: the structured artifact is easier to test, easier to query in unanticipated ways, and easier to compose with other materialized insights than a transient print-and-exit answer.

The implication for new work: when a task calls for a deep scan or non-trivial inference over project data, the first design question is *what materialized insight does this scan produce?* and *which build step owns its refresh?* — not *how do I print the answer faster?*

### Practices

- **Recognize the smell.** A script that takes minutes to produce a small result, multiple developers writing similar deep scans, or "I had to re-run that to find out" — these are signals that an insight is missing, not that the script needs to be faster.
- **Pick the canonical source.** The materialized insight is computed *from* canonical project data (CSVs, configs, source code, assets). The canonical source does not move into the insight; the insight references it.
- **Choose a refresh discipline up front.** Decide before writing the producer: does it run on every config build? On every asset save? On demand via an explicit command? In CI? The refresh rule is the contract that future consumers rely on.
- **Make consumers trivial.** A well-engineered insight is consumed by reading a file and doing set membership / dict lookup. If a consumer has to re-parse, re-walk, or re-infer, the insight is either missing data or carrying it in the wrong shape.
- **Document the insight near its consumers** when it is non-trivially shaped or has cross-cutting consumers, so future tooling discovers it instead of duplicating it.

### Anti-patterns

- **Throwaway deep scan.** A script that recomputes the same expensive result on every invocation. Replace with a materialized insight whose producer the script triggers (or assumes is fresh).
- **Insight in the wrong shape.** A producer that emits raw bytes or unstructured text, forcing every consumer to re-parse. Emit structured data (YAML/JSON with a schema).
- **Insight without a refresh discipline.** A file someone generated once and committed, with no rule for when it is stale. These rot silently.
- **Inferred-rather-than-canonical insight.** A consumer guesses at the answer with regex or string heuristics instead of reading the materialized insight produced by the system that owns the data. The insight should be produced by the authoritative system, not by external pattern-matching.
