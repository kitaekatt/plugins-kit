---
name: recap
description: "Use when the user asks for a recap, returns from afk, or an afk or autonomous run ends: a recap fit to its trigger. Do NOT use to summarize documents or code."
argument-hint: "[return|afk-ending|autonomous-done <pid>]"
skill-type: technique-skill
---

# recap

Write a recap fitted to what triggered it, and record it so the Stop hook can
see it. The presence model is `${CLAUDE_PLUGIN_ROOT}/references/presence.md`.

Arguments: $ARGUMENTS

1. Follow `${CLAUDE_SKILL_DIR}/references/recap-procedure.md` with the
   arguments above. Read it first, unless its text is already in your context
   this session: this skill can fire many times in a session and the procedure
   should load once. It names `recap-rows.md`, `recap-templates.md` and
   `recap-interview.md` in the same folder and says when to read each.
