# cohesion-principles/ insights

Decision provenance for the cohesion-principles skill (the placement spine).
The canonical content lives in SKILL.md; this file records the decisions about
the skill's own shape so they do not bleed into the SKILL.md body.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/cohesion-principles
    covers:
      - shape decisions about the cohesion-principles SKILL.md itself
    excludes:
      - the placement framework content (lives in SKILL.md)
  insights:
    - id: crp_verdict_keep_whole
      keywords: [crp verdict, no references dir, over threshold, keep whole, split evaluated, tool-call doubling, dec-11]
      summary: The SKILL.md is over the 500-line signal threshold (533 lines) with no references/ dir -- evaluated 2026-06-10 and deliberately kept whole; do not re-propose a split without new structure.
      detail: |
        Dec-11 evaluation (size is a signal, CRP is the test): the body is an
        Index facts block that routes into ONE content_allocation framework
        block, consumed as a unit by every reader (md-audit members judge
        against it; md-authoring places content by it). No section can be
        omitted on a typical invocation, so any L2 -> L3 split would produce a
        stub plus an always-co-loaded reference -- a tool-call doubling, not a
        context-efficiency win. Verdict: COMPLIANT as-is; keep whole. Re-run
        the CRP test only if the skill grows a genuinely separable sub-task
        (e.g. a worked-examples corpus readers skip on normal placement calls).
      origin: Arch-review finding S18 (2026-06-09); CRP evaluated during the S2-S18 remediation pass 2026-06-10.
      added: "2026-06-10"
```
