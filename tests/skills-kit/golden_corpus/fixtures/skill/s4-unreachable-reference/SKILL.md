---
name: s4-unreachable-reference
description: Use when testing the golden corpus reachability-FAIL path. Do NOT use for real work; fixture only.
skill-type: reference-skill
---

# S4 Unreachable Reference

Reference fixture whose references directory holds a file no SKILL.md edge
reaches.

```yaml
reference_skill:
  _schema_version: "1"
  identity: Reference for widget limits with an orphaned reference file.
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
```
