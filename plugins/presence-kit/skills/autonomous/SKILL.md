---
name: autonomous
description: "Use when the user hands work over to finish without them: decide instead of asking, recap at the end. Do NOT use for a plain afk or a quiet user."
skill-type: technique-wrapper
---

# autonomous

Record the user's hand-over as a granted goal and work it through, or revoke
it. The presence model is `${CLAUDE_PLUGIN_ROOT}/references/presence.md`.

Arguments: $ARGUMENTS

1. Follow the matching section of
   `${CLAUDE_SKILL_DIR}/references/autonomous-procedure.md` ("Record the
   hand-over and work the goal", or "Revoke the hand-over") with the arguments
   above. Read it first, unless its text is already in your context this
   session: this skill can fire many times in a session and the procedure
   should load once.
