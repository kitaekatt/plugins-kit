---
name: restart
description: "Use when the user says /restart or asks to restart the client or hand work to a new agent: stop work, restart or report. Do NOT use to write a hand-off doc."
argument-hint: "[hand-off [<task-ref>]]"
skill-type: technique-skill
---

# Restart

The user is about to end this session. Get out of the way cleanly; do not
finish the task. When the checklist finds no blocker, the
`mcp__relaunch-kit__request_restart` tool exits Claude Code about 3 seconds
after this turn ends and the terminal's `clauder` launcher starts the next
session. Not calling the tool is how you block that automatic restart.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Stop new work, then restart this terminal's session automatically when nothing blocks it, or report readiness.
  scope:
    covers:
      - restarting the client (bootstrap changes, plugin updates) and resuming this session
      - handing a task to a fresh session through the task-kit:task hand-off steps
      - reporting each step skipped because the session was not started under clauder
      - what `clauder --relay` adds to a launch, and how a session reports a relay launch that failed or stalls
    excludes:
      - writing a hand-off document
      - finishing or publishing the current task
  techniques:
    - id: restart_session
      name: Restart or hand off
      keywords: [restart, hand off, handoff, new session, reload plugins, bootstrap changes, relaunch, clauder]
      goal: Call request_restart once the checklist is clear, or end with a one-line readiness report and the reason the restart stays manual.
      steps:
        - n: 1
          action: >-
            Read the intent from the arguments and this turn's user message.
            `hand-off [<ref>]`, or plain words asking to hand the work to a new
            agent, means hand-off: a fresh session continues the task. No
            argument means restart: this session resumes after the relaunch and
            waits, with no continuation prompt. A hand-off you only inferred
            (for example, little context window remains) is never automatic:
            checklist item 8 blocks the tool call.
        - n: 2
          action: >-
            Stop. Start no new work and delegate nothing: no new subagents,
            background jobs, edits, commits, or reviews. The one exception is
            the hand-off packaging in step 4.
        - n: 3
          action: >-
            Let running background agents and jobs finish or reach a stopping
            point; start no replacements. If one is stuck, name it and ask
            whether to stop it.
        - n: 4
          action: >-
            Hand-off only. Find the task ref before any release, in this order:
            the argument; then the registration whose `session_id` equals
            `$CLAUDE_CODE_SESSION_ID` in the output of the `task-kit:task`
            `sessions list --format json` (its `task` field is the ref); then
            the `/task work <ref>` line in this conversation. Then run the
            `task-kit:task` hand_off steps 2 to 4 (update, self-verify, commit
            and push the task folder, release the registration). When no ref
            is found, or `task-kit:task` is unavailable, checklist item 3 is YES.
        - n: 5
          action: Walk the veto checklist. Any YES blocks the automatic restart.
          detail: |
            1. Does a background agent, workflow, shell job or Monitor that this
               session owns still run?
            2. Hand-off: did update or validate end with findings, or did the
               task-folder commit or push fail?
            3. Hand-off: is the task ref missing or ambiguous?
            4. Hand-off: did self-verify leave something a cold reader could not
               resolve?
            5. Is a decision or an answer from the user pending that the next
               session would need?
            6. Is this a subagent, or not the session the user is talking to?
            7. Did the user ask for a manual restart, or say not to restart?
            8. Is the restart or hand-off inferred, rather than asked for in this
               turn's user message?
            9. Is this a restart to load changes that are not yet published or
               provisioned, so that the new session would run the old code?
        - n: 6
          action: >-
            With all nine answers NO, call `mcp__relaunch-kit__request_restart`
            with `mode`, `reason` (one line) and, for a hand-off only, `prompt`
            (`/task work <ref>`, one line). For a restart, `mode` is "resume"
            and `prompt` is omitted. Read the JSON string it returns, then end
            the turn with the matching ending below. Start no other work after
            the call.
        - n: 7
          action: End the turn with the ending that matches the tool result or the checklist.
          detail: |
            - `status` "armed": one line saying the restart follows this turn
              and that typing anything or an interrupted turn cancels it. A
              hand-off adds the baton: `CWD: <project root>`, then
              `Continue: /task work <ref>`.
            - `status` "no-launcher" or "disabled": nothing is armed and nothing
              exits. A hand-off gives the baton first. Then report what was
              skipped, taking `<next_command>` from the result:
              `Skipped: exit and relaunch -- this session was not started under
              clauder (or: automatic relaunch is disabled: <reason>). Done:
              <steps run>. To continue: exit, then run <next_command>.`
            - A checklist blocker: name it in one line, then reply "Ready to
              Restart" or "Ready to Hand Off" (a hand-off adds the baton). Add a
              short line only for state the user would lose or need next:
              uncommitted or unpushed changes, a job you stopped, or where the
              next agent should start.
            - A tool denial: relay its text in one line, then end as for a
              blocker.
            - The tool is not in your tool list: end as for a blocker and add
              `Skipped: exit and relaunch -- the request_restart tool is not
              available`.
      gotchas:
        - Asking whether to continue the task. The user has chosen to stop; answer questions briefly and stay stopped.
        - Writing a hand-off document unasked. The baton is the only hand-off text.
        - Calling request_restart to be helpful when a checklist item is YES. Withholding the call is the veto, and the reply names the item.
        - Treating "armed" as proof of a restart. The exit follows only when this turn ends with a normal answer; the launcher then starts the next session.
    - id: relay_launch
      name: Account for a clauder --relay launch
      keywords: [relay, clauder --relay, channel, llm-kit, development channels, mcp-config, relaunch dialog]
      goal: Tell the user what `clauder --relay` did or refused, and why a relaunched session may wait.
      steps:
        - n: 1
          action: >-
            Know what `clauder --relay` adds. clauder removes the option before
            claude sees it and adds it again on every relaunch: the claude flags
            `--mcp-config <llm-kit>/channel/mcp.json`,
            `--dangerously-load-development-channels server:relay` and
            `--allowedTools mcp__relay__reply`, and the variables
            `BOOTSTRAP_PYTHON` (the caller's value, else clauder's interpreter)
            and `LLM_KIT_CHANNEL_SCRIPT` (the absolute path of
            `relay_channel.py`), which `mcp.json` expands. A restart or
            hand-off from such a session keeps all of it; do not add or remove
            these flags yourself.
        - n: 2
          action: >-
            When the user reports that `clauder --relay` printed
            `clauder: ...` and exited with status 2, read the message and pass
            it on. "none is installed" means `claude plugin install
            llm-kit@plugins-kit`; "predates the relay channel" means the
            installed llm-kit is older than 0.17.0, the first version to ship
            the channel, so run `claude plugin update llm-kit@plugins-kit`. clauder finds llm-kit in this
            order: `LLM_KIT_CHANNEL_SCRIPT`; under claudx the llm-kit directory
            in `CLAUDE_PLUGIN_TEST_DIRS` (never the cache; the fix is
            `claudx --all`); else the newest version in
            `~/.claude/plugins/cache/plugins-kit/llm-kit/`. `--relay` is also
            refused with `-p`, `--bg` and the other one-shot modes, a claude
            subcommand, and a stdin or stdout that is not a terminal.
        - n: 3
          action: >-
            Before a restart or hand-off from a relay session, say in the
            ending line that the relaunched claude shows the development-channels
            dialog (shown on every launch) and waits there until someone
            confirms it, so an unattended relaunch does not proceed on its own.
      gotchas:
        - Substituting another llm-kit, or launching without the relay, when the refusal names a missing or old llm-kit. The refusal is the answer.
```
