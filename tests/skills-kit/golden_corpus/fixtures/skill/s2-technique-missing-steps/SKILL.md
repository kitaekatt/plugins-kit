---
name: s2-technique-missing-steps
description: Use when testing the golden corpus schema-FAIL path. Do NOT use for real work; fixture only.
skill-type: technique-skill
---

# S2 Technique Missing Steps

Technique fixture whose contract block omits the required steps list.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Technique for renaming widgets in the fixture project.
  scope:
    covers:
      - widget renaming
    excludes:
      - widget deletion
  techniques:
    - id: rename_widget
      name: Rename a widget
      keywords: [widget, rename, batch]
      goal: Rename a widget without breaking batch references.
      gotchas:
        - Renames are case-sensitive.
```
