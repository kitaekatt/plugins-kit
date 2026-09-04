---
_schema_version: 1
name: plugin-dev
author: christina
skill-type: technique-skill
description: Use when cross-plugin edge, REQUIRED/REFUSE/DEGRADE, skill enable, optional-lib probe, or silent vs disclosed. Do NOT use for bootstrap config or md authoring.
---

# Plugin development

Choose REQUIRED when the plugin cannot work without the other plugin. For an
optional edge, choose REFUSE when the artifact would be false without it, or
DEGRADE when the artifact stays true; enabling is DEGRADE's silent sub-case
when the consuming skill can host a consented probe.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Procedure for choosing and applying the REQUIRED / REFUSE / DEGRADE contract for cross-plugin capability edges.
  scope:
    covers:
      - cross-plugin imports and optional shared-library edges
      - REQUIRED, REFUSE, and DEGRADE branch selection
      - skill-embedded enabling and consented capability probes
    excludes:
      - bootstrap manifest configuration
      - skill and CLAUDE.md authoring standards
  techniques:
    - id: choose_dependency_branch
      name: Choose and apply the cross-plugin dependency branch
      keywords: [cross-plugin import, REQUIRED REFUSE DEGRADE, optional library, skill enabling, silent degradation]
      goal: Select the dependency branch from the plugin's no-owner path and implement the matching mechanics.
      steps:
        - n: 1
          action: Answer whether the plugin can do its job without the other plugin. Classify the edge as REQUIRED when it cannot, or optional when it can.
          expected: One dependency branch is selected.
        - n: 2
          action: For an optional edge, read the no-owner artifact as a user would. Choose REFUSE if it would be false, or DEGRADE if it remains true; use enabling only for the silent artifact-true sub-case with a consuming skill probe host.
          expected: The optional branch and its artifact-truth reason are recorded.
        - n: 3
          action: Load the matching reference, implement its manifest, import, probe, disclosure, and test mechanics, and preserve the no-silent-substitution rule.
          tool: Read the selected reference in this skill's references/ directory.
          expected: The implementation and its state-specific tests match the selected branch.
      gotchas:
        - Enabling is not a fourth branch: absent and too-old capabilities stay silent only when the no-owner artifact remains true.

references:
  - id: optional_plugin_dependencies
    path: references/optional-plugin-dependencies.md
    keywords: [optional plugin dependency, REQUIRED REFUSE DEGRADE, frontier probe, artifact disclosure, cross-plugin import]
    summary: Contract and mechanics for required or optional cross-plugin imports, including REFUSE, DEGRADE, state diagnosis, and review checks.
  - id: skill_embedded_enabling
    path: references/enabling.md
    keywords: [skill enabling, consented probe, silent absence, capability advertisement, stale capability, disclosure scope, EN criteria, enabling reviewer checklist]
    summary: Skill-hosted enabling contract for present disclosure and silence when an absent or old capability leaves the artifact true; EN-1..EN-7 criteria, consent rules, anti-patterns, and reviewer checklist.
```
