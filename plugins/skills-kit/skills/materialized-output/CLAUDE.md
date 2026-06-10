# materialized-output

Pattern-skill teaching the *materialized insights* model and the *insight engineering* discipline. Per-directory meta about this skill -- type rationale (pattern- vs discipline-skill), content origin/provenance, and future composition -- lives in the `claude_md:` insights below.

## Insights

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/materialized-output
    covers:
      - meta about the materialized-output skill itself
      - skill type rationale (pattern-skill vs discipline-skill)
      - origin and provenance of the skill's content
      - future composition with sibling skills
    excludes:
      - the materialized-insights model itself (lives in SKILL.md and references)
      - generic materialized-output practices (taught by the skill, not by this CLAUDE.md)
  insights:
    - id: pattern_skill_classification
      keywords: [pattern-skill, classification, recognition, discipline-skill, type rationale, graduation]
      summary: materialized-output is classified as a pattern-skill because its primary value is teaching recognition of when the materialized-insights pattern applies, not enforcing rules under pressure.
      detail: |
        The skill's "practices" and "anti-patterns" sections reinforce the model
        rather than enforcing rules under pressure. If a future audit finds developers
        repeatedly violating the practices despite knowing the model, that is the
        signal to graduate this into a paired discipline-skill. Until then, keep it
        as a pattern-skill -- discipline-skill type would imply enforcement scaffolding
        that doesn't fit how this content is consumed.
      origin: Type rationale captured at skill creation (2026-05-19).
      added: "2026-05-19"
    - id: ported_from_project_glossary
      keywords: [origin, port, project-glossary, generic examples, cross-domain, stripped]
      summary: The skill's content was ported from project-glossary material in a different repo and made generic by stripping project-specific examples.
      detail: |
        Originally drafted in a different repo's Docs/Glossary.md.html. The framework
        portion was generic enough to move here; project-specific examples were
        stripped and replaced with cross-domain generic examples (search index over
        docs, pre-expanded config graph, codebase reference map, model-derived summary
        cache, pricing lookup). When updating examples, prefer cross-domain generics
        over project-specific cases so the skill stays portable.
      origin: Authored as a port of project-glossary content from a different repo's Docs/Glossary.md.html.
      added: "2026-05-19"
    - id: future_tool_design_domain_skill
      keywords: [future composition, tool-design, domain-skill, sibling skill, bundling]
      summary: If a tool/script-design sibling skill emerges, materialized-output could become a member of a bundling domain-skill.
      detail: |
        A future "tool-design" skill (covering CLI shape, exit-code discipline,
        scriptability) would be a natural sibling. At that point, a domain-skill
        could bundle the family. Until that sibling exists, keep this skill
        standalone -- premature bundling adds indirection without payoff.
      origin: Forward-looking composition note captured at skill creation (2026-05-19).
      added: "2026-05-19"
```

