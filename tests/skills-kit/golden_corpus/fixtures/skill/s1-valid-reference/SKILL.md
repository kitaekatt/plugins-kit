---
name: s1-valid-reference
description: Use when testing the golden corpus valid reference-skill path. Do NOT use for real work; fixture only.
skill-type: reference-skill
---

# S1 Valid Reference Skill

Reference fixture for widget limits: a well-formed reference-skill floor.

```yaml
reference_skill:
  _schema_version: "1"
  identity: Reference for widget limits in the fixture project.
  scope:
    covers:
      - widget limit values
    excludes:
      - widget assembly procedure
  facts:
    - id: max_widgets
      summary: The widget limit is 42.
      keywords: [widget, limit, maximum]
      detail: The fixture system rejects the 43rd widget. See references/details.md.
      gotchas:
        - The limit is per-batch, not global.
      example:
        input: add 43 widgets
        output: rejected with E_LIMIT
```

Deeper background lives in [references/details.md](references/details.md).
