---
name: afk
description: "Use when the user says afk or steps away, even with nothing running: cover jobs, park questions, recap. Do NOT use for a hand-over (presence-kit:autonomous)."
skill-type: technique-skill
---

# afk

Put this session into afk and apply the away behaviors. The presence model is
`${CLAUDE_PLUGIN_ROOT}/references/presence.md`.

1. Follow `${CLAUDE_SKILL_DIR}/references/afk-procedure.md`. Read it first,
   unless its text is already in your context this session: this skill can fire
   many times in a session and the procedure should load once.
