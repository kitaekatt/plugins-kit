# standards decisions

Decision provenance for the per-artifact standards docs in
`references/standards/`. Each record follows the surface / finding /
follow-up convention (plugins/skills-kit/CLAUDE.md, conventions). The
canonical rule text lives in the standards doc it amended; this file records
why the rule was tightened so the decision can be rewound.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/md-domain/references/standards
    covers:
      - decisions that tightened or amended a standards-doc criterion
    excludes:
      - the criteria themselves (live in the standards docs)
      - framework-vocabulary decisions (skill-authoring-decisions.md)
  insights:
    - id: a3_provenance_path_rule
      keywords: [provenance path, origin field, tmp path, gitignored citation, ephemeral artifact, A-3, run disagreement, idempotency]
      summary: "A-3 amended 2026-08-03: a provenance field citing an untracked path (e.g. tmp/...) is a decisive FIX (drop the path, keep description/date/ids), never an accepted historical pattern."
      detail: |
        Surface: back-to-back full audits of plugins/skills-kit (2026-08-03,
        detect lanes opus/high) disagreed on skills_kit_lib/CLAUDE.md line
        175 -- run 1 classified the gitignored tmp/ citation in an origin:
        field SILENT ("accepted structural pattern"), run 2 FAILed it as a
        broken reference. Finding: both readings were defensible under the
        old A-3 text, so the outcome depended on per-run judgment. Follow-up:
        the criterion now decides it -- cite ephemeral work by description,
        date, and finding ids; a path-form citation to an untracked location
        is a loss-free FIX. Rule text: claude-md-standards.md A-3.
      origin: Adjudicated 2026-08-03 after the run-1/run-2 disagreement; user directed that run disagreements be settled by tightening criteria.
      added: "2026-08-03"
    - id: h9_annotation_ceiling_test
      keywords: [H-9, pointer map, annotation ceiling, embedded documentation, error driver, routing payload, surface map, run disagreement]
      summary: "H-9 amended 2026-08-03: a pointer-map annotation may exceed one line only for constraint/error-driver lines not stated at the target; lines summarizing the target's own content/structure trip H-9 regardless of the map's routing value."
      detail: |
        Surface: the same back-to-back audits disagreed on the
        plugin_surface_overview map in plugins/skills-kit/CLAUDE.md -- run 1
        passed H-9 ("the map is the file's declared routing payload, so it
        earns the annotation"), run 2 FAILed it (multi-line recaps of the
        targets' own layout and config format re-embed deferred
        documentation). Finding: "earns the annotation" was a vibe, not a
        test, so the whole map's fate flipped per run. Follow-up: H-9 now
        carries a per-annotation test -- keep lines stating a constraint or
        agent-error driver absent at the target; collapse lines recapping the
        target's structure. Blanket load-bearing claims are explicitly not an
        exemption. Rule text: claude-md-standards.md H-9.
      origin: Adjudicated 2026-08-03 after the run-1/run-2 disagreement; applied to plugins/skills-kit/CLAUDE.md in the same change.
      added: "2026-08-03"
```
