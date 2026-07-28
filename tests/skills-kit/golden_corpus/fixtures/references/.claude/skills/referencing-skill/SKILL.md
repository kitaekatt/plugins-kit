---
name: referencing-skill
description: Use when testing the golden corpus broken-reference paths. Do NOT use for real work; fixture only.
---

# Referencing Skill

Valid reference: use /present-skill for resolution checks.

Broken soft reference: see /missing-skill for a skill that does not exist.

Broken hard dependency -- step 1 invokes the Skill tool with
skill: "absent-skill" and must be flagged as a missing hard dep.
