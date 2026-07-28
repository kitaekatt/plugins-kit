---
name: s3-mixed-type
description: Use when testing the golden corpus mixed-type detection path. Do NOT use for real work; fixture only.
skill-type: reference-skill
---

# S3 Mixed Type

Reference fixture that smuggles discipline-skill content (a rules list) into
a reference_skill block.

```yaml
reference_skill:
  _schema_version: "1"
  identity: Reference for widget limits that also tries to carry rules.
  scope:
    covers:
      - widget limit values
    excludes:
      - widget assembly procedure
  facts:
    - id: max_widgets
      summary: The widget limit is 42.
      keywords: [widget, limit, maximum]
      detail: The fixture system rejects the 43rd widget.
      gotchas:
        - The limit is per-batch, not global.
      example:
        input: add 43 widgets
        output: rejected with E_LIMIT
  rules:
    - id: never_exceed_limit
      statement: Never exceed the widget limit.
```
